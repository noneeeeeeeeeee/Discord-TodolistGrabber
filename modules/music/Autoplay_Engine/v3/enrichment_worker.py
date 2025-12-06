"""
Enrichment Worker - Factory Pipeline for V3 Autoplay Engine.

This module implements the factory worker pattern for processing tracks:
1. Track Queue → Deezer Resolution (search + track details for BPM/gain)
2. Deezer → Gemini Cultural Enrichment (batched, tags/mood/activity)
3. Gemini → Audio Analysis (Librosa + EfficientAT for embeddings)

The pipeline order is: Deezer → Gemini → Audio Analysis → Done
This ensures cultural context is available early for recommendations.

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
    deezer_bpm: Optional[float] = None
    deezer_gain: Optional[float] = None  # Loudness in dB
    
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
            "deezer_gain": self.deezer_gain,
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
            deezer_gain=data.get("deezer_gain"),
            stage=data.get("stage", 0),
            error=data.get("error"),
        )


class EnrichmentWorker:
    """
    Factory worker that processes tracks through the enrichment pipeline.
    
    Pipeline Stages (per v3_reimplementation.md):
    1. Deezer Resolution: Search Deezer → Get track details (BPM, gain, preview URL)
    2. Gemini Cultural: Batch queue for tags, mood, activity (waits for 50 or 8s timeout)
    3. Audio Analysis: Librosa (BPM, key) + EfficientAT (embeddings)
    
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
        # Each entry is a tuple of (EnrichmentTask, asyncio.Future) where the future
        # is resolved when the Gemini result is stored in cache
        self._gemini_batch: List[Tuple[EnrichmentTask, asyncio.Future]] = []
        self._gemini_batch_lock = asyncio.Lock()
        self._gemini_last_add_time: float = 0.0
        self._gemini_flush_task: Optional[asyncio.Task] = None
        self._gemini_timer_active: bool = False  # Tracks if timer is running
        
        # Failed tracks for retry
        self._gemini_retry_queue: List[EnrichmentTask] = []
        self._gemini_max_retries: int = 2
        
        # Shared Deezer client for batch processing (initialized in start())
        self._deezer_client: Optional[DeezerClient] = None
        
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
        """
        Load queue from disk for persistence across restarts.
        
        On fresh starts (Scenario A first run), we clear stale tasks to allow
        re-queuing. Tasks are considered stale if they've been pending for too long.
        """
        if self._queue_file.exists():
            try:
                with open(self._queue_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tasks = data.get("tasks", [])
                    saved_at = data.get("saved_at", 0)
                    
                    # Check if queue is stale (more than 1 hour old)
                    # On fresh starts, we want to allow re-queuing tracks
                    is_stale = (time.time() - saved_at) > 3600  # 1 hour
                    
                    if is_stale:
                        self._vlog(1, "🧹 [Worker] Clearing stale queue (%d tasks from %.1f hours ago)", 
                                  len(tasks), (time.time() - saved_at) / 3600)
                        # Don't load stale tasks - allow fresh queueing
                        self._queue_file.unlink()  # Delete stale file
                        return
                    
                    # Load recent tasks
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
        
        # Initialize shared Deezer client for efficient batch processing
        self._deezer_client = DeezerClient(max_concurrent=50, timeout=10.0)
        await self._deezer_client.__aenter__()
        
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
        
        # Close shared Deezer client
        if self._deezer_client:
            await self._deezer_client.__aexit__(None, None, None)
            self._deezer_client = None
        
        self._save_queue()
        self._vlog(1, "🏭 [Worker] Stopped enrichment worker")
    
    # =========================================================================
    # Main Worker Loop
    # =========================================================================
    
    async def _worker_loop(self) -> None:
        """
        Main worker loop - processes tasks from queue.
        
        To enable proper Gemini batching (50 tracks per batch), we process
        multiple tasks concurrently through Deezer resolution, then batch
        them for Gemini, then queue audio analysis.
        
        Batch Processing Flow:
        1. Grab up to GEMINI_BATCH_SIZE (50) tasks from queue
        2. Process all through Deezer resolution concurrently
        3. Batch-queue all for Gemini enrichment (single API call)
        4. Queue all for audio analysis
        5. Mark complete
        """
        while self._is_running:
            try:
                # Get a batch of tasks (up to GEMINI_BATCH_SIZE for efficient batching)
                tasks = await self._get_task_batch(max_size=GEMINI_BATCH_SIZE)
                if not tasks:
                    await asyncio.sleep(1)  # Idle wait
                    continue
                
                # Process batch through pipeline
                await self._process_task_batch(tasks)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                LOG.error("❌ [Worker] Loop error: %s", str(e)[:100])
                await asyncio.sleep(5)
    
    async def _get_task_batch(self, max_size: int = 50) -> List[EnrichmentTask]:
        """Get a batch of tasks from queue (priority-ordered)."""
        async with self._queue_lock:
            if not self._queue:
                return []
            
            # Get up to max_size tasks, but don't mix priorities
            # (process high-priority tasks separately for responsiveness)
            batch = []
            first_priority = self._queue[0].priority if self._queue else None
            
            for task in self._queue[:max_size]:
                # Only batch same-priority tasks (or allow P2+P3 together)
                if first_priority == Priority.USER:
                    # USER priority gets processed immediately (no batching)
                    batch = [self._queue[0]]
                    break
                elif task.priority <= Priority.BUFFER:
                    batch.append(task)
                elif task.priority == Priority.DAYDREAM and first_priority >= Priority.BUFFER:
                    batch.append(task)
            
            # Remove batch from queue
            for task in batch:
                if task in self._queue:
                    self._queue.remove(task)
            
            return batch
    
    async def _process_task_batch(self, tasks: List[EnrichmentTask]) -> None:
        """
        Process a batch of tasks through the full pipeline concurrently.
        
        This enables proper Gemini batching by processing multiple tracks
        through Deezer first, then sending them all to Gemini at once.
        """
        if not tasks:
            return
        
        self._vlog(1, "🏭 [Worker] Processing batch: %d tasks", len(tasks))
        
        # Stage 1: Deezer Resolution - process all concurrently
        deezer_tasks = [
            self._stage_deezer_resolution(task) 
            for task in tasks if task.stage < 1
        ]
        if deezer_tasks:
            results = await asyncio.gather(*deezer_tasks, return_exceptions=True)
            
            # Update stage and filter failed
            successful_tasks = []
            for task, result in zip(tasks, results):
                if isinstance(result, Exception):
                    task.error = str(result)[:100]
                    await self._complete_task(task, success=False)
                elif result:
                    task.stage = 1
                    successful_tasks.append(task)
                else:
                    await self._complete_task(task, success=False)
            
            tasks = successful_tasks
        
        if not tasks:
            return
        
        # Stage 2: Gemini Cultural Enrichment - batch all at once
        gemini_tasks = [task for task in tasks if task.stage < 2]
        if gemini_tasks:
            await self._stage_gemini_batch(gemini_tasks)
            for task in gemini_tasks:
                task.stage = 2
        
        # Stage 3: Audio Analysis - queue all for background processing
        for task in tasks:
            if task.stage < 3:
                success = await self._stage_audio_analysis(task)
                if success:
                    task.stage = 3
                    await self._complete_task(task, success=True)
                    self._vlog(2, "✅ [Worker] Complete: %s - %s", task.artist, task.title)
                else:
                    await self._complete_task(task, success=False)
    
    async def _get_next_task(self) -> Optional[EnrichmentTask]:
        """Get the next task from queue (priority-ordered). Used for USER priority."""
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
    
    async def _stage_gemini_batch(self, tasks: List[EnrichmentTask]) -> None:
        """
        Process multiple tasks through Gemini cultural enrichment in a single batch.
        
        This is the efficient path - processes up to 50 tracks in a single API call.
        Used by _process_task_batch() for bulk processing.
        
        Skips tracks that already have Gemini enrichment (tags populated) to avoid
        duplicate API calls when __init__.py has already called Gemini directly.
        
        Args:
            tasks: List of tasks that have completed Deezer resolution
        """
        if not hasattr(self.engine, '_gemini') or not self.engine._gemini:
            self._vlog(2, "⚠️ [Worker] Gemini service unavailable, skipping cultural enrichment")
            return
        
        if not tasks:
            return
        
        # Filter out tasks that already have Gemini enrichment (tags populated)
        tasks_needing_gemini = []
        for task in tasks:
            existing = await self.engine._cache.get_enrichment(task.artist, task.title)
            if existing and existing.tags:
                # Already has Gemini enrichment, skip
                self._vlog(2, "⏭️ [Gemini] Skipping (already enriched): %s - %s", task.artist, task.title)
                continue
            tasks_needing_gemini.append(task)
        
        if not tasks_needing_gemini:
            self._vlog(1, "⏭️ [Gemini] All %d tracks already enriched, skipping batch", len(tasks))
            return
        
        self._vlog(1, "📊 [Gemini] Processing batch: %d tracks (%d skipped as already enriched)", 
                   len(tasks_needing_gemini), len(tasks) - len(tasks_needing_gemini))
        
        # Queue all tracks for Gemini enrichment
        pending_futures: Dict[str, asyncio.Future] = {}
        task_map: Dict[str, EnrichmentTask] = {}
        
        for task in tasks_needing_gemini:
            try:
                gemini_future = await self.engine._gemini.queue_enrichment(
                    artist=task.artist,
                    title=task.title,
                    existing_tags=task.genres,
                    allow_grounding=self.engine._gemini.can_use_grounding(),
                )
                pending_futures[task.track_key] = gemini_future
                task_map[task.track_key] = task
            except Exception as e:
                self._vlog(2, "⚠️ [Gemini] Queue failed: %s - %s", task.artist, str(e)[:50])
        
        if not pending_futures:
            return
        
        # Trigger flush on Gemini service (sends the batch)
        await self.engine._gemini.flush_enrichment_queue()
        
        # Collect results and store in cache
        success_count = 0
        
        for track_key, gemini_future in pending_futures.items():
            task = task_map.get(track_key)
            if not task:
                continue
            
            try:
                result = await asyncio.wait_for(gemini_future, timeout=15.0)
                if not result:
                    continue
                
                # Update cache entry with Gemini results
                existing = await self.engine._cache.get_enrichment(task.artist, task.title)
                if not existing:
                    existing = EnrichmentEntry(
                        tags=[],
                        mood=None,
                        fetched_at=time.time(),
                        genres=task.genres,
                        bpm=int(task.deezer_bpm) if task.deezer_bpm else None,
                        deezer_gain=task.deezer_gain,
                    )
                
                # Update with Gemini cultural context
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
                success_count += 1
                
            except asyncio.TimeoutError:
                self._vlog(2, "⚠️ [Gemini] Timeout: %s", track_key)
            except Exception as e:
                self._vlog(2, "⚠️ [Gemini] Store failed: %s - %s", track_key, str(e)[:50])
        
        self._vlog(1, "✅ [Gemini] Batch complete: %d/%d succeeded", success_count, len(tasks_needing_gemini))
    
    # =========================================================================
    # Pipeline Stages
    # =========================================================================
    
    async def _stage_deezer_resolution(self, task: EnrichmentTask) -> bool:
        """
        Stage 1: Resolve track on Deezer and create initial cache entries.
        
        Uses waterfall search strategy:
        1. Exact search: "{artist} {title}"
        2. Fuzzy search: Title only, then filter by artist similarity
        3. Artist-only search: Get top tracks by artist, match title
        
        Gets:
        - Deezer track ID
        - Preview URL (30s HQ audio)
        - BPM (from track details API)
        - Gain/loudness (from track details API)
        - Album, duration, explicit flag
        
        Creates:
        - EnrichmentEntry with Deezer metadata (bpm, gain, genres)
        - MappingEntry with preview URL for audio analysis
        """
        # Use shared client for efficient batch processing
        client = self._deezer_client
        if not client:
            # Fallback: create a temporary client if shared one isn't available
            async with DeezerClient(max_concurrent=50, timeout=10.0) as temp_client:
                return await self._do_deezer_resolution(temp_client, task)
        
        return await self._do_deezer_resolution(client, task)
    
    async def _do_deezer_resolution(self, client: DeezerClient, task: EnrichmentTask) -> bool:
        """Internal helper that performs Deezer resolution with the given client."""
        result = None
        
        # Strategy 1: Exact search "{artist} {title}"
        query = f"{task.artist} {task.title}"
        results = await client.search_track(query)
        
        if results:
            result = results[0]
            self._vlog(2, "✅ [Deezer] Exact match: %s - %s", task.artist, task.title)
        else:
            # Strategy 2: Fuzzy search - title only, filter by artist
            self._vlog(2, "🔍 [Deezer] Trying fuzzy search for: %s - %s", task.artist, task.title)
            result = await self._fuzzy_deezer_search(client, task.artist, task.title)
        
        if not result:
            # Strategy 3: Search artist's top tracks
            self._vlog(2, "🔍 [Deezer] Trying artist top tracks for: %s - %s", task.artist, task.title)
            result = await self._search_artist_top_tracks(client, task.artist, task.title)
        
        if not result:
            self._vlog(2, "⚠️ [Worker] Deezer not found (all strategies): %s - %s", task.artist, task.title)
            return False
        
        task.deezer_id = int(result.id) if result.id else None
        task.preview_url = result.preview_url
        task.genres = result.genres if hasattr(result, 'genres') and result.genres else []
        
        # Fetch detailed metadata (BPM, gain) from track details API
        if task.deezer_id:
            details = await client.get_track_details(str(task.deezer_id))
            if details:
                task.deezer_bpm = details.get("bpm")
                task.deezer_gain = details.get("gain")
        
        # Create initial cache entries with Deezer data
        # This ensures entries exist before Gemini enrichment runs
        await self._create_initial_cache_entries(task)
        
        self._stats["deezer_resolved"] += 1
        self._vlog(2, "✅ [Worker] Deezer resolved: %s (preview: %s, bpm: %s, gain: %s)", 
                  task.track_key, "yes" if task.preview_url else "no",
                  task.deezer_bpm if task.deezer_bpm is not None else "N/A", 
                  f"{task.deezer_gain}dB" if task.deezer_gain is not None else "N/A")
        
        return task.preview_url is not None
    
    async def _fuzzy_deezer_search(self, client: DeezerClient, artist: str, title: str):
        """
        Fuzzy search: Search by title only, then filter results by artist similarity.
        
        This handles cases where:
        - Artist name has special characters that break search
        - Title is unique enough to find the track
        - Remasters or different versions exist
        """
        try:
            # Search by title only
            results = await client.search_track(title)
            if not results:
                return None
            
            # Normalize artist for comparison
            artist_lower = artist.lower().strip()
            artist_words = set(artist_lower.split())
            
            # Score each result by artist similarity
            best_match = None
            best_score = 0.0
            
            for result in results[:10]:  # Check top 10 results
                result_artist = getattr(result, 'artist', '').lower().strip()
                result_artist_words = set(result_artist.split())
                
                # Calculate Jaccard similarity
                intersection = len(artist_words & result_artist_words)
                union = len(artist_words | result_artist_words)
                score = intersection / union if union > 0 else 0
                
                # Boost if artist is substring
                if artist_lower in result_artist or result_artist in artist_lower:
                    score += 0.5
                
                if score > best_score and score >= 0.3:  # Minimum threshold
                    best_score = score
                    best_match = result
            
            return best_match
            
        except Exception as e:
            self._vlog(2, "⚠️ [Deezer] Fuzzy search error: %s", str(e)[:50])
            return None
    
    async def _search_artist_top_tracks(self, client: DeezerClient, artist: str, title: str):
        """
        Search artist's top tracks to find the target title.
        
        This handles cases where:
        - Track title has special characters
        - Track is a deep cut that doesn't appear in general search
        """
        try:
            # Search for artist
            artist_results = await client.search_artist(artist)
            if not artist_results:
                return None
            
            # Get artist's top tracks
            artist_id = artist_results[0].id if artist_results else None
            if not artist_id:
                return None
            
            top_tracks = await client.get_artist_top_tracks(str(artist_id), limit=50)
            if not top_tracks:
                return None
            
            # Normalize title for comparison
            title_lower = title.lower().strip()
            title_words = set(title_lower.split())
            
            # Find best matching track
            best_match = None
            best_score = 0.0
            
            for track in top_tracks:
                track_title = getattr(track, 'title', '').lower().strip()
                track_title_words = set(track_title.split())
                
                # Calculate Jaccard similarity
                intersection = len(title_words & track_title_words)
                union = len(title_words | track_title_words)
                score = intersection / union if union > 0 else 0
                
                # Boost if title is substring
                if title_lower in track_title or track_title in title_lower:
                    score += 0.5
                
                if score > best_score and score >= 0.4:  # Higher threshold for this method
                    best_score = score
                    best_match = track
            
            return best_match
            
        except Exception as e:
            self._vlog(2, "⚠️ [Deezer] Artist top tracks error: %s", str(e)[:50])
            return None
    
    async def _create_initial_cache_entries(self, task: EnrichmentTask) -> None:
        """
        Create initial cache entries after Deezer resolution.
        
        This ensures entries exist in cache before Gemini enrichment runs.
        Gemini and Audio stages will update these entries with their data.
        """
        try:
            # Create enrichment entry with Deezer metadata
            existing = await self.engine._cache.get_enrichment(task.artist, task.title)
            if existing:
                # Update existing entry with Deezer data
                existing.genres = task.genres
                existing.bpm = int(task.deezer_bpm) if task.deezer_bpm else None
                existing.deezer_gain = task.deezer_gain
                await self.engine._cache.set_enrichment(task.artist, task.title, existing)
            else:
                # Create new entry
                entry = EnrichmentEntry(
                    tags=[],
                    mood=None,
                    fetched_at=time.time(),
                    genres=task.genres,
                    bpm=int(task.deezer_bpm) if task.deezer_bpm else None,
                    deezer_gain=task.deezer_gain,
                )
                await self.engine._cache.set_enrichment(task.artist, task.title, entry)
            
            # Create mapping entry with preview URL
            mapping = MappingEntry(
                youtube_id="deezer_preview",
                url="",
                timestamp=time.time(),
                preview_url=task.preview_url,
                preview_duration_ms=30000,  # Deezer previews are 30s
                preview_fetched_at=time.time(),
                deezer_track_id=str(task.deezer_id) if task.deezer_id else None,
                ingest_source="enrichment_worker",
            )
            await self.engine._cache.set_mapping(task.artist, task.title, mapping)
            
        except Exception as e:
            LOG.warning("⚠️ [Worker] Failed to create cache entries: %s", str(e)[:100])
    
    async def _stage_audio_analysis(self, task: EnrichmentTask) -> bool:
        """
        Stage 3: Queue audio for analysis using Librosa + EfficientAT.
        
        This stage queues the track for the engine's audio analysis pipeline.
        The actual analysis runs asynchronously and sets analysis_verified=True
        when complete.
        
        Gets (via engine analysis worker):
        - BPM, key, mode, loudness (Librosa)
        - 512D-2048D embedding (EfficientAT MobileNet)
        - 5D simple_vibe vector (non-ML fallback)
        """
        if not task.preview_url:
            return False
        
        try:
            track_key = self.engine._make_track_key(task.artist, task.title)
            
            # Map priority to engine's priority system
            engine_priority = PRIORITY_DAYDREAM  # Default to lowest
            if task.priority == Priority.USER:
                engine_priority = 1  # PRIORITY_ACTIVE
            elif task.priority == Priority.BUFFER:
                engine_priority = 2  # PRIORITY_BUFFER
            
            # Queue for EfficientAT analysis (uses engine's worker system)
            success = self.engine.queue_analysis(
                track_key,
                "deezer_preview",  # Placeholder URL - actual preview is in mapping
                priority=engine_priority,
            )
            
            if success:
                self._stats["audio_analyzed"] += 1
            
            return success
            
        except Exception as e:
            LOG.warning("⚠️ [Worker] Audio analysis queue failed: %s", str(e)[:100])
            return False
    
    # =========================================================================
    # Gemini Batch Queue Cleanup (Legacy - used during stop())
    # =========================================================================
    
    async def _flush_gemini_batch(self) -> None:
        """
        Flush the Gemini batch queue.
        
        Processes all queued tracks, stores results in cache, and resolves
        the waiting futures so tasks can continue to the next stage.
        Failed tracks are added to retry queue.
        """
        async with self._gemini_batch_lock:
            if not self._gemini_batch:
                return
            
            batch = self._gemini_batch.copy()
            self._gemini_batch.clear()
            self._gemini_timer_active = False
        
        if not batch:
            return
        
        self._vlog(1, "📊 [Gemini] Flushing batch: %d tracks", len(batch))
        
        # Extract tasks and their result futures
        # batch is list of (EnrichmentTask, asyncio.Future) tuples
        task_futures: Dict[str, Tuple[EnrichmentTask, asyncio.Future]] = {}
        pending_gemini_futures: Dict[str, asyncio.Future] = {}
        
        for task, result_future in batch:
            try:
                gemini_future = await self.engine._gemini.queue_enrichment(
                    artist=task.artist,
                    title=task.title,
                    existing_tags=task.genres,
                    allow_grounding=self.engine._gemini.can_use_grounding(),
                )
                pending_gemini_futures[task.track_key] = gemini_future
                task_futures[task.track_key] = (task, result_future)
            except Exception as e:
                self._vlog(2, "⚠️ [Gemini] Queue failed: %s - %s", task.artist, str(e)[:50])
                # Resolve the future with failure so task can continue
                if not result_future.done():
                    result_future.set_result(False)
                self._add_to_retry_queue(task)
        
        if not pending_gemini_futures:
            return
        
        # Trigger flush on Gemini service
        await self.engine._gemini.flush_enrichment_queue()
        
        # Collect results and store in cache
        success_count = 0
        
        for track_key, (task, result_future) in task_futures.items():
            try:
                gemini_future = pending_gemini_futures.get(track_key)
                if not gemini_future:
                    if not result_future.done():
                        result_future.set_result(False)
                    continue
                
                result = await asyncio.wait_for(gemini_future, timeout=10.0)
                if not result:
                    if not result_future.done():
                        result_future.set_result(False)
                    self._add_to_retry_queue(task)
                    continue
                
                # Update cache entry with Gemini results
                existing = await self.engine._cache.get_enrichment(task.artist, task.title)
                if not existing:
                    # Create new entry if it doesn't exist (fallback)
                    existing = EnrichmentEntry(
                        tags=[],
                        mood=None,
                        fetched_at=time.time(),
                        genres=task.genres,
                        bpm=int(task.deezer_bpm) if task.deezer_bpm else None,
                        deezer_gain=task.deezer_gain,
                    )
                
                # Update with Gemini cultural context
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
                success_count += 1
                
                # Resolve the result future so the task can continue
                if not result_future.done():
                    result_future.set_result(True)
                    
            except asyncio.TimeoutError:
                self._vlog(2, "⚠️ [Gemini] Timeout: %s", track_key)
                if not result_future.done():
                    result_future.set_result(False)
                self._add_to_retry_queue(task)
            except Exception as e:
                self._vlog(2, "⚠️ [Gemini] Store failed: %s - %s", track_key, str(e)[:50])
                if not result_future.done():
                    result_future.set_result(False)
                self._add_to_retry_queue(task)
        
        # Count failed (those in retry queue from this batch)
        failed_count = len(task_futures) - success_count
        
        self._vlog(1, "✅ [Gemini] Batch complete: %d/%d succeeded, %d failed (will retry)", 
                  success_count, len(batch), failed_count)
        
        # Process retry queue if we have pending retries
        if self._gemini_retry_queue:
            asyncio.create_task(self._process_retry_queue())
    
    def _add_to_retry_queue(self, task: EnrichmentTask) -> None:
        """Add a failed task to the retry queue."""
        # Check retry count (stored in error field as counter)
        retry_count = 0
        if task.error and task.error.startswith("retry:"):
            try:
                retry_count = int(task.error.split(":")[1])
            except (ValueError, IndexError):
                pass
        
        if retry_count < self._gemini_max_retries:
            task.error = f"retry:{retry_count + 1}"
            self._gemini_retry_queue.append(task)
            self._vlog(2, "🔄 [Gemini] Added to retry queue: %s (attempt %d/%d)", 
                      task.track_key, retry_count + 1, self._gemini_max_retries)
    
    async def _process_retry_queue(self) -> None:
        """Process the Gemini retry queue."""
        if not self._gemini_retry_queue:
            return
        
        # Wait a bit before retrying
        await asyncio.sleep(5.0)
        
        async with self._gemini_batch_lock:
            retry_batch = self._gemini_retry_queue[:GEMINI_BATCH_SIZE]
            self._gemini_retry_queue = self._gemini_retry_queue[GEMINI_BATCH_SIZE:]
        
        if retry_batch:
            self._vlog(1, "🔄 [Gemini] Processing retry batch: %d tracks", len(retry_batch))
            
            # Re-queue for processing with new futures
            for task in retry_batch:
                # Create a new future for this retry (we don't need to await it)
                retry_future: asyncio.Future = asyncio.get_event_loop().create_future()
                self._gemini_batch.append((task, retry_future))
            
            await self._flush_gemini_batch()
    
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
        
        Uses event-based waiting instead of polling for faster response.
        
        Args:
            tracks: List of (artist, title) tuples
            max_wait: Maximum wait time in seconds
        
        Returns:
            Number of tracks successfully enriched
        """
        if not tracks:
            return 0
        
        # Add all with buffer priority
        await self.add_batch(tracks, Priority.BUFFER, source="buffer")
        
        # Use event-based waiting via CacheManager
        enriched = await self.engine._cache.wait_for_enrichments(tracks, timeout=max_wait)
        
        return enriched
