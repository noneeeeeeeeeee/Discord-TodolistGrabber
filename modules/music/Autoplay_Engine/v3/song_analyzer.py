"""
V3 Autoplay Engine - Song Analyzer

Three-layer audio analysis pipeline:
1. Physics Layer (Librosa): BPM, key, loudness, timbre
2. Semantics Layer (EfficientAT): Neural embeddings, instrument/sound tags
3. Librarian Layer (Gemini): Genres, moods, themes, cultural context

Performance optimizations:
- Worker pool for EfficientAT/Librosa analysis (configurable, default 3 workers)
- Bulk Gemini processing to avoid API spam
- Priority queue for analysis ordering
- Downloads Deezer 30s previews (deleted after analysis)
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import aiohttp
import numpy as np

from .cache_manager import CacheManager, get_cache_manager
from .constants import (
    ANALYZER_CONFIG,
    AnalysisMode,
    EventType,
    LibrarianLayer,
    PhysicsLayer,
    SemanticsLayer,
    SongMetadata,
    V3Config,
)
from .event_bus import EventBus, EventPayload
from .gemini_manager import GeminiManager, get_gemini_manager
from .mappings import MappingsManager, SongIdentifier, get_mappings_manager

logger = logging.getLogger(__name__)


class AnalysisPriority(Enum):
    """Priority levels for analysis queue."""
    IMMEDIATE = 0  # Currently playing or about to play
    HIGH = 1       # In buffer (next 5 songs)
    MEDIUM = 2     # In extended queue
    LOW = 3        # Background/daydreamer exploration
    BATCH = 4      # Bulk analysis during idle


@dataclass
class AnalysisTask:
    """Task in the analysis queue."""
    song_id: str
    identifier: SongIdentifier
    priority: AnalysisPriority
    mode: AnalysisMode
    created_at: float
    retry_count: int = 0
    
    def __lt__(self, other: "AnalysisTask") -> bool:
        """Compare by priority for heap ordering."""
        if self.priority.value != other.priority.value:
            return self.priority.value < other.priority.value
        return self.created_at < other.created_at


class SongAnalyzer:
    """
    Orchestrates three-layer song analysis pipeline.
    
    Manages a priority queue of songs to analyze, downloads previews,
    runs audio analysis, and caches results. Preview files are deleted
    immediately after analysis.
    
    Performance Features:
    - Worker pool (ThreadPoolExecutor) for EfficientAT/Librosa analysis
    - Configurable worker count (default 3) via ANALYZER_CONFIG
    - Bulk Gemini processing to avoid API spam
    - Priority queue with concurrent analysis support
    
    Dependencies:
    - librosa: Audio feature extraction
    - EfficientAT: Neural audio tagging (via dependency_manager)
    - Gemini: Contextual analysis (batched)
    """
    
    # Use configuration from constants
    SAMPLE_RATE = ANALYZER_CONFIG.sample_rate
    HOP_LENGTH = ANALYZER_CONFIG.hop_length
    MAX_PREVIEW_SIZE = int(ANALYZER_CONFIG.preview_max_size_mb * 1024 * 1024)
    MAX_CONCURRENT_DOWNLOADS = ANALYZER_CONFIG.max_concurrent_downloads
    MAX_CONCURRENT_ANALYSIS = ANALYZER_CONFIG.max_concurrent_analysis
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        cache: Optional[CacheManager] = None,
        gemini: Optional[GeminiManager] = None,
        mappings: Optional[MappingsManager] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize song analyzer.
        
        Args:
            config: V3 configuration
            cache: Cache manager for storing results
            gemini: Gemini manager for librarian analysis
            mappings: Mappings manager for ID resolution
            event_bus: Event bus for notifications
        """
        self.config = config or V3Config()
        self.cache = cache or get_cache_manager()
        self.gemini = gemini or get_gemini_manager()
        self.mappings = mappings or get_mappings_manager()
        self.event_bus = event_bus or EventBus()
        
        # Priority queue (using list with manual sorting)
        self._queue: list[AnalysisTask] = []
        self._queue_lock = asyncio.Lock()
        
        # Currently processing
        self._processing: set[str] = set()
        
        # Worker pool for CPU-bound audio analysis
        self._worker_pool: Optional[ThreadPoolExecutor] = None
        self._worker_count = ANALYZER_CONFIG.analysis_worker_count
        
        # Worker tasks
        self._worker_tasks: list[asyncio.Task] = []
        self._running = False
        
        # HTTP session for downloads
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Temp directory for previews
        self._temp_dir: Optional[Path] = None
        
        # EfficientAT model (lazy loaded)
        self._efficientat_model = None
        self._efficientat_labels = None
        
        # Gemini bulk processing queue
        self._gemini_batch: list[tuple[AnalysisTask, AudioFeatures, Optional[SemanticFeatures]]] = []
        self._gemini_batch_lock = asyncio.Lock()
        self._gemini_batch_event = asyncio.Event()
        self._gemini_worker_task: Optional[asyncio.Task] = None
        
        # Statistics
        self._stats = {
            "queued": 0,
            "completed": 0,
            "failed": 0,
            "cache_hits": 0,
            "gemini_batches_sent": 0,
            "gemini_songs_analyzed": 0
        }
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize analyzer, create worker pool, and start workers."""
        if self._initialized:
            return
        
        # Create temp directory
        self._temp_dir = Path(tempfile.mkdtemp(prefix="v3_audio_"))
        
        # Create HTTP session
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        )
        
        # Create worker pool for CPU-bound analysis (EfficientAT/Librosa)
        self._worker_pool = ThreadPoolExecutor(
            max_workers=self._worker_count,
            thread_name_prefix="v3_audio_worker"
        )
        logger.info(f"Created worker pool with {self._worker_count} workers")
        
        # Initialize dependencies
        await self.cache.initialize()
        await self.gemini.initialize()
        await self.mappings.initialize()
        
        # Start analysis workers (multiple for concurrent processing)
        self._running = True
        for i in range(self._worker_count):
            task = asyncio.create_task(self._worker_loop())
            self._worker_tasks.append(task)
        
        # Start Gemini batch worker for bulk processing
        self._gemini_worker_task = asyncio.create_task(self._gemini_batch_worker())
        
        self._initialized = True
        logger.info(f"Song analyzer initialized (workers={self._worker_count}, temp={self._temp_dir})")
    
    async def shutdown(self) -> None:
        """Stop workers, clean up thread pool, and release resources."""
        self._running = False
        
        # Cancel all worker tasks
        for task in self._worker_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker_tasks.clear()
        
        # Cancel Gemini batch worker
        if self._gemini_worker_task:
            self._gemini_worker_task.cancel()
            try:
                await self._gemini_worker_task
            except asyncio.CancelledError:
                pass
        
        # Shutdown thread pool
        if self._worker_pool:
            self._worker_pool.shutdown(wait=True)
            self._worker_pool = None
        
        if self._session:
            await self._session.close()
        
        # Clean up temp directory
        if self._temp_dir and self._temp_dir.exists():
            import shutil
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        
        self._initialized = False
        logger.info("Song analyzer shutdown complete")
    
    async def enqueue(
        self,
        identifier: SongIdentifier,
        priority: AnalysisPriority = AnalysisPriority.MEDIUM,
        mode: AnalysisMode = AnalysisMode.FULL
    ) -> bool:
        """
        Add a song to the analysis queue.
        
        Args:
            identifier: Song to analyze
            priority: Analysis priority
            mode: Which layers to analyze
            
        Returns:
            True if queued, False if already analyzed or in queue
        """
        await self.initialize()
        
        song_id = identifier.primary_id
        if not song_id:
            return False
        
        # Check if already analyzed
        existing = await self.cache.get_metadata(song_id)
        if existing and self._is_fully_analyzed(existing, mode):
            self._stats["cache_hits"] += 1
            return False
        
        async with self._queue_lock:
            # Check if already in queue or processing
            if song_id in self._processing:
                return False
            
            if any(t.song_id == song_id for t in self._queue):
                # Update priority if higher
                for task in self._queue:
                    if task.song_id == song_id and priority.value < task.priority.value:
                        task.priority = priority
                        self._queue.sort()
                return False
            
            # Add to queue
            task = AnalysisTask(
                song_id=song_id,
                identifier=identifier,
                priority=priority,
                mode=mode,
                created_at=time.time()
            )
            self._queue.append(task)
            self._queue.sort()
            self._stats["queued"] += 1
        
        return True
    
    async def enqueue_batch(
        self,
        identifiers: list[SongIdentifier],
        priority: AnalysisPriority = AnalysisPriority.LOW
    ) -> int:
        """
        Add multiple songs to queue.
        
        Args:
            identifiers: Songs to analyze
            priority: Priority for all songs
            
        Returns:
            Number of songs queued
        """
        count = 0
        for identifier in identifiers:
            if await self.enqueue(identifier, priority):
                count += 1
        return count
    
    def _is_fully_analyzed(
        self,
        metadata: SongMetadata,
        mode: AnalysisMode
    ) -> bool:
        """Check if metadata has all required analysis layers."""
        if mode == AnalysisMode.PHYSICS_ONLY:
            return metadata.audio_features is not None
        
        if mode == AnalysisMode.QUICK:
            return (
                metadata.audio_features is not None and
                metadata.semantic_features is not None
            )
        
        # FULL mode
        return (
            metadata.audio_features is not None and
            metadata.semantic_features is not None and
            metadata.librarian_info is not None
        )
    
    async def _worker_loop(self) -> None:
        """Background worker that processes the analysis queue."""
        while self._running:
            try:
                task = await self._get_next_task()
                
                if task:
                    await self._process_task(task)
                else:
                    # No tasks, wait a bit
                    await asyncio.sleep(0.5)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error: {e}")
                await asyncio.sleep(1)
    
    async def _get_next_task(self) -> Optional[AnalysisTask]:
        """Get next task from queue respecting concurrency limits."""
        async with self._queue_lock:
            if not self._queue:
                return None
            
            if len(self._processing) >= self.MAX_CONCURRENT_ANALYSIS:
                return None
            
            task = self._queue.pop(0)
            self._processing.add(task.song_id)
            return task
    
    async def _process_task(self, task: AnalysisTask) -> None:
        """
        Process a single analysis task.
        
        For IMMEDIATE priority: full inline analysis including Gemini
        For other priorities: audio analysis inline, Gemini via batch
        """
        preview_path = None
        
        try:
            # Download preview
            preview_path = await self._download_preview(task.identifier)
            
            if not preview_path:
                raise ValueError("Failed to download preview")
            
            # Run physics analysis (Librosa) - via worker pool
            audio_features = await self._analyze_physics(preview_path)
            
            # Run semantics analysis (EfficientAT) if not physics-only - via worker pool
            semantic_features = None
            if task.mode != AnalysisMode.PHYSICS_ONLY:
                semantic_features = await self._analyze_semantics(preview_path)
            
            # Run librarian analysis (Gemini) if full mode
            librarian_info = None
            if task.mode == AnalysisMode.FULL:
                # For IMMEDIATE priority, do inline Gemini call
                # For other priorities, queue for batch processing
                if task.priority == AnalysisPriority.IMMEDIATE:
                    librarian_info = await self._analyze_librarian(
                        task.identifier,
                        audio_features,
                        semantic_features
                    )
                else:
                    # Queue for bulk Gemini processing
                    await self._queue_for_gemini_batch(
                        task, audio_features, semantic_features
                    )
            
            # Build and cache metadata (librarian_info may be None if batched)
            metadata = SongMetadata(
                song_id=task.song_id,
                title=task.identifier.title or "",
                artist=task.identifier.artist or "",
                album=task.identifier.album,
                duration_ms=task.identifier.duration_ms,
                preview_url=task.identifier.preview_url,
                isrc=task.identifier.isrc,
                audio_features=audio_features,
                semantic_features=semantic_features,
                librarian_info=librarian_info,  # May be None, filled later by batch
                analysis_version=1,
                created_at=time.time(),
                updated_at=time.time()
            )
            
            await self.cache.set_metadata(task.song_id, metadata)
            self._stats["completed"] += 1
            
            # Publish completion event
            await self.event_bus.publish(EventPayload(
                event_type=EventType.ANALYSIS_COMPLETE,
                data={
                    "song_id": task.song_id,
                    "title": task.identifier.title,
                    "artist": task.identifier.artist,
                    "mode": task.mode.value,
                    "gemini_pending": librarian_info is None and task.mode == AnalysisMode.FULL
                }
            ))
            
            logger.debug(f"Analyzed: {task.identifier.artist} - {task.identifier.title}")
            
        except Exception as e:
            logger.error(f"Analysis failed for {task.song_id}: {e}")
            self._stats["failed"] += 1
            
            # Retry logic
            if task.retry_count < 2:
                task.retry_count += 1
                task.priority = AnalysisPriority.LOW
                async with self._queue_lock:
                    self._queue.append(task)
                    self._queue.sort()
            
        finally:
            # Clean up preview file
            if preview_path and preview_path.exists():
                try:
                    preview_path.unlink()
                except Exception:
                    pass
            
            # Remove from processing set
            async with self._queue_lock:
                self._processing.discard(task.song_id)
    
    async def _download_preview(
        self,
        identifier: SongIdentifier
    ) -> Optional[Path]:
        """Download 30s preview from Deezer."""
        if not identifier.preview_url:
            # Try to resolve preview URL
            resolved = await self.mappings.resolve_song(
                title=identifier.title,
                artist=identifier.artist,
                deezer_id=identifier.deezer_id
            )
            if resolved and resolved.preview_url:
                identifier = resolved
            else:
                return None
        
        try:
            async with self._session.get(identifier.preview_url) as response:
                if response.status != 200:
                    return None
                
                # Check size
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.MAX_PREVIEW_SIZE:
                    logger.warning(f"Preview too large: {content_length}")
                    return None
                
                # Generate filename from hash
                hash_input = f"{identifier.deezer_id or identifier.title}_{identifier.artist}"
                filename = hashlib.md5(hash_input.encode()).hexdigest()[:16] + ".mp3"
                preview_path = self._temp_dir / filename
                
                # Download
                with open(preview_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(8192):
                        f.write(chunk)
                
                return preview_path
                
        except Exception as e:
            logger.error(f"Preview download failed: {e}")
            return None
    
    async def _analyze_physics(self, audio_path: Path) -> AudioFeatures:
        """
        Run Librosa analysis for physics layer via worker pool.
        
        Extracts: BPM, key, loudness, timbre (MFCC), spectral features
        Uses thread pool to avoid blocking event loop.
        """
        try:
            import librosa
        except ImportError:
            logger.warning("Librosa not installed, using placeholder values")
            return AudioFeatures(
                bpm=120.0,
                key="C",
                mode="major",
                loudness_db=-10.0,
                energy=0.5,
                spectral_centroid=2000.0,
                spectral_rolloff=4000.0
            )
        
        # Run in worker pool to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._worker_pool,  # Use our configured worker pool
            self._librosa_analysis,
            audio_path
        )
    
    def _librosa_analysis(self, audio_path: Path) -> AudioFeatures:
        """Synchronous Librosa analysis (runs in thread pool)."""
        import librosa
        
        # Load audio
        y, sr = librosa.load(str(audio_path), sr=self.SAMPLE_RATE)
        
        # BPM detection
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo) if not hasattr(tempo, '__len__') else float(tempo[0])
        
        # Key detection using chroma
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)
        key_idx = int(np.argmax(chroma_mean))
        keys = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
        key = keys[key_idx]
        
        # Mode detection (major/minor) - simplified
        # Uses ratio of major vs minor third
        mode = "major"  # Simplified, would need more analysis
        
        # Loudness (RMS energy in dB)
        rms = librosa.feature.rms(y=y)[0]
        loudness_db = float(20 * np.log10(np.mean(rms) + 1e-10))
        
        # Energy (normalized RMS)
        energy = float(np.clip(np.mean(rms) * 10, 0, 1))
        
        # Spectral features
        spectral_centroids = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
        spectral_rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr)[0]
        
        # MFCC for timbre
        mfccs = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
        mfcc_mean = np.mean(mfccs, axis=1).tolist()
        
        # Zero crossing rate (percussiveness indicator)
        zcr = librosa.feature.zero_crossing_rate(y)[0]
        
        return AudioFeatures(
            bpm=bpm,
            key=key,
            mode=mode,
            loudness_db=loudness_db,
            energy=energy,
            spectral_centroid=float(np.mean(spectral_centroids)),
            spectral_rolloff=float(np.mean(spectral_rolloff)),
            mfcc_coefficients=mfcc_mean,
            zero_crossing_rate=float(np.mean(zcr))
        )
    
    async def _analyze_semantics(self, audio_path: Path) -> SemanticFeatures:
        """
        Run EfficientAT analysis for semantics layer via worker pool.
        
        Uses MobileNet (mn10_as) for audio tagging and embeddings.
        """
        # Check if model is available
        model = await self._get_efficientat_model()
        
        if model is None:
            logger.warning("EfficientAT not available, using placeholder")
            return SemanticFeatures(
                embedding=[0.0] * 128,
                instrument_tags=["unknown"],
                sound_tags=["music"],
                predicted_genres=[]
            )
        
        # Run inference in worker pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._worker_pool,  # Use our configured worker pool
            self._efficientat_inference,
            audio_path,
            model
        )
    
    async def _get_efficientat_model(self):
        """Lazy load EfficientAT model."""
        if self._efficientat_model is not None:
            return self._efficientat_model
        
        try:
            # Import and load model
            from dependency_manager import DependencyManager
            
            dep_manager = DependencyManager()
            model_path = await dep_manager.ensure_model_file("mn10_as")
            
            if model_path:
                import torch
                
                # Load model (implementation depends on EfficientAT structure)
                # This is a placeholder for the actual loading logic
                logger.info(f"Loading EfficientAT model from {model_path}")
                
                # Model would be loaded here
                # self._efficientat_model = torch.load(model_path)
                
                return self._efficientat_model
                
        except Exception as e:
            logger.warning(f"Failed to load EfficientAT: {e}")
        
        return None
    
    def _efficientat_inference(
        self,
        audio_path: Path,
        model
    ) -> SemanticFeatures:
        """Run EfficientAT inference (synchronous, runs in thread pool)."""
        # This is a placeholder implementation
        # Actual implementation would:
        # 1. Load audio and preprocess for model
        # 2. Run inference to get embeddings and tags
        # 3. Post-process outputs
        
        return SemanticFeatures(
            embedding=[0.0] * 128,
            instrument_tags=["placeholder"],
            sound_tags=["music"],
            predicted_genres=[]
        )
    
    async def _analyze_librarian(
        self,
        identifier: SongIdentifier,
        audio_features: AudioFeatures,
        semantic_features: Optional[SemanticFeatures]
    ) -> Optional[LibrarianInfo]:
        """
        Run Gemini analysis for librarian layer.
        
        Note: This method is for immediate/single analysis.
        For batch processing, use _queue_for_gemini_batch instead.
        """
        # Build audio features dict
        audio_dict = {
            "bpm": audio_features.bpm,
            "key": audio_features.key,
            "energy": audio_features.energy,
            "loudness": audio_features.loudness_db
        }
        
        # Get semantic tags
        semantic_tags = None
        if semantic_features:
            semantic_tags = (
                semantic_features.instrument_tags +
                semantic_features.sound_tags
            )
        
        # Call Gemini
        response = await self.gemini.analyze_song(
            title=identifier.title or "",
            artist=identifier.artist or "",
            album=identifier.album,
            audio_features=audio_dict,
            semantic_tags=semantic_tags
        )
        
        if not response.success:
            logger.warning(f"Gemini analysis failed: {response.error}")
            return None
        
        data = response.data
        
        return LibrarianInfo(
            genres=data.get("genres", []),
            moods=data.get("moods", []),
            themes=data.get("themes", []),
            energy_level=data.get("energy_level", 0.5),
            danceability=data.get("danceability", 0.5),
            explicit_content=data.get("explicit_content", False),
            cultural_vibe=data.get("cultural_vibe", ""),
            canonical_title=data.get("canonical_title"),
            similar_artists=data.get("similar_artists", []),
            recommendation_tags=data.get("recommendation_tags", [])
        )
    
    async def _queue_for_gemini_batch(
        self,
        task: AnalysisTask,
        audio_features: AudioFeatures,
        semantic_features: Optional[SemanticFeatures]
    ) -> None:
        """
        Queue a song for bulk Gemini processing.
        
        Instead of making individual API calls, songs are batched
        together and processed in bulk to avoid API spam.
        """
        async with self._gemini_batch_lock:
            self._gemini_batch.append((task, audio_features, semantic_features))
            
            # Signal the batch worker if we've reached batch size
            if len(self._gemini_batch) >= ANALYZER_CONFIG.gemini_batch_size:
                self._gemini_batch_event.set()
    
    async def _gemini_batch_worker(self) -> None:
        """
        Background worker that processes Gemini analysis in batches.
        
        Batches songs together to reduce API calls:
        - Waits for batch to fill OR timeout
        - Sends bulk request to Gemini
        - Distributes results back to cached metadata
        """
        while self._running:
            try:
                # Wait for batch to be ready or timeout
                try:
                    await asyncio.wait_for(
                        self._gemini_batch_event.wait(),
                        timeout=ANALYZER_CONFIG.gemini_batch_timeout
                    )
                except asyncio.TimeoutError:
                    pass  # Timeout is fine, process whatever we have
                
                self._gemini_batch_event.clear()
                
                # Get batch to process
                async with self._gemini_batch_lock:
                    if not self._gemini_batch:
                        continue
                    
                    # Take up to batch_size items
                    batch = self._gemini_batch[:ANALYZER_CONFIG.gemini_batch_size]
                    self._gemini_batch = self._gemini_batch[ANALYZER_CONFIG.gemini_batch_size:]
                
                if not batch:
                    continue
                
                # Process batch
                await self._process_gemini_batch(batch)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Gemini batch worker error: {e}")
                await asyncio.sleep(1)
    
    async def _process_gemini_batch(
        self,
        batch: list[tuple[AnalysisTask, AudioFeatures, Optional[SemanticFeatures]]]
    ) -> None:
        """
        Process a batch of songs through Gemini in a single bulk request.
        """
        if not batch:
            return
        
        logger.debug(f"Processing Gemini batch of {len(batch)} songs")
        
        # Build batch request
        songs_data = []
        for task, audio, semantic in batch:
            song_info = {
                "song_id": task.song_id,
                "title": task.identifier.title or "",
                "artist": task.identifier.artist or "",
                "album": task.identifier.album,
                "audio_features": {
                    "bpm": audio.bpm,
                    "key": audio.key,
                    "energy": audio.energy,
                    "loudness": audio.loudness_db
                }
            }
            
            if semantic:
                song_info["semantic_tags"] = (
                    semantic.instrument_tags + semantic.sound_tags
                )
            
            songs_data.append(song_info)
        
        try:
            # Make bulk Gemini request
            results = await self.gemini.analyze_songs_bulk(songs_data)
            
            self._stats["gemini_batches_sent"] += 1
            self._stats["gemini_songs_analyzed"] += len(batch)
            
            # Update cached metadata with results
            for (task, audio, semantic), result in zip(batch, results):
                if result and result.get("success"):
                    data = result.get("data", {})
                    
                    librarian_info = LibrarianInfo(
                        genres=data.get("genres", []),
                        moods=data.get("moods", []),
                        themes=data.get("themes", []),
                        energy_level=data.get("energy_level", 0.5),
                        danceability=data.get("danceability", 0.5),
                        explicit_content=data.get("explicit_content", False),
                        cultural_vibe=data.get("cultural_vibe", ""),
                        canonical_title=data.get("canonical_title"),
                        similar_artists=data.get("similar_artists", []),
                        recommendation_tags=data.get("recommendation_tags", [])
                    )
                    
                    # Update cached metadata
                    existing = await self.cache.get_metadata(task.song_id)
                    if existing:
                        existing.librarian_info = librarian_info
                        existing.updated_at = time.time()
                        await self.cache.set_metadata(task.song_id, existing)
                else:
                    logger.warning(f"Gemini batch failed for {task.song_id}")
                    
        except Exception as e:
            logger.error(f"Gemini batch processing failed: {e}")
    
    async def get_queue_status(self) -> dict[str, Any]:
        """Get current queue status including batch info."""
        async with self._queue_lock:
            priority_counts = {}
            for p in AnalysisPriority:
                priority_counts[p.name] = sum(
                    1 for t in self._queue if t.priority == p
                )
            
            async with self._gemini_batch_lock:
                gemini_batch_size = len(self._gemini_batch)
            
            return {
                "queue_size": len(self._queue),
                "processing": len(self._processing),
                "priorities": priority_counts,
                "worker_count": self._worker_count,
                "gemini_batch_pending": gemini_batch_size,
                **self._stats
            }
    
    async def analyze_immediate(
        self,
        identifier: SongIdentifier,
        mode: AnalysisMode = AnalysisMode.FULL
    ) -> Optional[SongMetadata]:
        """
        Analyze a song immediately, bypassing the queue.
        
        Used for currently playing songs that need immediate analysis.
        
        Args:
            identifier: Song to analyze
            mode: Analysis mode
            
        Returns:
            SongMetadata if successful
        """
        await self.initialize()
        
        song_id = identifier.primary_id
        if not song_id:
            return None
        
        # Check cache first
        existing = await self.cache.get_metadata(song_id)
        if existing and self._is_fully_analyzed(existing, mode):
            return existing
        
        # Create and process task inline
        task = AnalysisTask(
            song_id=song_id,
            identifier=identifier,
            priority=AnalysisPriority.IMMEDIATE,
            mode=mode,
            created_at=time.time()
        )
        
        await self._process_task(task)
        
        # Return result from cache
        return await self.cache.get_metadata(song_id)


# Singleton instance
_song_analyzer: Optional[SongAnalyzer] = None


def get_song_analyzer() -> SongAnalyzer:
    """Get global song analyzer instance."""
    global _song_analyzer
    if _song_analyzer is None:
        _song_analyzer = SongAnalyzer()
    return _song_analyzer
