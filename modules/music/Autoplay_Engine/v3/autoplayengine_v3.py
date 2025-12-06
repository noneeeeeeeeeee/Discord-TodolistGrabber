import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from collections import deque
from modules.enviromentfilegenerator import check_and_load_env_file
from .cache_manager import CacheManager, EnrichmentEntry, ParsingEntry
from .collaborative_matrix import CollaborativeMatrix
from .contextual_recommender import (
    CandidateFeatures,
    ContextualRecommender,
    ScoredCandidate,
)
from .dependency_manager import DependencyManager
from .feedback_manager import FeedbackManager
from .gemini_service import GeminiService
from .track_resolver import TrackResolver
from .session_manager import AutoplaySessionManager
from .bootstrap_manager import BootstrapManager

# V3 Factory Worker Pattern modules
from .enrichment_worker import EnrichmentWorker, Priority

try:
    from .preview_fetcher import PreviewFetcher
except ImportError:
    PreviewFetcher = None  # type: ignore

try:
    from .enriching_service import EnrichingService
except ImportError:
    EnrichingService = None

# AudioDownloader REMOVED - Deezer-only architecture (no YouTube audio downloads)

try:
    from .deezer_fetch import DeezerClient
except ImportError:
    DeezerClient = None  # type: ignore

LOG = logging.getLogger(__name__)

# Import shared configuration constants to avoid circular imports
from .config import (
    DEFAULT_VERBOSITY,
    LASTFM_API_KEY_ENV,
    PARSING_SCHEMA_VERSION,
    DEFAULT_ANALYSIS_MODE,
    DEFAULT_EMBEDDING_MODEL,
    MAX_ANALYSIS_RETRIES,
    PRIORITY_ACTIVE,
    PRIORITY_BUFFER,
    PRIORITY_DAYDREAM,
    DAYDREAM_QUEUE_BACKLOG_LIMIT,
    DAYDREAM_BATCH_SIZE,
    BUFFER_TARGET_SIZE,
    P1_CANDIDATE_LIMIT,
    P1_TIMEOUT_SECONDS,
    MAX_QUEUE_BACKLOG,
)

print("[AutoplayEngineV3] Set verbosity to ", DEFAULT_VERBOSITY)
print(
    f"[AutoplayEngineV3] Configuration: ANALYSIS_MODE={DEFAULT_ANALYSIS_MODE}, EMBEDDING_MODEL={DEFAULT_EMBEDDING_MODEL}"
)
check_and_load_env_file()


@dataclass
class AvailabilityReport:
    lastfm: bool
    gemini: bool
    status: str


