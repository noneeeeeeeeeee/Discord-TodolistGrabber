"""
Enrichment Worker - Factory Pipeline for V3 Autoplay Engine.

This module implements the factory worker pattern for processing tracks:
1. Track Queue → Deezer Resolution (preview URL + metadata)
2. Deezer → Audio Analysis (Librosa + EfficientAT)
3. Parallel: Gemini Cultural Enrichment (batched)

The worker operates independently from the Track Fetcher (bootstrap_manager)
and the Recommender, providing a clean separation of concerns.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Any, Tuple

from .config import (
    DEFAULT_VERBOSITY,
    GEMINI_BATCH_SIZE,
    GEMINI_BATCH_TIMEOUT_SECONDS,
    PRIORITY_DAYDREAM,
)
from .deezer_fetch import DeezerClient
from .cache_manager import EnrichmentEntry, MappingEntry

if TYPE_CHECKING:
    from .autoplayengine_v3 import AutoplayEngineV3

LOG = logging.getLogger(__name__)


class Priority(IntEnum):
    """Priority levels for enrichment tasks."""
    USER = 1       # User request - highest priority, blocks until complete
    BUFFER = 2     # Buffer refill - high priority for Apple Music style buffer
    DAYDREAM = 3   # Background daydreaming - lowest priority


@dataclass
class EnrichmentTask:
    """A task in the enrichment queue."""
    artist: str
    title: str
    priority: Priority
    added_at: float = field(default_factory=time.time)
    source: str = "unknown"  # "lastfm", "deezer", "user", "similar"
    
    # Deezer resolution results (populated after stage 1)
    deezer_id: Optional[int] = None
    preview_url: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    deezer_bpm: Optional[int] = None
    
    # Processing state
    stage: int = 0  # 0=queued, 1=deezer_resolved, 2=analyzing, 3=complete
    error: Optional[str] = None
    
    @property
    def track_key(self) -> str:
        """Normalized key for deduplication."""
        return f"{self.artist.lower().strip()}::{self.title.lower().strip()}"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "artist": self.artist,
            "title": self.title,
            "priority": self.priority,
            "added_at": self.added_at,
            "source": self.source,
            "deezer_id": self.deezer_id,
            "preview_url": self.preview_url,
            "genres": self.genres,
            "deezer_bpm": self.deezer_bpm,
            "stage": self.stage,
            "error": self.error,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EnrichmentTask":
        return cls(
            artist=data["artist"],
            title=data["title"],
            priority=Priority(data.get("priority", Priority.DAYDREAM)),
            added_at=data.get("added_at", time.time()),
            source=data.get("source", "unknown"),
            deezer_id=data.get("deezer_id"),
            preview_url=data.get("preview_url"),
            genres=data.get("genres", []),
            deezer_bpm=data.get("deezer_bpm"),
            stage=data.get("stage", 0),
            error=data.get("error"),
        )


class EnrichmentWorker:
    """
    Factory worker that processes tracks through the enrichment pipeline.
    
    Pipeline Stages:
    1. Deezer Resolution: Search Deezer, get preview URL and metadata
    2. Audio Analysis: Librosa (BPM, key) + EfficientAT (embeddings)
    3. Gemini Cultural: Batch queue for cultural context (parallel)
    
    Priority System:
    - P1 (USER): Immediate processing, caller waits
    - P2 (BUFFER): High priority for autoplay buffer
    - P3 (DAYDREAM): Background processing, never blocks
    """
    
    # Queue limits
    MAX_QUEUE_SIZE = 200
    
    def __init__(self, engine: "AutoplayEngineV3"):
        self.engine = engine
        self._verbose = DEFAULT_VERBOSITY
        
        # Main task queue (priority sorted)
        self._queue: List[EnrichmentTask] = []
        self._queue_lock = asyncio.Lock()
        
        # Track keys in queue for deduplication
        self._queued_keys: Set[str] = set()
        
        # Processing state
        self._is_running = False
        self._worker_task: Optional[asyncio.Task] = None
        
        # Gemini batch queue (for parallel cultural enrichment)
        self._gemini_batch: List[EnrichmentTask] = []
        self._gemini_batch_lock = asyncio.Lock()
        self._gemini_last_add_time: float = 0.0
        self._gemini_flush_task: Optional[asyncio.Task] = None
        
        # Stats
        self._stats = {
            "tasks_queued": 0,
            "tasks_completed": 0,
            "deezer_resolved": 0,
            "audio_analyzed": 0,
            "gemini_enriched": 0,
            "errors": 0,
        }
        
        # Persistence
        self._queue_file = Path("cache/music/enrichment_queue.json")
        self._load_queue()
    
    def _vlog(self, level: int, message: str, *args) -> None:
        """Log based on verbosity level."""
        if self._verbose < level:
            return
        log_fn = LOG.info if level == 1 else LOG.debug
        log_fn(message, *args)
    
    # =========================================================================
    # Queue Management
    # =========================================================================
    
    def _load_queue(self) -> None:
        """Load queue from disk for persistence across restarts."""
        if self._queue_file.exists():
            try:
                with open(self._queue_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tasks = data.get("tasks", [])
                    for task_data in tasks:
                        task = EnrichmentTask.from_dict(task_data)
                        self._queue.append(task)
                        self._queued_keys.add(task.track_key)
                    self._vlog(1, "🏭 [Worker] Loaded %d tasks from queue", len(self._queue))
            except Exception as e:
                LOG.warning("⚠️ [Worker] Failed to load queue: %s", str(e)[:100])
    
    def _save_queue(self) -> None:
        """Save queue to disk for persistence."""
        try:
            self._queue_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._queue_file, "w", encoding="utf-8") as f:
                json.dump({
                    "tasks": [task.to_dict() for task in self._queue],
                    "saved_at": time.time(),
                }, f)
        except Exception as e:
            LOG.warning("⚠️ [Worker] Failed to save queue: %s", str(e)[:100])
    
    async def add_task(
        self,
        artist: str,
        title: str,
        priority: Priority = Priority.DAYDREAM,
        source: str = "unknown",
    ) -> bool:
        """
        Add a track to the enrichment queue.
        
        Args:
            artist: Artist name
            title: Track title
            priority: Task priority (USER, BUFFER, DAYDREAM)
            source: Where the track came from (lastfm, deezer, user, etc.)
        
        Returns:
            True if added, False if duplicate or queue full
        """
        task = EnrichmentTask(
            artist=artist.strip(),
            title=title.strip(),
            priority=priority,
            source=source,
        )
        
        async with self._queue_lock:
            # Check for duplicates
            if task.track_key in self._queued_keys:
                self._vlog(2, "⏭️ [Worker] Duplicate skipped: %s", task.track_key)
                return False
            
            # Check if already enriched
            existing = await self.engine._cache.get_enrichment(artist, title)
            if existing and existing.analysis_verified:
                self._vlog(2, "⏭️ [Worker] Already enriched: %s", task.track_key)
                return False
            
            # Check queue limit (only for DAYDREAM priority)
            if priority == Priority.DAYDREAM and len(self._queue) >= self.MAX_QUEUE_SIZE:
                self._vlog(2, "⚠️ [Worker] Queue full, skipping: %s", task.track_key)
                return False
            
            # Add to queue
            self._queue.append(task)
            self._queued_keys.add(task.track_key)
            self._stats["tasks_queued"] += 1
            
            # Sort by priority (lower = higher priority)
            self._queue.sort(key=lambda t: (t.priority, t.added_at))
            
            self._save_queue()
            self._vlog(2, "📥 [Worker] Queued: %s - %s (P%d)", artist, title, priority)
            return True
    
    async def add_batch(
        self,
        tracks: List[Tuple[str, str]],
        priority: Priority = Priority.DAYDREAM,
        source: str = "unknown",
    ) -> int:
        """
        Add multiple tracks to the queue.
        
        Args:
            tracks: List of (artist, title) tuples
            priority: Task priority
            source: Track source
        
        Returns:
            Number of tracks added
        """
        added = 0
        for artist, title in tracks:
            if await self.add_task(artist, title, priority, source):
                added += 1
        
        if added > 0:
            self._vlog(1, "📥 [Worker] Batch added: %d/%d tracks (P%d)", added, len(tracks), priority)
        
        return added
    
    def get_queue_size(self) -> int:
        """Get current queue size."""
        return len(self._queue)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get worker statistics."""
        return {
            **self._stats,
            "queue_size": len(self._queue),
            "gemini_batch_size": len(self._gemini_batch),
            "is_running": self._is_running,
        }
    
    # =========================================================================
    # Worker Lifecycle
    # =========================================================================
    
    async def start(self) -> None:
        """Start the enrichment worker."""
        if self._is_running:
            return
        
        self._is_running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._vlog(1, "🏭 [Worker] Started enrichment worker")
    
    async def stop(self) -> None:
        """Stop the enrichment worker."""
        self._is_running = False
        
        # Flush Gemini batch
        if self._gemini_flush_task:
            self._gemini_flush_task.cancel()
        await self._flush_gemini_batch()
        
        # Stop worker
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        
        self._save_queue()
        self._vlog(1, "🏭 [Worker] Stopped enrichment worker")
    
    # =========================================================================
    # Main Worker Loop
    # =========================================================================
    
    async def _worker_loop(self) -> None:
        """Main worker loop - processes tasks from queue."""
        while self._is_running:
            try:
                # Get next task
                task = await self._get_next_task()
                if not task:
                    await asyncio.sleep(1)  # Idle wait
                    continue
                
                # Process task through pipeline
                await self._process_task(task)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                LOG.error("❌ [Worker] Loop error: %s", str(e)[:100])
                await asyncio.sleep(5)
    
    async def _get_next_task(self) -> Optional[EnrichmentTask]:
        """Get the next task from queue (priority-ordered)."""
        async with self._queue_lock:
            if not self._queue:
                return None
            return self._queue[0]  # Already sorted by priority
    
    async def _complete_task(self, task: EnrichmentTask, success: bool = True) -> None:
        """Mark task as complete and remove from queue."""
        async with self._queue_lock:
            if task in self._queue:
                self._queue.remove(task)
            self._queued_keys.discard(task.track_key)
            
            if success:
                self._stats["tasks_completed"] += 1
            else:
                self._stats["errors"] += 1
            
            self._save_queue()
    
    # =========================================================================
    # Pipeline Stages
    # =========================================================================
    
    async def _process_task(self, task: EnrichmentTask) -> None:
        """Process a single task through the full pipeline."""
        try:
            self._vlog(2, "🏭 [Worker] Processing: %s - %s", task.artist, task.title)
            
            # Stage 1: Deezer Resolution
            if task.stage < 1:
                success = await self._stage_deezer_resolution(task)
                if not success:
                    await self._complete_task(task, success=False)
                    return
                task.stage = 1
            
            # Stage 2: Audio Analysis
            if task.stage < 2:
                success = await self._stage_audio_analysis(task)
                if not success:
                    await self._complete_task(task, success=False)
                    return
                task.stage = 2
            
            # Stage 3: Queue for Gemini (parallel, non-blocking)
            await self._queue_gemini_enrichment(task)
            task.stage = 3
            
            await self._complete_task(task, success=True)
            self._vlog(2, "✅ [Worker] Complete: %s - %s", task.artist, task.title)
            
        except Exception as e:
            task.error = str(e)[:100]
            await self._complete_task(task, success=False)
            LOG.warning("❌ [Worker] Task failed: %s - %s", task.artist, str(e)[:100])
    
    async def _stage_deezer_resolution(self, task: EnrichmentTask) -> bool:
        """
        Stage 1: Resolve track on Deezer.
        
        Gets:
        - Deezer track ID
        - Preview URL (30s HQ audio)
        - Genres
        - BPM (if available)
        """
        async with DeezerClient(max_concurrent=5, timeout=10.0) as client:
            # Search for track - returns a list of results
            query = f"{task.artist} {task.title}"
            results = await client.search_track(query)
            
            if not results:
                self._vlog(2, "⚠️ [Worker] Deezer not found: %s - %s", task.artist, task.title)
                return False
            
            # Use the best match (first result)
            result = results[0]
            
            task.deezer_id = int(result.id) if result.id else None
            task.preview_url = result.preview_url
            task.genres = result.genres if hasattr(result, 'genres') else []
            task.deezer_bpm = result.bpm if hasattr(result, 'bpm') else None
            
            self._stats["deezer_resolved"] += 1
            self._vlog(2, "✅ [Worker] Deezer resolved: %s (preview: %s)", 
                      task.track_key, "yes" if task.preview_url else "no")
            
            return task.preview_url is not None
    
    async def _stage_audio_analysis(self, task: EnrichmentTask) -> bool:
        """
        Stage 2: Analyze audio using Librosa + EfficientAT.
        
        Gets:
        - BPM, key, mode, loudness (Librosa)
        - 512D-2048D embedding (EfficientAT MobileNet)
        - 5D simple_vibe vector (non-ML fallback)
        """
        if not task.preview_url:
            return False
        
        # Queue for analysis using the engine's analysis system
        try:
            # Create or update cache entry first
            track_key = self.engine._make_track_key(task.artist, task.title)
            existing = await self.engine._cache.get_enrichment(task.artist, task.title)
            
            if existing:
                entry = existing
            else:
                entry = EnrichmentEntry(
                    tags=[],
                    mood=None,
                    fetched_at=time.time(),
                    genres=task.genres,
                    bpm=task.deezer_bpm,
                )
            
            # Update with Deezer data
            entry.genres = task.genres
            entry.bpm = task.deezer_bpm
            await self.engine._cache.set_enrichment(task.artist, task.title, entry)
            
            # Create mapping entry with preview URL (required for queue_analysis)
            mapping = MappingEntry(
                youtube_id="deezer_preview",  # Placeholder - Deezer-only architecture
                url="",
                timestamp=time.time(),
                preview_url=task.preview_url,
                preview_duration_ms=30000,  # Deezer previews are 30s
                preview_fetched_at=time.time(),
                deezer_track_id=str(task.deezer_id) if task.deezer_id else None,
                ingest_source="enrichment_worker",
            )
            await self.engine._cache.set_mapping(task.artist, task.title, mapping)
            
            # Map priority to engine's priority system
            engine_priority = PRIORITY_DAYDREAM  # Default to lowest
            if task.priority == Priority.USER:
                engine_priority = 1  # PRIORITY_ACTIVE
            elif task.priority == Priority.BUFFER:
                engine_priority = 2  # PRIORITY_BUFFER
            
            # Queue for EfficientAT analysis (uses engine's worker system)
            # queue_analysis expects: track_id (artist::title format), youtube_url (for compat), priority
            success = self.engine.queue_analysis(
                track_key,
                "deezer_preview",  # Placeholder URL - actual preview is in mapping
                priority=engine_priority,
            )
            
            if success:
                self._stats["audio_analyzed"] += 1
            
            return success
            
        except Exception as e:
            LOG.warning("⚠️ [Worker] Audio analysis failed: %s", str(e)[:100])
            return False
    
    # =========================================================================
    # Gemini Batch Queue (Parallel)
    # =========================================================================
    
    async def _queue_gemini_enrichment(self, task: EnrichmentTask) -> None:
        """
        Queue track for Gemini cultural enrichment.
        
        Batching logic:
        - Wait for 50 tracks OR 8s timeout, whichever comes first
        - Reset timer when new track added
        - Flush batch immediately if 50 tracks reached
        """
        if not hasattr(self.engine, '_gemini') or not self.engine._gemini:
            return
        
        async with self._gemini_batch_lock:
            self._gemini_batch.append(task)
            self._gemini_last_add_time = time.time()
            
            # Check if batch is full
            if len(self._gemini_batch) >= GEMINI_BATCH_SIZE:
                await self._flush_gemini_batch()
            else:
                # Schedule flush after timeout (reset timer)
                if self._gemini_flush_task:
                    self._gemini_flush_task.cancel()
                self._gemini_flush_task = asyncio.create_task(
                    self._schedule_gemini_flush()
                )
    
    async def _schedule_gemini_flush(self) -> None:
        """Schedule Gemini batch flush after timeout."""
        try:
            await asyncio.sleep(GEMINI_BATCH_TIMEOUT_SECONDS)
            await self._flush_gemini_batch()
        except asyncio.CancelledError:
            pass
    
    async def _flush_gemini_batch(self) -> None:
        """Flush the Gemini batch queue."""
        async with self._gemini_batch_lock:
            if not self._gemini_batch:
                return
            
            batch = self._gemini_batch.copy()
            self._gemini_batch.clear()
        
        if not batch:
            return
        
        self._vlog(1, "📊 [Worker] Flushing Gemini batch: %d tracks", len(batch))
        
        # Queue all tracks for Gemini enrichment
        pending_futures: Dict[str, asyncio.Future] = {}
        
        for task in batch:
            try:
                future = await self.engine._gemini.queue_enrichment(
                    artist=task.artist,
                    title=task.title,
                    existing_tags=task.genres,
                    allow_grounding=self.engine._gemini.can_use_grounding(),
                )
                pending_futures[task.track_key] = future
            except Exception as e:
                self._vlog(2, "⚠️ [Worker] Gemini queue failed: %s", str(e)[:50])
        
        if not pending_futures:
            return
        
        # Trigger flush
        await self.engine._gemini.flush_enrichment_queue()
        
        # Collect results and store in cache
        for task in batch:
            try:
                future = pending_futures.get(task.track_key)
                if not future:
                    continue
                
                result = await asyncio.wait_for(future, timeout=10.0)
                if not result:
                    continue
                
                # Update cache entry with Gemini results
                existing = await self.engine._cache.get_enrichment(task.artist, task.title)
                if existing:
                    # Update cultural context
                    if result.get("tags"):
                        existing.tags = [str(t).lower() for t in result["tags"] if str(t).strip()]
                    if result.get("activity_affinity"):
                        existing.activity_affinity = result["activity_affinity"]
                    if result.get("daypart_affinity"):
                        existing.daypart_affinity = result["daypart_affinity"]
                    if result.get("emotional_intensity") is not None:
                        try:
                            existing.emotional_intensity = float(result["emotional_intensity"])
                        except (TypeError, ValueError):
                            pass
                    if result.get("mood"):
                        existing.mood = result["mood"]
                    
                    await self.engine._cache.set_enrichment(task.artist, task.title, existing)
                    self._stats["gemini_enriched"] += 1
                    
            except asyncio.TimeoutError:
                self._vlog(2, "⚠️ [Worker] Gemini timeout: %s", task.track_key)
            except Exception as e:
                self._vlog(2, "⚠️ [Worker] Gemini store failed: %s", str(e)[:50])
        
        self._vlog(1, "✅ [Worker] Gemini batch complete: %d tracks", len(batch))
    
    # =========================================================================
    # User Request API
    # =========================================================================
    
    async def enrich_for_user(
        self,
        artist: str,
        title: str,
        timeout: float = 30.0,
    ) -> bool:
        """
        Enrich a track for user request (highest priority, blocks until complete).
        
        Args:
            artist: Artist name
            title: Track title
            timeout: Maximum wait time in seconds
        
        Returns:
            True if enrichment completed successfully
        """
        # Add with highest priority
        await self.add_task(artist, title, Priority.USER, source="user")
        
        # Wait for completion
        start = time.time()
        while time.time() - start < timeout:
            # Check if enriched
            existing = await self.engine._cache.get_enrichment(artist, title)
            if existing and existing.analysis_verified:
                return True
            
            await asyncio.sleep(0.5)
        
        return False
    
    async def enrich_batch_for_buffer(
        self,
        tracks: List[Tuple[str, str]],
        max_wait: float = 60.0,
    ) -> int:
        """
        Enrich tracks for autoplay buffer (high priority, partial completion ok).
        
        Args:
            tracks: List of (artist, title) tuples
            max_wait: Maximum wait time in seconds
        
        Returns:
            Number of tracks successfully enriched
        """
        # Add all with buffer priority
        await self.add_batch(tracks, Priority.BUFFER, source="buffer")
        
        # Wait for completion
        enriched = 0
        start = time.time()
        
        while time.time() - start < max_wait and enriched < len(tracks):
            enriched = 0
            for artist, title in tracks:
                existing = await self.engine._cache.get_enrichment(artist, title)
                if existing and existing.analysis_verified:
                    enriched += 1
            
            if enriched >= len(tracks):
                break
            
            await asyncio.sleep(1)
        
        return enriched