class AutoplayEngineV3:
    """Primary coordination class for Autoplay V3 services."""

    def __init__(
        self,
        *,
        cache_dir: Path | str = Path("cache/music"),
        gemini_service: Optional[GeminiService] = None,
        feedback_manager: Optional[FeedbackManager] = None,
        collaborative_matrix: Optional[CollaborativeMatrix] = None,
        track_resolver: Optional[TrackResolver] = None,
        analysis_mode: Optional[str] = None,  # "ml" or "non-ml", defaults to env var
        embedding_model: Optional[str] = None,  # Only used in ML mode, defaults to env var
        analysis_workers: int = 3,  # Number of concurrent analysis workers
    ) -> None:
        cache_path = Path(cache_dir)
        # instance verbosity (0 = off, 1 = debug, 2 = very verbose)
        self._verbose = DEFAULT_VERBOSITY
        if self._verbose:
            # Configure logging for autoplay V3 modules only (not root logger)
            # Verbosity 0: WARN+ only, Verbosity 1: INFO+, Verbosity 2: DEBUG+
            autoplay_loggers = [
                "modules.music.Autoplay_Engine.v3",
                "modules.music.Autoplay_Engine.v3.autoplayengine_v3",
                "modules.music.Autoplay_Engine.v3.gemini_service",
                "modules.music.Autoplay_Engine.v3.cache_manager",
                "modules.music.Autoplay_Engine.v3.context_tracker",
                "modules.music.Autoplay_Engine.v3.contextual_recommender",
                "modules.music.Autoplay_Engine.v3.feedback_manager",
                "modules.music.Autoplay_Engine.v3.novelty_controller",
                "modules.music.Autoplay_Engine.v3.track_resolver",
                "modules.music.Autoplay_Engine.v3.deezer_fetch",
                "modules.music.Autoplay_Engine.v3.preview_fetcher",
                "modules.music.Autoplay_Engine.v3.bootstrap_manager",
                "modules.music.Autoplay_Engine.v3.enriching_service",
            ]
            
            # Map verbosity to logging level
            if self._verbose >= 2:
                log_level = logging.DEBUG
            elif self._verbose >= 1:
                log_level = logging.INFO
            else:
                log_level = logging.WARNING

            # Add console handler to each autoplay logger if not present
            for logger_name in autoplay_loggers:
                logger = logging.getLogger(logger_name)
                logger.setLevel(log_level)

                # Only add handler if this logger doesn't have one
                if not logger.handlers:
                    handler = logging.StreamHandler()
                    handler.setLevel(log_level)
                    formatter = logging.Formatter(
                        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
                    )
                    handler.setFormatter(formatter)
                    logger.addHandler(handler)
                    # Don't propagate to avoid duplicate logs
                    logger.propagate = False

            LOG.debug(
                "[AutoplayV3] Verbosity enabled: level=%s (autoplay modules only)",
                self._verbose,
            )
        self._cache = CacheManager(cache_path)
        self._preview_metadata_cache: Dict[str, Dict[str, Any]] = {}
        self._preview_locks: Dict[str, asyncio.Lock] = {}
        self._preview_cache_ttl = 3600.0  # seconds
        self._gemini = gemini_service or GeminiService(cache_dir=cache_path)
        self._feedback = feedback_manager or FeedbackManager(cache_dir=cache_path)
        self._collaborative = collaborative_matrix or CollaborativeMatrix(self._cache)
        self._recommender = ContextualRecommender(self._collaborative)
        self._track_resolver = track_resolver or TrackResolver(
            self._cache, self._gemini
        )
        self._lastfm_key = os.getenv(LASTFM_API_KEY_ENV, "").strip()
        
        # Resource limiter: Max 2 concurrent ML sessions
        max_sessions = int(os.getenv("AUTOPLAY_MAX_SESSIONS", "2"))
        self._session_manager = AutoplaySessionManager(max_sessions=max_sessions)
        
        # Cold Start Bootstrapper (Daydreamer) - handles background track discovery
        self._bootstrap_manager = BootstrapManager(self)
        
        # V3 Factory Worker Pattern: Enrichment pipeline for tracks
        self._enrichment_worker = EnrichmentWorker(self)

        # V3 Configuration: Use environment variables if not explicitly provided
        if analysis_mode is None:
            analysis_mode = DEFAULT_ANALYSIS_MODE
            LOG.info(f"📊 Using ANALYSIS_MODE from environment: {analysis_mode}")
        else:
            analysis_mode = analysis_mode.lower()
        
        if embedding_model is None:
            embedding_model = DEFAULT_EMBEDDING_MODEL
            LOG.info(f"📊 Using EMBEDDING_MODEL from environment: {embedding_model}")
        else:
            embedding_model = embedding_model.lower()

        # Validate analysis_mode
        if analysis_mode not in ("ml", "non-ml"):
            LOG.warning(f"⚠️ Invalid ANALYSIS_MODE '{analysis_mode}', defaulting to 'ml'")
            analysis_mode = "ml"
        
        # Validate embedding_model (map legacy identifiers to EfficientAT)
        legacy_model_aliases = {
            "panns_mobilenetv2": "mn10_as",
            "panns_cnn14": "mn10_as",
            "openl3": "mn10_as",
        }
        normalized_model = legacy_model_aliases.get(embedding_model, embedding_model)
        if normalized_model != embedding_model:
            LOG.info(
                "ℹ️ Mapping legacy embedding model '%s' to '%s'",
                embedding_model,
                normalized_model,
            )
        if normalized_model != "mn10_as":
            LOG.warning(
                "⚠️ Unsupported EMBEDDING_MODEL '%s', defaulting to 'mn10_as'",
                normalized_model,
            )
            normalized_model = "mn10_as"
        embedding_model = normalized_model
        
        # Dependency + ingest helpers
        self._dependency_manager = DependencyManager(model_key=embedding_model)
        try:
            if analysis_mode == "ml":
                self._dependency_manager.ensure_ready()
            else:
                self._dependency_manager.ensure_ffmpeg()
        except Exception as exc:
            LOG.debug("Dependency priming skipped: %s", exc)

        preview_cache = cache_path / "previews"
        self._preview_fetcher = None
        if PreviewFetcher:
            try:
                self._preview_fetcher = PreviewFetcher(cache_dir=preview_cache)
            except Exception as exc:
                LOG.warning("⚠️ Preview fetcher unavailable (Deezer ingest disabled): %s", exc)
        else:
            LOG.warning("⚠️ Preview fetcher dependency missing (install aiohttp to enable Deezer ingest)")

        # Initialize EfficientAT/Librosa analysis service
        self._analyzer = None
        self._embedding_model = embedding_model
        if EnrichingService:
            try:
                self._analyzer = EnrichingService(
                    analysis_mode=analysis_mode,
                    embedding_model=embedding_model,
                    verbose=False,
                )
                if not self._analyzer.is_available() and analysis_mode == "ml":
                    LOG.warning("⚠️ ML analysis unavailable, retrying in non-ML mode")
                    self._analyzer = EnrichingService(
                        analysis_mode="non-ml",
                        embedding_model=embedding_model,
                        verbose=False,
                    )
            except Exception as exc:
                LOG.error("❌ Failed to initialize EnrichingService: %s", exc)
                if analysis_mode == "ml":
                    LOG.warning("⚠️ Attempting Non-ML mode fallback")
                    try:
                        self._analyzer = EnrichingService(
                            analysis_mode="non-ml",
                            embedding_model=embedding_model,
                            verbose=False,
                        )
                    except Exception as non_ml_exc:
                        LOG.error("❌ Non-ML fallback also failed: %s", non_ml_exc)
                        self._analyzer = None

        if self._analyzer is None:
            LOG.error(
                "❌ EnrichingService unavailable. Install librosa, numpy, scipy, torch, torchaudio"
            )

        # AudioDownloader REMOVED - V3 uses Deezer previews only (no YouTube audio downloads)
        
        # Analysis Worker Pool (async background processing)
        self._analysis_queue: deque = deque()  # Queue of job dicts (track_id, youtube_url)
        self._active_jobs: Dict[int, Dict[str, Any]] = {}
        self._analysis_workers: List[asyncio.Task] = []
        self._analysis_worker_count = analysis_workers
        self._analysis_shutdown = False
        self._analysis_mode = analysis_mode
        
        # Monitoring stats
        self._analysis_stats = {
            "queued": 0,
            "processed": 0,
            "failed": 0,
            "queue_depth": 0,
            "avg_duration": 0.0,
            "in_progress": 0,
        }
        self._last_summary_time = 0.0  # For level 1 periodic summary logs

        self._restore_persistent_queue()
        
        if self._analyzer and self._analyzer.is_available():
            mode_str = f"Non-ML mode (lightweight)" if analysis_mode == "non-ml" else f"ML mode ({embedding_model})"
            LOG.info(f"✅ Audio analysis enabled: {mode_str} with {analysis_workers} workers")
        else:
            LOG.info("ℹ️ Audio analysis disabled (using Gemini estimates)")

        if not self._lastfm_key:
            LOG.warning(
                "Last.fm API key missing. Populate LASTFM_API_KEY in .env (see modules/enviromentfilegenerator.py)."
            )
        if not self._gemini.is_available:
            LOG.warning(
                "Gemini unavailable during AutoplayEngineV3 init: %s",
                self._gemini.status,
            )

        # Eagerly start analysis workers if possible (Daydream mode)
        if self._analyzer and self._analyzer.is_available():
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.start_analysis_workers())
                LOG.info("🚀 Scheduled background analysis workers (Daydream mode)")
            except RuntimeError:
                # No running loop yet (e.g. during early init)
                LOG.debug("⚠️ Could not start analysis workers in __init__ (no event loop)")

    # ------------------------------------------------------------------
    # Verbosity helpers
    # ------------------------------------------------------------------
    def _log_verbose(
        self,
        level: int,
        message: str,
        *args: Any,
        log_fn: Optional[Callable[..., None]] = None,
    ) -> None:
        """Emit a log line when the configured verbosity meets the threshold."""

        if self._verbose < level:
            return

        if log_fn is None:
            log_fn = LOG.info

        log_fn(message, *args)

    def _format_eta(self, jobs_remaining: int) -> str:
        """Return a human-readable ETA for the remaining jobs."""

        avg = float(self._analysis_stats.get("avg_duration", 0.0) or 0.0)
        if avg <= 0.0 or jobs_remaining <= 0:
            return "ETA: --"

        total_seconds = int(jobs_remaining * avg)
        minutes, seconds = divmod(total_seconds, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"ETA: {hours}h {minutes}m"
        if minutes:
            return f"ETA: {minutes}m {seconds:02d}s"
        return f"ETA: {seconds}s"

    def _record_job_duration(self, duration: float) -> None:
        """Update the rolling average duration for worker jobs."""

        if duration <= 0:
            return

        avg = float(self._analysis_stats.get("avg_duration", 0.0) or 0.0)
        if avg <= 0.0:
            self._analysis_stats["avg_duration"] = duration
            return

        # Simple exponential moving average (80/20 split)
        self._analysis_stats["avg_duration"] = (avg * 0.8) + (duration * 0.2)

    def _log_summary_if_due(self) -> None:
        """Log periodic summary at verbosity level 1 (every 30s when active)."""
        if self._verbose < 1:
            return
        
        now = time.time()
        # Log every 30 seconds when there's activity
        if now - self._last_summary_time < 30.0:
            return
        
        stats = self._analysis_stats
        queued = len(self._analysis_queue)
        processed = stats.get("processed", 0)
        failed = stats.get("failed", 0)
        in_progress = stats.get("in_progress", 0)
        
        # Only log if there's activity
        if queued == 0 and in_progress == 0:
            return
        
        eta = self._format_eta(queued + in_progress)
        mode_str = "ML" if self._analysis_mode == "ml" else "Non-ML"
        
        LOG.info(
            "🎵 [Autoplay] Analyzing: %d in progress, %d queued, %d done, %d failed (%s, %s)",
            in_progress, queued, processed, failed, mode_str, eta
        )
        self._last_summary_time = now

    def _log_queue_update(self, track_id: str) -> None:
        """Log when track added to queue. Level 2 only."""
        if self._verbose < 2:
            return

        pending = len(self._analysis_queue)
        eta = self._format_eta(pending + self._analysis_stats.get("in_progress", 0))
        track_display = track_id if len(track_id) <= 80 else track_id[:77] + "..."
        LOG.info(
            "➕ Added to worker queue: %s (%d pending, %s)",
            track_display,
            pending,
            eta,
        )

    def _log_worker_progress(self, worker_id: int, track_id: str) -> None:
        """Log worker progress. Level 2 shows per-track, level 1 is suppressed (batch summary instead)."""
        if self._verbose < 2:
            return

        track_display = track_id if len(track_id) <= 80 else track_id[:77] + "..."
        processed = self._analysis_stats["processed"]
        failed = self._analysis_stats["failed"]
        in_progress = self._analysis_stats.get("in_progress", 0)
        queued = len(self._analysis_queue)

        current_job = processed + failed + in_progress
        total_jobs = current_job + queued
        eta = self._format_eta(queued + in_progress)

        LOG.info(
            "👷 Worker %d: %s (%d/%d, %s)",
            worker_id,
            track_display,
            max(1, current_job),
            max(current_job, total_jobs, 1),
            eta,
        )

    def _normalize_youtube_url(self, youtube_url: str) -> str:
        if youtube_url.startswith("http"):
            return youtube_url
        return f"https://www.youtube.com/watch?v={youtube_url}"

    def _build_analysis_job(
        self,
        track_id: str,
        youtube_url: str,
        *,
        preview_url: Optional[str] = None,
        preview_duration_ms: Optional[int] = None,
        deezer_track_id: Optional[str] = None,
        priority: int = PRIORITY_ACTIVE,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        job: Dict[str, Any] = {
            "track_id": track_id,
            "youtube_url": self._normalize_youtube_url(youtube_url),
            "priority": priority,
            "attempts": 0,
            "enqueued_at": time.time(),
            "last_error": None,
        }
        if preview_url:
            job["preview_url"] = preview_url
        if preview_duration_ms is not None:
            try:
                job["preview_duration_ms"] = int(preview_duration_ms)
            except (TypeError, ValueError):
                pass
        if deezer_track_id:
            job["deezer_track_id"] = deezer_track_id

        if overrides:
            for key in (
                "priority",
                "attempts",
                "enqueued_at",
                "last_error",
                "preview_url",
                "preview_duration_ms",
                "deezer_track_id",
            ):
                if key in overrides and overrides[key] is not None:
                    job[key] = overrides[key]
        return job

    def _restore_persistent_queue(self) -> None:
        try:
            state = self._cache.load_ingest_queue_state()
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("Failed to restore ingest queue: %s", exc)
            state = {"pending": [], "in_progress": []}

        restored: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for bucket in ("in_progress", "pending"):
            for job in state.get(bucket, []):
                track_id = job.get("track_id")
                youtube_url = job.get("youtube_url")
                if not track_id or not youtube_url or track_id in seen:
                    continue
                restored.append(self._build_analysis_job(track_id, youtube_url, overrides=job))
                seen.add(track_id)

        for job in restored:
            self._analysis_queue.append(job)

        self._analysis_stats["queue_depth"] = len(self._analysis_queue)
        if restored and self._verbose:
            LOG.info("♻️ Restored %d ingest job(s) from disk", len(restored))

    def _persist_analysis_queue_state(self) -> None:
        pending = list(self._analysis_queue)
        in_progress = list(self._active_jobs.values())
        try:
            self._cache.persist_ingest_queue_state(pending, in_progress)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("Failed to persist ingest queue state: %s", exc)

    def _is_job_tracked(self, track_id: str) -> bool:
        if any(job.get("track_id") == track_id for job in self._analysis_queue):
            return True
        return any(job.get("track_id") == track_id for job in self._active_jobs.values())

    def _get_next_priority_job(self) -> Optional[Dict[str, Any]]:
        """
        Get the highest priority job from the queue.
        
        Priority order: P1 (Active) > P2 (Buffer) > P3 (Daydream)
        Within same priority, FIFO order is maintained.
        
        Returns:
            Job dict or None if queue is empty
        """
        if not self._analysis_queue:
            return None
        
        # Find the job with lowest priority number (P1=1 is highest priority)
        best_idx = 0
        best_priority = self._analysis_queue[0].get("priority", 3)  # Default to P3
        
        for idx, job in enumerate(self._analysis_queue):
            job_priority = job.get("priority", 3)
            if job_priority < best_priority:
                best_priority = job_priority
                best_idx = idx
                # P1 is highest, no need to look further
                if job_priority == PRIORITY_ACTIVE:
                    break
        
        # Remove and return the selected job
        job = self._analysis_queue[best_idx]
        del self._analysis_queue[best_idx]
        return job

    def _handle_job_failure(
        self,
        worker_id: int,
        job: Dict[str, Any],
        error_message: str,
    ) -> None:
        track_id = job.get("track_id") or ""
        artist_key, title_key = self._split_track_key(track_id)
        async def _mark_failure_state(permanent: bool) -> None:
            if not artist_key and not title_key:
                return
            try:
                entry = await self._cache.get_enrichment(artist_key, title_key)
                if not entry:
                    return
                entry.last_analysis_attempt = time.time()
                entry.analysis_in_progress = not permanent
                if permanent:
                    entry.analysis_verified = False
                await self._cache.set_enrichment(artist_key, title_key, entry)
            except Exception as exc:
                LOG.debug("Failed to update analysis state for %s: %s", track_id, exc)

        attempts = int(job.get("attempts", 0) or 0) + 1
        job["attempts"] = attempts
        job["last_error"] = error_message

        if attempts < MAX_ANALYSIS_RETRIES:
            job["enqueued_at"] = time.time()
            self._analysis_queue.append(job)
            self._analysis_stats["queue_depth"] = len(self._analysis_queue)
            self._log_verbose(
                1,
                "🔁 Worker %d retrying %s (attempt %d/%d)",
                worker_id,
                job.get("track_id"),
                attempts,
                MAX_ANALYSIS_RETRIES,
            )
            asyncio.create_task(_mark_failure_state(permanent=False))
            return

        self._analysis_stats["failed"] += 1
        LOG.warning(
            "❌ [Worker %d] Giving up on %s after %d attempts (%s)",
            worker_id,
            job.get("track_id"),
            attempts,
            error_message,
        )
        asyncio.create_task(_mark_failure_state(permanent=True))

    @property
    def is_available(self) -> bool:
        """Full system availability: both Last.fm AND Gemini must be ready.

        Use this for features that require full enrichment (mood analysis, etc).
        """
        return bool(self._lastfm_key) and self._gemini.is_available

    @property
    def can_recommend(self) -> bool:
        """Can generate basic recommendations: only Last.fm is required.

        Use this for autoplay trigger checks - recommendations work even if
        Gemini is temporarily rate-limited (tags/moods just won't be enriched).
        """
        return bool(self._lastfm_key)

    def availability_report(self) -> AvailabilityReport:
        status = "ready" if self.is_available else self._gemini.status
        return AvailabilityReport(
            lastfm=bool(self._lastfm_key),
            gemini=self._gemini.is_available,
            status=status,
        )

    @property
    def feedback_manager(self) -> FeedbackManager:
        return self._feedback

    @property
    def collaborative_matrix(self) -> CollaborativeMatrix:
        return self._collaborative

    @property
    def recommender(self) -> ContextualRecommender:
        return self._recommender

    @property
    def track_resolver(self) -> TrackResolver:
        return self._track_resolver

    @property
    def cache(self) -> CacheManager:
        """Access to the cache manager for enrichment entries."""
        return self._cache
    
    @property
    def gemini(self) -> GeminiService:
        """Access to the Gemini service for cultural enrichment."""
        return self._gemini
    
    @property
    def preview_fetcher(self):
        """Access to the preview fetcher for Deezer audio."""
        return self._preview_fetcher
    
    @property
    def analyzer(self):
        """Access to the audio analyzer (EnrichingService)."""
        return self._analyzer
    
    @property
    def enrichment_worker(self) -> EnrichmentWorker:
        """Access to the EnrichmentWorker for adding tasks."""
        return self._enrichment_worker
    
    @property
    def bootstrap_manager(self) -> BootstrapManager:
        """Access to the BootstrapManager (Daydreamer)."""
        return self._bootstrap_manager

    # ========== AUDIO ANALYSIS WORKER POOL ==========
    
    async def start_analysis_workers(self) -> None:
        """Start background audio analysis workers (V3)."""
        if not self._analyzer or not self._analyzer.is_available():
            LOG.debug("Audio analyzer not available, skipping worker startup")
            return
        # Deezer-only architecture: preview_fetcher is REQUIRED
        if not self._preview_fetcher:
            LOG.warning("⚠️ Preview fetcher unavailable (Deezer-only architecture requires it)")
            return
            
        if self._analysis_workers:
            LOG.debug("Analysis workers already running")
            return
        
        mode_str = "Non-ML" if self._analysis_mode == "non-ml" else "ML"
        self._log_verbose(
            1, "🚀 Starting %d %s analysis workers", 
            self._analysis_worker_count, mode_str
        )
        self._analysis_shutdown = False
        for i in range(self._analysis_worker_count):
            task = asyncio.create_task(self._analysis_worker_loop(worker_id=i))
            self._analysis_workers.append(task)
            
        # Start bootstrapper (Daydreamer)
        if self._bootstrap_manager:
            await self._bootstrap_manager.start()
        
        # Start V3 Factory Worker Pattern - Enrichment Worker
        if self._enrichment_worker:
            await self._enrichment_worker.start()
        
    
    async def stop_analysis_workers(self) -> None:
        """Gracefully shutdown analysis workers."""
        if not self._analysis_workers:
            return
        
        self._log_verbose(1, "🛑 Stopping %d analysis workers...", len(self._analysis_workers))
        self._analysis_shutdown = True
        
        # Stop V3 Factory Worker Pattern - Enrichment Worker
        if self._enrichment_worker:
            await self._enrichment_worker.stop()
        
        # Stop bootstrapper
        if self._bootstrap_manager:
            await self._bootstrap_manager.stop()
        
        # Wait for workers to finish current jobs
        await asyncio.gather(*self._analysis_workers, return_exceptions=True)
        self._analysis_workers.clear()
        self._active_jobs.clear()
        self._analysis_stats["in_progress"] = 0
        self._persist_analysis_queue_state()
        self._log_verbose(1, "✅ Analysis workers stopped")
    

    def queue_analysis(
        self, track_id: str, youtube_url: str, *, priority: int = PRIORITY_ACTIVE
    ) -> bool:
        """Queue a track for background analysis using Deezer preview only.

        NOTE: Despite the parameter name, youtube_url is kept for backward compatibility
        but is NOT used for audio downloading. Only Deezer previews are used.

        Args:
            track_id: Normalized ``artist::title`` cache key.
            youtube_url: YouTube URL (DEPRECATED - kept for cache key compatibility).
            priority: Job priority (1=active/user waiting, 2=buffer, 3=daydream).

        Returns:
            True if the track was enqueued, False otherwise.
        """
        if not self._analyzer or not self._analyzer.is_available():
            return False
        if not youtube_url:
            self._log_verbose(
                2,
                "[Analysis Queue] Missing YouTube URL for %s, skipping",
                track_id,
                log_fn=LOG.debug,
            )
            return False
        
        mapping_entry = self._cache._mapping_cache.get(track_id)
        preview_url = mapping_entry.preview_url if mapping_entry else None
        preview_duration_ms = mapping_entry.preview_duration_ms if mapping_entry else None
        deezer_track_id = mapping_entry.deezer_track_id if mapping_entry else None
        
        # Deezer-only architecture: REQUIRE preview_url
        # If no Deezer preview, track is assumed non-commercial music
        if not preview_url:
            self._log_verbose(
                1,
                "[Analysis Queue] No Deezer preview for %s - assuming non-commercial music, skipping",
                track_id,
                log_fn=LOG.debug,
            )
            return False
        
        if not self._preview_fetcher:
            self._log_verbose(
                1,
                "[Analysis Queue] Preview fetcher unavailable (Deezer-only architecture)",
                log_fn=LOG.debug,
            )
            return False

        entry = self._cache._enrichment_cache.get(track_id)
        if not entry:
            self._log_verbose(
                2,
                "[Analysis Queue] No enrichment entry yet for %s, skipping",
                track_id,
                log_fn=LOG.debug,
            )
            return False
        
        if self._analysis_mode == "ml" and entry.computed_embedding is not None:
            self._log_verbose(
                2,
                "Track %s already has ML analysis, skipping queue",
                track_id,
                log_fn=LOG.debug,
            )
            return False
        if self._analysis_mode == "non-ml" and entry.computed_simple_vibe is not None:
            self._log_verbose(
                2,
                "Track %s already has Non-ML analysis, skipping queue",
                track_id,
                log_fn=LOG.debug,
            )
            return False

        if self._is_job_tracked(track_id):
            self._log_verbose(
                2,
                "Track %s already queued or processing; skipping duplicate",
                track_id,
                log_fn=LOG.debug,
            )
            return False

        url = youtube_url
        if not youtube_url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={youtube_url}"
        artist, title = self._split_track_key(track_id)
        if entry:
            entry.analysis_in_progress = True
            entry.analysis_verified = False
            entry.last_analysis_attempt = time.time()
            async def _persist_state() -> None:
                try:
                    await self._cache.set_enrichment(artist, title, entry)
                except Exception as exc:
                    LOG.debug("Failed to persist analysis state for %s: %s", track_id, exc)
            asyncio.create_task(_persist_state())

        job = self._build_analysis_job(
            track_id,
            url,
            preview_url=preview_url,
            preview_duration_ms=preview_duration_ms,
            deezer_track_id=deezer_track_id,
            priority=priority,
        )
        self._analysis_queue.append(job)
        self._analysis_stats["queued"] += 1
        self._analysis_stats["queue_depth"] = len(self._analysis_queue)
        self._persist_analysis_queue_state()
        self._log_queue_update(track_id)
        return True
    

    def queue_audio_analysis(
        self,
        artist: str,
        title: str,
        youtube_id: str,
        *,
        preview_url: Optional[str] = None,
        preview_duration_ms: Optional[int] = None,
        deezer_track_id: Optional[str] = None,
        priority: int = PRIORITY_ACTIVE,
    ) -> bool:
        """Helper to enqueue analysis using a bare YouTube video ID."""
        if not youtube_id:
            return False
        track_id = self._make_track_key(artist, title)
        if track_id not in self._cache._enrichment_cache:
            return False
        url = youtube_id
        if not youtube_id.startswith("http"):
            url = f"https://www.youtube.com/watch?v={youtube_id}"
        # Note: queue_analysis reads preview_url/deezer_track_id from mapping cache internally
        return self.queue_analysis(track_id, url, priority=priority)

    async def ensure_preview_metadata(
        self,
        artist: str,
        title: str,
        *,
        youtube_id: Optional[str] = None,
        expected_duration_ms: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """Resolve Deezer preview metadata for a track, caching results when possible."""

        track_key = self._make_track_key(artist, title)
        lock = self._preview_locks.get(track_key)
        if lock is None:
            lock = asyncio.Lock()
            self._preview_locks[track_key] = lock

        async with lock:
            mapping = await self._cache.get_mapping(artist, title)
            preview_url = mapping.preview_url if mapping else None
            preview_duration_ms = mapping.preview_duration_ms if mapping else None
            deezer_track_id = mapping.deezer_track_id if mapping else None

            if preview_url:
                return preview_url, preview_duration_ms, deezer_track_id

            cached = self._preview_metadata_cache.get(track_key)
            if cached:
                if time.time() - cached["timestamp"] <= self._preview_cache_ttl:
                    return (
                        cached.get("preview_url"),
                        cached.get("preview_duration_ms"),
                        cached.get("deezer_track_id") or deezer_track_id,
                    )
                self._preview_metadata_cache.pop(track_key, None)

            if DeezerClient is None:
                return preview_url, preview_duration_ms, deezer_track_id

            lookup_duration = expected_duration_ms
            if lookup_duration is None and mapping and mapping.duration_ms:
                lookup_duration = mapping.duration_ms

            try:
                async with DeezerClient() as deezer_client:
                    match = await deezer_client.find_track_by_metadata(
                        artist=artist,
                        title=title,
                        expected_duration_ms=lookup_duration,
                    )
            except Exception as exc:
                LOG.debug(
                    "Deezer preview lookup failed for %s - %s (yt=%s): %s",
                    artist,
                    title,
                    youtube_id or "unknown",
                    exc,
                )
                return preview_url, preview_duration_ms, deezer_track_id

            if not match:
                return preview_url, preview_duration_ms, deezer_track_id

            preview_url = match.track.preview_url or preview_url
            preview_duration_ms = preview_duration_ms or match.track.duration_ms
            deezer_track_id = match.track.id or deezer_track_id

            if preview_url:
                self._preview_metadata_cache[track_key] = {
                    "preview_url": preview_url,
                    "preview_duration_ms": preview_duration_ms,
                    "deezer_track_id": deezer_track_id,
                    "timestamp": time.time(),
                }

            if mapping and preview_url:
                mapping.preview_url = preview_url
                mapping.preview_duration_ms = preview_duration_ms
                mapping.preview_fetched_at = time.time()
                if deezer_track_id:
                    mapping.deezer_track_id = deezer_track_id
                await self._cache.set_mapping(artist, title, mapping)
            elif mapping and deezer_track_id and not mapping.deezer_track_id:
                mapping.deezer_track_id = deezer_track_id
                await self._cache.set_mapping(artist, title, mapping)

            return preview_url, preview_duration_ms, deezer_track_id

    async def refresh_ingest_queue(self, *, limit: Optional[int] = None) -> Dict[str, Any]:
        """Rebuild the ingest queue by scanning cache entries missing analysis."""

        if not self._cache._enrichment_cache:
            return {"queued": 0, "missing_mapping": 0, "duplicates": 0, "queue_depth": len(self._analysis_queue)}

        queued = 0
        missing_mapping = 0
        duplicates = 0
        limit = max(1, limit) if limit else None

        for track_key, entry in self._cache._enrichment_cache.items():
            needs_analysis = (
                entry.computed_embedding is None
                if self._analysis_mode == "ml"
                else entry.computed_simple_vibe is None
            )
            if not needs_analysis:
                continue

            mapping = self._cache._mapping_cache.get(track_key)
            youtube_source = None
            if mapping:
                youtube_source = mapping.url or mapping.youtube_id
            if not youtube_source:
                missing_mapping += 1
                continue

            if self.queue_analysis(track_key, youtube_source, priority=PRIORITY_BUFFER):
                queued += 1
            else:
                duplicates += 1

            if limit and queued >= limit:
                break

        return {
            "queued": queued,
            "missing_mapping": missing_mapping,
            "duplicates": duplicates,
            "queue_depth": len(self._analysis_queue),
        }

    async def reingest_track(
        self,
        artist: str,
        title: str,
        *,
        youtube_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Force re-enrichment + analysis for a specific track."""

        artist = artist.strip()
        title = title.strip()
        track_key = self._make_track_key(artist, title)
        await self._cache.delete_enrichment_entry(artist, title)

        mapping = self._cache._mapping_cache.get(track_key)
        youtube_source = youtube_id or (mapping.url if mapping and mapping.url else None)
        if not youtube_source and mapping and mapping.youtube_id:
            youtube_source = mapping.youtube_id

        enrichment = await self.enrich_track(artist, title, youtube_id=youtube_source)

        queued = False
        if youtube_source and enrichment:
            queued = self.queue_audio_analysis(artist, title, youtube_source)

        return {
            "enriched": bool(enrichment),
            "queued_analysis": queued,
            "youtube_source": youtube_source,
            "mapping_found": bool(mapping),
        }
    
    async def _daydream_analysis(self) -> None:
        """
        When idle, proactively analyze popular/trending tracks.
        """
        # Strategy 1: Analyze tracks from collaborative matrix (high affinity)
        try:
            popular_tracks = await self._collaborative.get_popular_tracks(limit=50)
            
            for track_id in popular_tracks:
                # Check if already analyzed
                artist, title = self._split_track_key(track_id)
                entry = await self._cache.get_enrichment(artist, title)
                
                needs_analysis = False
                if not entry:
                    needs_analysis = True
                elif self._analysis_mode == "ml" and not entry.computed_embedding:
                    needs_analysis = True
                elif self._analysis_mode == "non-ml" and not entry.computed_simple_vibe:
                    needs_analysis = True
                
                if needs_analysis:
                    # Queue for analysis
                    mapping = await self._cache.get_mapping(artist, title)
                    youtube_url = mapping.url if mapping else None
                    if not youtube_url and mapping and mapping.youtube_id:
                        youtube_url = mapping.youtube_id
                    
                    if youtube_url:
                        queued = self.queue_analysis(track_id, youtube_url, priority=PRIORITY_DAYDREAM)
                        if queued:
                            LOG.debug(f"💤 [Daydream] Queued {track_id} for background analysis")
                            await asyncio.sleep(10)  # Rate limit: 1 track per 10s in daydream
                            return  # Process one track per daydream cycle
        except Exception as e:
            LOG.debug(f"Daydream error: {e}")
        
        await asyncio.sleep(60)  # Daydream cooldown: 1 minute

    async def _analysis_worker_loop(self, worker_id: int) -> None:
        """Background worker that processes audio analysis queue (V3).

        Supports both ML mode (EfficientAT embeddings) and Non-ML mode (Librosa-only).
        
        Args:
            worker_id: Unique worker identifier for logging
        """
        mode_str = "Non-ML" if self._analysis_mode == "non-ml" else "ML"
        self._log_verbose(2, "👷 Analysis worker %d (%s) started", worker_id, mode_str)
        
        while not self._analysis_shutdown:
            try:
                # Deezer-only architecture: preview_fetcher is REQUIRED
                if not self._preview_fetcher:
                    LOG.warning(
                        f"[Worker {worker_id}] Preview fetcher unavailable (Deezer-only); shutting down"
                    )
                    break
                # Check for work
                if not self._analysis_queue:
                    # Priority 2: Daydream mode
                    await self._daydream_analysis()
                    continue
                
                # Get next job (sorted by priority: P1 > P2 > P3)
                job = self._get_next_priority_job()
                if not job:
                    await self._daydream_analysis()
                    continue
                self._analysis_stats["queue_depth"] = len(self._analysis_queue)
                self._analysis_stats["in_progress"] += 1
                track_id = job.get("track_id")
                artist_from_key, title_from_key = self._split_track_key(track_id or "")
                youtube_url = job.get("youtube_url")
                job_start = time.time()
                self._active_jobs[worker_id] = job
                self._persist_analysis_queue_state()
                
                # Level 1: Periodic summary, Level 2: Per-track details
                self._log_summary_if_due()
                self._log_worker_progress(worker_id, track_id)
                
                # Download audio (Deezer preview ONLY - no YouTube fallback)
                # Per design: "ALWAYS download the deezer preview. NOT USE YOUTUBE"
                # If no Deezer preview, assume non-commercial music and skip
                audio_path = None
                cleanup_parent = False
                preview_url = job.get("preview_url")
                preview_duration_ms = job.get("preview_duration_ms")
                try:
                    preview_duration_ms = int(preview_duration_ms)
                except (TypeError, ValueError):
                    preview_duration_ms = None
                audio_source = "deezer_preview"
                try:
                    if preview_url and self._preview_fetcher:
                        audio_path = await self._preview_fetcher.fetch_preview(
                            preview_url,
                            track_id=track_id,
                            expected_duration_ms=preview_duration_ms,
                        )
                        if audio_path:
                            audio_source = "deezer_preview"
                        else:
                            LOG.debug(
                                "[Worker %d] Deezer preview download failed for %s",
                                worker_id,
                                track_id or "unknown",
                            )

                    # NO YOUTUBE FALLBACK - Deezer-only architecture
                    # If no Deezer preview, assume non-commercial music and skip
                    if not audio_path:
                        LOG.info(
                            "🚫 [Worker %d] No Deezer preview for %s - assuming non-commercial music, skipping",
                            worker_id,
                            track_id or "unknown",
                        )
                        self._analysis_stats["failed"] += 1
                        # Mark as permanently failed (no Deezer preview available)
                        artist_key, title_key = self._split_track_key(track_id or "")
                        if artist_key and title_key:
                            entry = await self._cache.get_enrichment(artist_key, title_key)
                            if entry:
                                entry.analysis_verified = False
                                entry.analysis_in_progress = False
                                entry.last_analysis_attempt = time.time()
                                await self._cache.set_enrichment(artist_key, title_key, entry)
                        continue  # Skip to next job

                    if self._verbose >= 2:
                        LOG.debug(
                            "[Worker %d] Using %s for %s",
                            worker_id,
                            audio_source,
                            track_id,
                        )
                    
                    # Analyze with EnrichingService (EfficientAT or Librosa-only)
                    analysis = await asyncio.to_thread(
                        self._analyzer.analyze_track,
                        str(audio_path)
                    )
                    
                    if analysis and analysis.success:
                        # Update cache with analysis results
                        entry = None
                        if artist_from_key or title_from_key:
                            entry = await self._cache.get_enrichment(
                                artist_from_key,
                                title_from_key,
                            )
                        if entry:
                            # Flow Vector (4D) - always present
                            entry.computed_tempo = analysis.tempo
                            entry.computed_loudness = analysis.loudness
                            entry.computed_key = analysis.key
                            entry.computed_mode = analysis.mode
                            
                            # Mode-specific vibe data
                            if analysis.analysis_mode == "ml":
                                # ML Mode: Store learned embedding
                                entry.computed_embedding = analysis.embedding
                                entry.computed_embedding_model = analysis.embedding_model
                                entry.computed_embedding_dim = analysis.embedding_dim
                                if analysis.simple_vibe:
                                    entry.computed_simple_vibe = analysis.simple_vibe
                            elif analysis.analysis_mode == "non-ml":
                                # Non-ML Mode: Store simple vibe
                                entry.computed_simple_vibe = analysis.simple_vibe
                            # Mark as verified
                            entry.analysis_verified = True
                            entry.analysis_in_progress = False
                            entry.last_analysis_attempt = time.time()
                            
                            await self._cache.set_enrichment(
                                artist_from_key,
                                title_from_key,
                                entry,
                            )
                            
                            self._analysis_stats["processed"] += 1
                            self._log_verbose(
                                2,
                                "✅ [Worker %d] Analysis complete: %s",
                                worker_id,
                                track_id,
                            )
                        else:
                            LOG.warning(f"[Worker {worker_id}] No cache entry found for {track_id}")
                            self._analysis_stats["failed"] += 1
                    else:
                        error_msg = analysis.error if analysis else "No analysis result"
                        LOG.warning(
                            f"[Worker {worker_id}] Analysis failed for {track_id}: {error_msg}"
                        )
                        self._handle_job_failure(worker_id, job, error_msg)
                
                except Exception as e:
                    LOG.error(f"[Worker {worker_id}] Analysis failed for {track_id}: {e}")
                    self._handle_job_failure(worker_id, job, str(e))
                
                finally:
                    duration = time.time() - job_start
                    self._record_job_duration(duration)
                    self._analysis_stats["in_progress"] = max(
                        0,
                        self._analysis_stats.get("in_progress", 0) - 1,
                    )
                    self._active_jobs.pop(worker_id, None)
                    self._persist_analysis_queue_state()
                    # Cleanup audio file
                    if audio_path and audio_path.exists():
                        try:
                            audio_path.unlink()
                            if cleanup_parent:
                                parent = audio_path.parent
                                if parent.exists() and not any(parent.iterdir()):
                                    parent.rmdir()
                        except Exception as e:
                            LOG.warning(f"[Worker {worker_id}] Failed to cleanup audio: {e}")
            
            except asyncio.CancelledError:
                self._log_verbose(2, "👷 Analysis worker %d cancelled", worker_id)
                break
            except Exception as e:
                LOG.error(f"[Worker {worker_id}] Unexpected error: {e}")
                await asyncio.sleep(1)  # Prevent tight error loop
        
        self._log_verbose(2, "👷 Analysis worker %d stopped", worker_id)
    

    def get_analysis_stats(self) -> Dict[str, Any]:
        """Get analysis worker pool statistics.
        
        Returns:
            Dict including queued, processed, failed, queue_depth, avg_duration, in_progress
        """
        return self._analysis_stats.copy()
    

    def get_analysis_health(self) -> Dict[str, Any]:
        """Get comprehensive analysis worker pool health metrics.
        
        Returns:
            Dict with:
                - available: bool (is analysis service available)
                - mode: str ("ml" or "non-ml")
                - workers_running: int (number of active workers)
                - stats: dict (queued, processed, failed, queue_depth)
                - success_rate: float (0.0-1.0, processed / (processed + failed))
                - coverage_estimate: str (estimated % of tracks with analysis data)
        """
        stats = self._analysis_stats.copy()
        total_attempts = stats["processed"] + stats["failed"]
        success_rate = stats["processed"] / total_attempts if total_attempts > 0 else 0.0
        
        # Estimate coverage by checking cache
        # Estimate coverage by checking cache
        try:
            total_tracks = len(self._cache._enrichment_cache)
            if self._analysis_mode == "ml":
                tracks_with_analysis = sum(
                    1 for entry in self._cache._enrichment_cache.values()
                    if entry.computed_embedding is not None
                )
            else:  # non-ml
                tracks_with_analysis = sum(
                    1 for entry in self._cache._enrichment_cache.values()
                    if entry.computed_simple_vibe is not None
                )
            coverage_pct = (tracks_with_analysis / total_tracks * 100) if total_tracks > 0 else 0.0
        except Exception:
            coverage_pct = 0.0
        
        return {
            "available": self._analyzer is not None and self._analyzer.is_available(),
            "mode": self._analysis_mode,
            "workers_running": len(self._analysis_workers),
            "stats": stats,
            "success_rate": success_rate,
            "coverage_estimate": f"{coverage_pct:.1f}%",
        }
    
    def log_analysis_health(self) -> None:
        """Log current analysis worker pool health status."""
        health = self.get_analysis_health()
        
        if not health["available"]:
            LOG.info("📊 [Analysis Health] Service unavailable (using Gemini estimates)")
            return
        
        mode_str = "Non-ML (lightweight)" if health["mode"] == "non-ml" else f"ML ({self._embedding_model})"
        LOG.info(
            f"📊 [Analysis Health] Mode: {mode_str} | "
            f"Workers: {health['workers_running']} | "
            f"Queue: {health['stats']['queue_depth']} | "
            f"Success: {health['success_rate']:.1%} | "
            f"Coverage: {health['coverage_estimate']}"
        )
    
    async def parse_track(
        self, raw_title: str, channel_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Parse track metadata with the resilient waterfall and cached fallbacks.
        
        Uses parse_with_resilient_waterfall() for the multi-stage escalation,
        re-running it when cached entries are refreshed.
        """
        # Use YouTube ID as cache key (channel_name serves as secondary key)
        youtube_id = channel_name 
        
        async def _attempt_waterfall() -> Optional[Dict[str, Any]]:
            if not self._gemini.is_available:
                LOG.debug("Gemini unavailable; skipping resilient waterfall")
                return None
            try:
                return await self.parse_with_resilient_waterfall(
                    raw_title,
                    youtube_id,
                    channel_name=channel_name,
                )
            except Exception as exc:
                LOG.warning(
                    "⚠️ Waterfall parse failed: %s",
                    exc,
                )
                return None

        parsed = await _attempt_waterfall()
        if parsed:
            return parsed

        # Fallback with cached parsing
        cached = await self._cache.get_parsing(raw_title, channel_name)
        if cached:
            schema_version = getattr(cached, "schema_version", 1)
            schema_mismatch = schema_version < PARSING_SCHEMA_VERSION
            heuristic_refresh = self._should_refresh_parsing_cache(
                raw_title,
                channel_name,
                cached,
            )
            needs_refresh = schema_mismatch or heuristic_refresh

            if not needs_refresh:
                self._log_verbose(
                    2,
                    "📁 [Cache Hit: Parsing] %r / %r -> artist=%s, title=%s, track_type=%s, entity=%s",
                    raw_title[:50] + "..." if len(raw_title) > 50 else raw_title,
                    (
                        channel_name[:30] + "..."
                        if len(channel_name) > 30
                        else channel_name
                    ),
                    cached.artist,
                    cached.title,
                    cached.track_type,
                    cached.primary_entity or "N/A",
                )
                return {
                    "artist": cached.artist,
                    "title": cached.title,
                    "confidence": cached.confidence,
                    "track_type": cached.track_type,
                    "primary_entity": cached.primary_entity,
                }

            reason = "schema_version" if schema_mismatch else "ost_heuristic"
            self._log_verbose(
                2,
                "♻️ [Parsing Refresh] Re-parsing %r / %r due to %s mismatch",
                raw_title[:50] + "..." if len(raw_title) > 50 else raw_title,
                (
                    channel_name[:30] + "..."
                    if len(channel_name) > 30
                    else channel_name
                ),
                reason,
            )

            # Drop stale parsing result so we can refresh with updated prompt logic
            await self._cache.delete_parsing(raw_title, channel_name)
            parsed = await _attempt_waterfall()
            if parsed:
                return parsed

        if self._gemini.is_available:
            LOG.warning(
                "Gemini unable to parse track metadata (title=%r, channel=%r)",
                raw_title,
                channel_name,
            )
        return None

    def _should_refresh_parsing_cache(
        self,
        raw_title: str,
        channel_name: str,
        cached: ParsingEntry,
    ) -> bool:
        """Heuristic to invalidate stale parsing entries after prompt updates."""
        if cached.track_type != "music":
            return False

        if getattr(cached, "primary_entity", None):
            return False

        text = f"{raw_title} {channel_name}".lower()
        ost_tokens = (
            " ost",
            "official soundtrack",
            "original soundtrack",
            "soundtrack",
            "score",
            "opening",
            "ending theme",
            "bgm",
        )
        if any(token in text for token in ost_tokens):
            return True

        artist = (cached.artist or "").lower().strip()
        if artist.endswith(" cast") or artist.endswith(" ost"):
            return True

        channel = channel_name.lower().strip()
        if channel in {"prime video", "netflix", "crunchyroll"}:
            return True

        return False

    async def enrich_track(
        self,
        artist: str,
        title: str,
        existing_tags: Optional[List[str]] = None,
        *,
        require_grounding: bool = False,
        youtube_id: Optional[str] = None,
        deezer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        cached = await self._cache.get_enrichment(artist, title)
        track_key = self._make_track_key(artist, title)
        if cached:
            # No vector synthesis yet if EfficientAT analysis has not completed
            artist_safe = artist[:30] + "..." if len(artist) > 30 else artist
            title_safe = title[:40] + "..." if len(title) > 40 else title
            self._log_verbose(
                2,
                "📁 [Cache Hit: Enrichment] %s - %s -> tags=%s, vibe=%s, energy=%s",
                artist_safe,
                title_safe,
                cached.tags[:3],
                (cached.computed_simple_vibe[:3] if cached.computed_simple_vibe else []),
                cached.energy or "n/a",
            )
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "Enrichment cache hit: %s",
                    {
                        "artist": artist,
                        "title": title,
                        "tags": cached.tags[:5],
                        "mood": cached.mood,
                        "energy": cached.energy,
                        "computed_simple_vibe": cached.computed_simple_vibe,
                        "computed_loudness": cached.computed_loudness,
                        "computed_tempo": cached.computed_tempo,
                    },
                )
            return self._build_enrichment_response(track_key, cached)

        if not self._gemini.is_available:
            LOG.debug("Gemini unavailable; skipping enrichment")
            return None

        self._log_verbose(
            2,
            "🤖 [Gemini Enrichment] Fetching for %s - %s",
            artist[:30] + "..." if len(artist) > 30 else artist,
            title[:40] + "..." if len(title) > 40 else title,
        )

        try:
            response = await self._gemini.request_enrichment(
                artist,
                title,
                existing_tags or [],
                allow_grounding=require_grounding,
            )
        except Exception as exc:
            LOG.warning("Gemini enrichment failed: %s", exc)
            return None

        if not response:
            return None

        if self._verbose >= 2:
            LOG.debug(
                "[AutoplayV3][enrich_track] Gemini response for %s: tags=%s moods=%s energy=%s",
                f"{artist}::{title}",
                response.get("tags"),
                response.get("moods"),
                response.get("energy"),
            )

        def _safe_float(value: Any) -> Optional[float]:
            try:
                if value is None or value == "":
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        def _safe_int(value: Any) -> Optional[int]:
            try:
                if value is None or value == "":
                    return None
                return int(value)
            except (TypeError, ValueError):
                return None

        tags = [
            str(tag).lower() for tag in response.get("tags", []) if isinstance(tag, str)
        ]
        if existing_tags:
            for tag in existing_tags:
                if isinstance(tag, str):
                    normalized = tag.lower().strip()
                    if normalized and normalized not in tags:
                        tags.append(normalized)
        if tags:
            # Preserve first occurrence order while removing duplicates
            tags = list(dict.fromkeys(tags))

        moods = [
            str(mood).strip()
            for mood in response.get("moods", [])
            if isinstance(mood, str) and mood.strip()
        ]
        mood_value = moods[0] if moods else None
        energy = (
            response.get("energy") if isinstance(response.get("energy"), str) else None
        )

        raw_genres = response.get("genres")
        genres: List[str] = []
        if isinstance(raw_genres, list):
            genres = [str(g).strip() for g in raw_genres if str(g).strip()]
        elif isinstance(raw_genres, str):
            genre_text = raw_genres.strip()
            if genre_text:
                genres = [genre_text]
        elif isinstance(response.get("genre"), str):
            genre_text = response.get("genre", "").strip()
            if genre_text:
                genres = [genre_text]

        # V3: Extract mood text from response (cultural context only)
        mood_vector_data = response.get("mood_vector")
        if isinstance(mood_vector_data, dict):
            raw_mood_label = mood_vector_data.get("mood")
            if not mood_value and isinstance(raw_mood_label, str):
                stripped = raw_mood_label.strip()
                if stripped:
                    mood_value = stripped

        # Gemini cannot provide computed_* fields—only the EfficientAT analysis pipeline can.
        # Leave them as None so the background analyzer fills them in once audio processing finishes.

        bpm_val = response.get("bpm")
        key_val = response.get("key")
        activity_affinity_val = response.get("activity_affinity")
        emotional_intensity_val = response.get("emotional_intensity")
        daypart_affinity_val = response.get("daypart_affinity")

        entry = EnrichmentEntry(
            tags=tags,
            mood=mood_value,
            fetched_at=time.time(),
            bpm=_safe_int(bpm_val),
            key=str(key_val).strip() if isinstance(key_val, str) and key_val.strip() else None,
            activity_affinity=(
                str(activity_affinity_val).strip()
                if isinstance(activity_affinity_val, str) and activity_affinity_val.strip()
                else None
            ),
            emotional_intensity=_safe_float(emotional_intensity_val),
            daypart_affinity=(
                str(daypart_affinity_val).strip()
                if isinstance(daypart_affinity_val, str) and daypart_affinity_val.strip()
                else None
            ),
            genres=genres,
            computed_loudness=None,
            computed_tempo=None,
            computed_key=None,
            computed_mode=None,
        )

        # === AUDIO ANALYSIS PHASE ===
        # Queue Librosa+MobileNet analysis so flow/simple vibe fields fill in asynchronously
        # V3 Architecture: Quality > Speed - no Gemini estimates, wait for real analysis
        if youtube_id:
            queued = self.queue_audio_analysis(artist, title, youtube_id)
            if queued:
                self._log_verbose(
                    2,
                    "📥 [Analysis Queue] Queued background analysis for %s - %s (youtube_id=%s)",
                    artist[:30] + "..." if len(artist) > 30 else artist,
                    title[:40] + "..." if len(title) > 40 else title,
                    youtube_id,
                )
        
        # === DEEZER FALLBACK PHASE ===
        # If EfficientAT analysis is unavailable but we have deezer_id, fetch BPM/gain from Deezer
        deezer_details = None
        if deezer_id and DeezerClient:
            try:
                self._log_verbose(
                    2,
                    "🎧 [Deezer Fallback] Fetching track details for %s - %s (deezer_id=%s)",
                    artist[:30] + "..." if len(artist) > 30 else artist,
                    title[:40] + "..." if len(title) > 40 else title,
                    deezer_id,
                )

                async with DeezerClient() as deezer_client:
                    deezer_details = await deezer_client.get_track_details(deezer_id)

                if deezer_details:
                    self._log_verbose(
                        2,
                        "✅ [Deezer Fallback] Retrieved -> BPM=%s, Gain=%.2f dB",
                        deezer_details.get("bpm", "N/A"),
                        deezer_details.get("gain", 0.0),
                    )
            except Exception as exc:
                LOG.warning("Deezer fallback failed for %s - %s: %s", artist, title, exc)
        elif deezer_id and not DeezerClient:
            LOG.debug("Deezer fallback skipped (aiohttp dependency missing)")
        
        # === DEEZER METADATA INTEGRATION ===
        # Use Deezer BPM/gain as supplementary data (does NOT override EfficientAT results)
        if deezer_details:
            # Store Deezer BPM in entry.bpm (Gemini estimate field)
            # Workers will override with computed_tempo when EfficientAT completes
            if deezer_details.get("bpm") and not entry.bpm:
                entry.bpm = int(deezer_details["bpm"])
            
            if self._verbose >= 2:
                LOG.debug(
                    "[AutoplayV3][enrich_track] Deezer metadata: bpm=%s, gain=%.2f",
                    entry.bpm or "N/A",
                    deezer_details.get("gain", 0.0),
                )

        # No vector synthesis: if computed_simple_vibe is None it means
        # 1. EfficientAT analysis is still running (background job queued/pending)
        # 2. No youtube_id/preview available (cannot analyze)
        # 3. Audio download failed (will retry in background)
        # Recommendation system must handle None gracefully (use collaborative filtering)

        await self._cache.set_enrichment(artist, title, entry)
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(
                "Enrichment stored: %s",
                {
                    "artist": artist,
                    "title": title,
                    "tags": tags[:5],
                    "mood": mood_value,
                    "computed_simple_vibe": entry.computed_simple_vibe,
                    "genres": entry.genres,
                    "computed_loudness": entry.computed_loudness,
                    "computed_tempo": entry.computed_tempo,
                },
            )

        return self._build_enrichment_response(track_key, entry)

    async def hydrate_collaborative(self, source: Optional[Path | str] = None) -> bool:
        matrix = self._collaborative
        if source is not None:
            return await matrix.load_from_file(source, persist=True)
        return await matrix.hydrate()

    def _build_enrichment_response(
        self,
        track_key: str,
        entry: EnrichmentEntry,
    ) -> Dict[str, Any]:
        """
        Normalize enrichment payload for downstream consumers.
        """
        return {
            "tags": list(entry.tags),
            "mood": entry.mood,
            "mood_vector": self._build_mood_payload(entry, track_key=track_key),
            "bpm": entry.bpm,
            "key": entry.key,
            "activity_affinity": entry.activity_affinity,
            "emotional_intensity": entry.emotional_intensity,
            "daypart_affinity": entry.daypart_affinity,
            "genres": list(entry.genres),
            "computed_simple_vibe": list(entry.computed_simple_vibe or []),
            "computed_embedding": list(entry.computed_embedding or []),
            "computed_embedding_model": entry.computed_embedding_model,
            "computed_embedding_dim": entry.computed_embedding_dim,
            "computed_loudness": entry.computed_loudness,
            "computed_tempo": entry.computed_tempo,
            "computed_key": entry.computed_key,
            "computed_mode": entry.computed_mode,
            "analysis_verified": entry.analysis_verified,
            "analysis_in_progress": entry.analysis_in_progress,
            "last_analysis_attempt": entry.last_analysis_attempt,
        }

    def _build_mood_payload(
        self,
        entry: EnrichmentEntry,
        *,
        track_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Translate an enrichment entry into the unified 5D vibe payload.
        
        V3 Architecture: Only uses computed_simple_vibe from Librosa/MobileNet analysis.
        Returns None if audio analysis hasn't been completed yet (Quality > Speed).
        """
        if not entry.computed_simple_vibe:
            return None  # No Gemini fallback - wait for audio analysis

        vector = list(entry.computed_simple_vibe)
        
        payload: Dict[str, Any] = {
            "vector": vector,
            "dimensions": [
                "energy",
                "valence",
                "danceability",
                "acousticness",
                "brightness",
            ],
            "energy": vector[0] if len(vector) > 0 else None,
            "valence": vector[1] if len(vector) > 1 else None,
            "danceability": vector[2] if len(vector) > 2 else None,
            "acousticness": vector[3] if len(vector) > 3 else None,
            "brightness": vector[4] if len(vector) > 4 else None,
            "mood": entry.mood,
            "source": "analysis",
        }

        if track_key:
            payload["id"] = track_key

        return payload

    def score_candidates(
        self,
        guild_id: int | str,
        candidates: Sequence[CandidateFeatures],
        *,
        seed_track_ids: Optional[Iterable[str]] = None,
        session_mood_vector: Optional[Sequence[float]] = None,
        target_mood: Optional[str] = None,
        # Phase 3: Enhanced scoring parameters
        session_focus_genres: Optional[List[str]] = None,
        liked_mood_vector: Optional[Sequence[float]] = None,
        energy_trend: float = 0.0,
        last_energy: Optional[float] = None,
    ) -> List[ScoredCandidate]:
        return self._recommender.score_candidates(
            guild_id,
            candidates,
            seed_track_ids=seed_track_ids,
            session_mood_vector=session_mood_vector,
            target_mood=target_mood,
            session_focus_genres=session_focus_genres,
            liked_mood_vector=liked_mood_vector,
            energy_trend=energy_trend,
            last_energy=last_energy,
        )

    def get_stats(self) -> Dict[str, Any]:
        availability = self.availability_report()
        cache_stats = self._cache.get_cache_stats()
        collaborative_metadata = self._collaborative.snapshot_metadata()
        recommender_settings = self._recommender.snapshot_settings()
        feedback_stats = self._feedback.get_buffer_stats()
        gemini_stats = {
            "available": self._gemini.is_available,
            "status": self._gemini.status,
            "quota_remaining": self._gemini.quota_remaining,
        }

        return {
            "availability": {
                "lastfm": availability.lastfm,
                "gemini": availability.gemini,
                "status": availability.status,
            },
            "gemini": gemini_stats,
            "cache": cache_stats,
            "collaborative": {
                "matrix": collaborative_metadata,
                "recommender": recommender_settings,
            },
            "feedback": feedback_stats,
        }

    async def resolve_track(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
        prefer_cache: bool = True,
    ) -> Optional[Any]:
        if self._verbose >= 2:
            LOG.debug(
                "[AutoplayV3][resolve_track] resolving %s (duration=%s)",
                f"{artist}::{title}",
                expected_duration_ms,
            )
        return await self._track_resolver.resolve_track(
            artist,
            title,
            expected_duration_ms=expected_duration_ms,
            prefer_cache=prefer_cache,
        )

    async def record_feedback_event(
        self,
        *,
        guild_id: Optional[int | str],
        user_id: Optional[int | str],
        track_id: str,
        event_type: str,
        timestamp: Optional[float] = None,
        session_id: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self._log_verbose(
            2,
            "[AutoplayV3][record_feedback_event] guild=%s user=%s track=%s event=%s metadata_keys=%s",
            guild_id,
            user_id,
            track_id,
            event_type,
            list((metadata or {}).keys()),
            log_fn=LOG.debug,
        )
        return await self._feedback.record_event(
            guild_id=guild_id,
            user_id=user_id,
            track_id=track_id,
            event_type=event_type,
            timestamp=timestamp,
            session_id=session_id,
            source=source,
            metadata=metadata,
        )

    async def clear_guild_session(self, guild_id: int) -> bool:
        return await self._feedback.clear_guild_session(guild_id)

    async def parse_with_resilient_waterfall(
        self,
        youtube_title: str,
        youtube_id: str,
        channel_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self._track_resolver.parse_youtube_to_deezer(
            youtube_title,
            youtube_id,
            channel_name=channel_name,
            verbose=self._verbose,
        )

    async def get_mood_vector(
        self,
        artist: str,
        title: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the 5D vibe vector for a track from cache. 
        V3 Architecture: Only returns vectors computed by Librosa/MobileNet audio analysis.
        """
        key = self._make_track_key(artist, title)
        entry = await self._cache.get_enrichment(artist, title)
        if not entry:
            return None
        return self._build_mood_payload(entry, track_key=key)

    @staticmethod
    def _make_track_key(artist: str, title: str) -> str:
        return f"{artist.lower().strip()}::{title.lower().strip()}"

    @staticmethod
    def _split_track_key(track_key: str) -> tuple[str, str]:
        """Split normalized track key back into artist and title components."""
        if not track_key:
            return "", ""
        if "::" in track_key:
            artist, title = track_key.split("::", 1)
        elif "|" in track_key:
            artist, title = track_key.split("|", 1)
        else:
            artist, title = track_key, ""
        return artist.strip(), title.strip()
