import asyncio
import logging
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
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

try:
    from .preview_fetcher import PreviewFetcher
except ImportError:
    PreviewFetcher = None  # type: ignore

try:
    from .enriching_service import EnrichingService
except ImportError:
    EnrichingService = None

try:
    from .audio_downloader import AudioDownloader
except ImportError:
    AudioDownloader = None

try:
    from .deezer_fetch import DeezerClient
except ImportError:
    DeezerClient = None  # type: ignore

LOG = logging.getLogger(__name__)

# Verbosity control: prefer AUTOPLAY_V3_VERBOSITY but honor legacy env for compatibility.
DEFAULT_VERBOSITY = int(
    os.getenv("AUTOPLAY_V3_VERBOSITY", os.getenv("AUTOPLAY_V2_VERBOSITY", "0"))
)
LASTFM_API_KEY_ENV = "LASTFM_API_KEY"
PARSING_SCHEMA_VERSION = 2

# V3 Configuration: Read from environment variables with sensible defaults
DEFAULT_ANALYSIS_MODE = os.getenv("ANALYSIS_MODE", "ml").lower()  # "ml" or "non-ml"
DEFAULT_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "mn10_as").lower()
MAX_ANALYSIS_RETRIES = 3

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
        self._cache = CacheManager(cache_path)
        self._gemini = gemini_service or GeminiService(cache_dir=cache_path)
        self._feedback = feedback_manager or FeedbackManager(cache_dir=cache_path)
        self._collaborative = collaborative_matrix or CollaborativeMatrix(self._cache)
        self._recommender = ContextualRecommender(self._collaborative)
        self._track_resolver = track_resolver or TrackResolver(
            self._cache, self._gemini
        )
        self._lastfm_key = os.getenv(LASTFM_API_KEY_ENV, "").strip()
        
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

        self._audio_downloader = None
        if AudioDownloader and self._analyzer and self._analyzer.is_available():
            try:
                self._audio_downloader = AudioDownloader()
            except Exception as exc:
                LOG.warning("⚠️ Audio downloader unavailable: %s", exc)
        
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

        self._restore_persistent_queue()
        
        if self._analyzer and self._analyzer.is_available():
            mode_str = f"Non-ML mode (lightweight)" if analysis_mode == "non-ml" else f"ML mode ({embedding_model})"
            LOG.info(f"✅ Audio analysis enabled: {mode_str} with {analysis_workers} workers")
        else:
            LOG.info("ℹ️ Audio analysis disabled (using Gemini estimates)")

        # instance verbosity (0 = off, 1 = debug, 2 = very verbose)
        self._verbose = DEFAULT_VERBOSITY
        if self._verbose:
            # Configure logging for autoplay V3 modules only (not root logger)
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
            ]

            # Add console handler to each autoplay logger if not present
            for logger_name in autoplay_loggers:
                logger = logging.getLogger(logger_name)
                logger.setLevel(logging.DEBUG)

                # Only add handler if this logger doesn't have one
                if not logger.handlers:
                    handler = logging.StreamHandler()
                    handler.setLevel(logging.DEBUG)
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

        if not self._lastfm_key:
            LOG.warning(
                "Last.fm API key missing. Populate LASTFM_API_KEY in .env (see modules/enviromentfilegenerator.py)."
            )
        if not self._gemini.is_available:
            LOG.warning(
                "Gemini unavailable during AutoplayEngineV3 init: %s",
                self._gemini.status,
            )

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

    def _log_queue_update(self, track_id: str) -> None:
        if self._verbose < 1:
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
        if self._verbose < 1:
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
            "👷 Worker %d working on %s (%d/%d songs, %s)",
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
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        job: Dict[str, Any] = {
            "track_id": track_id,
            "youtube_url": self._normalize_youtube_url(youtube_url),
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
                restored.append(self._build_analysis_job(track_id, youtube_url, job))
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

    # ========== AUDIO ANALYSIS WORKER POOL ==========
    
    async def start_analysis_workers(self) -> None:
        """Start background audio analysis workers (V3)."""
        if not self._analyzer or not self._analyzer.is_available():
            LOG.debug("Audio analyzer not available, skipping worker startup")
            return
        if not (self._preview_fetcher or self._audio_downloader):
            LOG.warning("⚠️ No audio ingest source available; cannot start analysis workers")
            return
            
        if self._analysis_workers:
            LOG.debug("Analysis workers already running")
            return
        
        mode_str = "Non-ML" if self._analysis_mode == "non-ml" else "ML"
        source_hint = "Deezer previews" if self._preview_fetcher else "YouTube fallback"
        LOG.info(
            f"🚀 Starting {self._analysis_worker_count} {mode_str} analysis workers ({source_hint})"
        )
        self._analysis_shutdown = False
        for i in range(self._analysis_worker_count):
            task = asyncio.create_task(self._analysis_worker_loop(worker_id=i))
            self._analysis_workers.append(task)
        
    
    async def stop_analysis_workers(self) -> None:
        """Gracefully shutdown analysis workers."""
        if not self._analysis_workers:
            return
        
        LOG.info(f"🛑 Stopping {len(self._analysis_workers)} analysis workers...")
        self._analysis_shutdown = True
        
        # Wait for workers to finish current jobs
        await asyncio.gather(*self._analysis_workers, return_exceptions=True)
        self._analysis_workers.clear()
        self._active_jobs.clear()
        self._analysis_stats["in_progress"] = 0
        self._persist_analysis_queue_state()
        LOG.info("✅ Analysis workers stopped")
    

    def queue_analysis(self, track_id: str, youtube_url: str) -> bool:
        """Queue a track for background analysis using Deezer preview or YouTube fallback.

        Args:
            track_id: Normalized ``artist::title`` cache key.
            youtube_url: YouTube URL used when no Deezer preview is available.

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
        has_preview_source = bool(preview_url and self._preview_fetcher)
        has_youtube_source = bool(self._audio_downloader and youtube_url)
        if not (has_preview_source or has_youtube_source):
            self._log_verbose(
                1,
                "[Analysis Queue] No ingest source for %s (preview=%s, youtube=%s)",
                track_id,
                bool(preview_url),
                bool(youtube_url),
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
        )
        self._analysis_queue.append(job)
        self._analysis_stats["queued"] += 1
        self._analysis_stats["queue_depth"] = len(self._analysis_queue)
        self._persist_analysis_queue_state()
        self._log_queue_update(track_id)
        return True
    

    def queue_audio_analysis(self, artist: str, title: str, youtube_id: str) -> bool:
        """Helper to enqueue analysis using a bare YouTube video ID."""
        if not youtube_id:
            return False
        track_id = self._make_track_key(artist, title)
        if track_id not in self._cache._enrichment_cache:
            return False
        url = youtube_id
        if not youtube_id.startswith("http"):
            url = f"https://www.youtube.com/watch?v={youtube_id}"
        return self.queue_analysis(track_id, url)

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

            if self.queue_analysis(track_key, youtube_source):
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
    
    async def _analysis_worker_loop(self, worker_id: int) -> None:
        """Background worker that processes audio analysis queue (V3).

        Supports both ML mode (EfficientAT embeddings) and Non-ML mode (Librosa-only).
        
        Args:
            worker_id: Unique worker identifier for logging
        """
        mode_str = "Non-ML" if self._analysis_mode == "non-ml" else "ML"
        LOG.info(f"👷 Analysis worker {worker_id} ({mode_str}) started")
        
        while not self._analysis_shutdown:
            try:
                if not (self._preview_fetcher or self._audio_downloader):
                    LOG.warning(
                        f"[Worker {worker_id}] No audio ingest source available; shutting down worker"
                    )
                    break
                # Check for work
                if not self._analysis_queue:
                    await asyncio.sleep(1)  # Idle wait
                    continue
                
                # Get next job
                job = self._analysis_queue.popleft()
                self._analysis_stats["queue_depth"] = len(self._analysis_queue)
                self._analysis_stats["in_progress"] += 1
                track_id = job.get("track_id")
                artist_from_key, title_from_key = self._split_track_key(track_id or "")
                youtube_url = job.get("youtube_url")
                job_start = time.time()
                self._active_jobs[worker_id] = job
                self._persist_analysis_queue_state()
                self._log_worker_progress(worker_id, track_id)
                
                # Download audio (prefer Deezer preview, fallback to YouTube)
                audio_path = None
                cleanup_parent = False
                preview_url = job.get("preview_url")
                preview_duration_ms = job.get("preview_duration_ms")
                try:
                    preview_duration_ms = int(preview_duration_ms)
                except (TypeError, ValueError):
                    preview_duration_ms = None
                audio_source = "preview" if preview_url else "youtube"
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
                                "[Worker %d] Deezer preview unavailable for %s",
                                worker_id,
                                track_id or "unknown",
                            )

                    if not audio_path:
                        if not self._audio_downloader:
                            raise RuntimeError("Preview unavailable and downloader missing")
                        audio_path = await self._audio_downloader.download_audio(youtube_url)
                        cleanup_parent = True
                        audio_source = "youtube_fallback"

                    if not audio_path:
                        raise Exception("Audio ingest failed")

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
                            # Clear estimated guesses once verified
                            entry.estimated_simple_vibe = None
                            entry.estimated_tempo = None
                            entry.estimated_loudness = None
                            entry.estimated_key = None
                            entry.estimated_mode = None
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
                LOG.info(f"👷 Analysis worker {worker_id} cancelled")
                break
            except Exception as e:
                LOG.error(f"[Worker {worker_id}] Unexpected error: {e}")
                await asyncio.sleep(1)  # Prevent tight error loop
        
        LOG.info(f"👷 Analysis worker {worker_id} stopped")
    

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

        mood_vector_data = response.get("mood_vector")
        mood_energy = mood_valence = mood_tempo = mood_confidence = None
        if isinstance(mood_vector_data, dict):
            mood_energy = _safe_float(mood_vector_data.get("energy"))
            mood_valence = _safe_float(mood_vector_data.get("valence"))
            mood_tempo = _safe_float(mood_vector_data.get("tempo"))
            mood_confidence = _safe_float(mood_vector_data.get("confidence"))
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
            energy=energy,
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
            mood_energy=mood_energy,
            mood_valence=mood_valence,
            mood_tempo=mood_tempo,
            mood_confidence=mood_confidence,
            computed_loudness=None,
            computed_tempo=None,
            computed_key=None,
            computed_mode=None,
        )

        vibe_guess = response.get("simple_vibe_guess") or response.get("vibe_guess")
        if isinstance(vibe_guess, list):
            cleaned: List[float] = []
            for value in vibe_guess[:5]:
                try:
                    cleaned.append(max(0.0, min(1.0, float(value))))
                except (TypeError, ValueError):
                    cleaned = []
                    break
            if len(cleaned) == 5:
                entry.estimated_simple_vibe = cleaned

        # === AUDIO ANALYSIS PHASE ===
        # Queue Librosa+MobileNet analysis so flow/simple vibe fields fill in asynchronously
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
                    "energy": energy,
                    "computed_simple_vibe": entry.computed_simple_vibe,
                    "estimated_simple_vibe": entry.estimated_simple_vibe,
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
        """Normalize enrichment payload for downstream consumers."""
        # No vector synthesis - return None until EfficientAT finishes analyzing
        return {
            "tags": list(entry.tags),
            "mood": entry.mood,
            "energy": entry.energy,
            "mood_vector": self._build_mood_payload(entry, track_key=track_key),
            "bpm": entry.bpm,
            "key": entry.key,
            "activity_affinity": entry.activity_affinity,
            "emotional_intensity": entry.emotional_intensity,
            "daypart_affinity": entry.daypart_affinity,
            "genres": list(entry.genres),
            "computed_simple_vibe": list(entry.computed_simple_vibe or []),
            "estimated_simple_vibe": list(entry.estimated_simple_vibe or []),
            "computed_embedding": list(entry.computed_embedding or []),
            "computed_embedding_model": entry.computed_embedding_model,
            "computed_embedding_dim": entry.computed_embedding_dim,
            "gemini_simple_vibe": list(entry.estimated_simple_vibe or []),
            "computed_loudness": entry.computed_loudness,
            "computed_tempo": entry.computed_tempo,
            "computed_key": entry.computed_key,
            "computed_mode": entry.computed_mode,
            "estimated_tempo": entry.estimated_tempo,
            "estimated_loudness": entry.estimated_loudness,
            "estimated_key": entry.estimated_key,
            "estimated_mode": entry.estimated_mode,
            "mood_energy": entry.mood_energy,
            "mood_valence": entry.mood_valence,
            "mood_tempo": entry.mood_tempo,
            "mood_confidence": entry.mood_confidence,
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
        """Translate an enrichment entry into the unified 5D vibe payload."""
        vector = None
        vibe_source = "heuristic"
        if entry.computed_simple_vibe:
            vector = list(entry.computed_simple_vibe)
            vibe_source = "analysis"
        elif entry.estimated_simple_vibe:
            vector = list(entry.estimated_simple_vibe)
            vibe_source = "estimate"

        if not vector:
            return None

        payload: Dict[str, Any] = {
            "vector": vector,
            "dimensions": [
                "energy",
                "valence",
                "danceability",
                "acousticness",
                "instrumentalness",
            ],
            "energy": vector[0] if len(vector) > 0 else None,
            "valence": vector[1] if len(vector) > 1 else None,
            "danceability": vector[2] if len(vector) > 2 else None,
            "acousticness": vector[3] if len(vector) > 3 else None,
            "instrumentalness": vector[4] if len(vector) > 4 else None,
            "tempo": entry.mood_tempo,
            "confidence": entry.mood_confidence,
            "mood": entry.mood,
            "source": vibe_source,
            "legacy_energy": entry.mood_energy,
            "legacy_valence": entry.mood_valence,
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
        """
        Clear guild telemetry session when bot leaves VC.

        Returns:
            True if session was cleared successfully.
        """
        return await self._feedback.clear_guild_session(guild_id)

    async def parse_with_resilient_waterfall(
        self,
        youtube_title: str,
        youtube_id: str,
        channel_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Phase 0.5: Multi-stage resilient parsing with Deezer canonical verification.
        
        Stage 0: Cache check with self-healing TTL
        Stage 1: Flash-Lite → 9 Deezer queries (85% confidence threshold)
        Stage 2: Flash + Grounding + Thinking → 3 refined queries (75% threshold)
        Stage 3: Flash + Grounding + Thinking → Direct extraction (best_guess)
        Emergency: Regex fallback (retry_after_days=1)
        
        Args:
            youtube_title: Raw YouTube video title
            youtube_id: YouTube video ID for cache key
            channel_name: YouTube channel name (optional, used as fallback artist)
            
        Returns:
            Dictionary with artist, title, confidence, track_type, primary_entity, 
            is_canonical, is_best_guess, deezer_id (if verified)
        """
        import re
        if DeezerClient is None:
            LOG.warning(
                "Deezer client unavailable (install aiohttp to enable canonical parsing)"
            )
            return None
        
        # Stage 0: Cache check with self-healing TTL
        cached = await self._cache.get_parsing(youtube_title, youtube_id)
        if cached:
            # Backward compatibility: Check schema version for old cache entries
            schema_version = getattr(cached, "schema_version", 1)
            if schema_version < PARSING_SCHEMA_VERSION:
                self._log_verbose(
                    2,
                    "🔄 [Stage 0: Schema Upgrade] Old cache entry (v%s) for '%s...', refreshing with waterfall",
                    schema_version,
                    youtube_title[:50],
                )
                # Delete old entry and proceed with waterfall
                await self._cache.delete_parsing(youtube_title, youtube_id)
            else:
                # Check if cache entry has expired based on type
                should_refresh = False
                cache_type = "CANONICAL" if getattr(cached, "is_canonical", False) else \
                            "BEST_GUESS" if getattr(cached, "is_best_guess", False) else \
                            "FALLBACK"
                
                if cache_type == "CANONICAL":
                    # Canonical entries never expire (Deezer-verified)
                    should_refresh = False
                elif cache_type == "BEST_GUESS":
                    # Best guess entries expire after 7 days
                    should_refresh = cached.is_expired(7 * 24 * 3600)
                else:
                    # FALLBACK entries use adaptive TTL (retry_after_days)
                    retry_after_days = getattr(cached, "retry_after_days", 1)
                    if not isinstance(retry_after_days, (int, float)) or retry_after_days <= 0:
                        # Ensure a sane default even if old cache lacks this field
                        retry_after_days = 1
                    ttl_seconds = int(retry_after_days * 24 * 3600)
                    should_refresh = cached.is_expired(ttl_seconds)
                
                if not should_refresh:
                    self._log_verbose(
                        2,
                        "📁 [Stage 0: Cache Hit] %s entry for '%s...' -> %s - %s",
                        cache_type,
                        youtube_title[:50],
                        cached.artist,
                        cached.title,
                    )
                    return {
                        "artist": cached.artist,
                        "title": cached.title,
                        "confidence": cached.confidence,
                        "track_type": getattr(cached, "track_type", "music"),
                        "primary_entity": getattr(cached, "primary_entity", None),
                        "is_canonical": getattr(cached, "is_canonical", False),
                        "is_best_guess": getattr(cached, "is_best_guess", False),
                        "deezer_id": getattr(cached, "deezer_id", None),
                    }
                else:
                    self._log_verbose(
                        2,
                        "🔄 [Stage 0: Cache Expired] %s entry expired for '%s...', refreshing",
                        cache_type,
                        youtube_title[:50],
                    )
        
        self._log_verbose(
            2,
            "🔍 [Stage 0: Cache Miss] Starting waterfall for '%s...'",
            youtube_title[:60],
        )
        
        queries_response: Optional[Dict[str, Any]] = None
        grounded_response: Optional[Dict[str, Any]] = None

        # Initialize Deezer client with async context manager
        async with DeezerClient() as deezer_client:
            # Stage 1: Flash-Lite → 9 Deezer queries (85% threshold)
            if self._gemini.is_available:
                self._log_verbose(
                    2,
                    "⚡ [Stage 1: Flash-Lite] Generating 9 Deezer queries...",
                )
                
                try:
                    queries_response = await self._gemini.generate_deezer_queries_lite(youtube_title)
                    if queries_response and "queries" in queries_response:
                        queries = queries_response["queries"][:9]  # Ensure max 9
                        
                        if self._verbose >= 2:
                            LOG.debug(f"⚡ [Stage 1] Generated queries: {queries}")
                        
                        stage1_threshold = 0.85
                        # Try each query against Deezer
                        for idx, query in enumerate(queries, 1):
                            if self._verbose >= 2:
                                LOG.debug(f"⚡ [Stage 1] Query {idx}/9: '{query}'")
                            
                            # Search Deezer and get best match
                            # Compare results against the original YouTube title for validation
                            search_results = await deezer_client.search_track(query)
                            match = deezer_client.get_best_match(
                                search_results,
                                threshold=stage1_threshold,
                                expected_title=youtube_title,  # Use YouTube title for fuzzy validation
                            )
                            if match:
                                relaxed = match.confidence < stage1_threshold
                                status = "SUCCESS" if not relaxed else "RELAXED SUCCESS"
                                self._log_verbose(
                                    2,
                                    "✅ [Stage 1: %s] Deezer match on query %d/9: %s - %s (confidence=%.2f, deezer_id=%s)",
                                    status,
                                    idx,
                                    match.track.artist,
                                    match.track.title,
                                    match.confidence,
                                    match.track.id,
                                )
                                
                                # Cache as canonical (indefinite TTL)
                                entry = ParsingEntry(
                                    artist=match.track.artist,
                                    title=match.track.title,
                                    confidence=match.confidence,
                                    parsed_at=time.time(),
                                    track_type="music",  # Deezer verified = commercial music
                                    primary_entity=None,
                                    is_canonical=True,
                                    is_best_guess=False,
                                    deezer_id=match.track.id,
                                    schema_version=PARSING_SCHEMA_VERSION,
                                )
                                await self._cache.set_parsing(youtube_title, youtube_id, entry)
                                
                                return {
                                    "artist": match.track.artist,
                                    "title": match.track.title,
                                    "confidence": match.confidence,
                                    "track_type": "music",
                                    "primary_entity": None,
                                    "is_canonical": True,
                                    "is_best_guess": False,
                                    "deezer_id": match.track.id,
                                }
                        
                        self._log_verbose(
                            2,
                            "⚠️ [Stage 1: FAIL] All 9 queries failed to meet 85%% threshold",
                        )
                
                except Exception as exc:
                    LOG.warning(f"⚠️ [Stage 1: ERROR] Flash-Lite failed: {exc}")
            
            # Stage 2: Flash + Grounding + Thinking → 3 refined queries (75% threshold)
            if self._gemini.is_available and self._gemini.can_use_grounding():
                self._log_verbose(
                    2,
                    "🧠 [Stage 2: Flash+Grounding+Thinking] Generating 3 refined queries...",
                )
                
                try:
                    # Pass failed Stage 1 queries for context awareness
                    context = queries_response.get("queries", []) if queries_response else []
                    grounded_response = await self._gemini.generate_deezer_queries_grounded(
                        youtube_title, 
                        failed_queries=context[:3]  # Pass first 3 failed attempts
                    )
                    
                    if grounded_response and "queries" in grounded_response:
                        queries = grounded_response["queries"][:3]  # Max 3 refined
                        
                        if self._verbose >= 2:
                            reasoning = grounded_response.get("reasoning", "N/A")
                            LOG.debug(f"🧠 [Stage 2] Reasoning: {reasoning}")
                            LOG.debug(f"🧠 [Stage 2] Refined queries: {queries}")
                        
                        stage2_threshold = 0.75
                        for idx, query in enumerate(queries, 1):
                            if self._verbose >= 2:
                                LOG.debug(f"🧠 [Stage 2] Query {idx}/3: '{query}'")
                            
                            # Search Deezer and get best match
                            search_results = await deezer_client.search_track(query)
                            match = deezer_client.get_best_match(
                                search_results, 
                                threshold=stage2_threshold,
                                expected_title=youtube_title
                            )
                            if match:
                                relaxed = match.confidence < stage2_threshold
                                status = "SUCCESS" if not relaxed else "RELAXED SUCCESS"
                                self._log_verbose(
                                    2,
                                    "✅ [Stage 2: %s] Deezer match on query %d/3: %s - %s (confidence=%.2f, deezer_id=%s)",
                                    status,
                                    idx,
                                    match.track.artist,
                                    match.track.title,
                                    match.confidence,
                                    match.track.id,
                                )
                                
                                # Cache as canonical (indefinite TTL)
                                entry = ParsingEntry(
                                    artist=match.track.artist,
                                    title=match.track.title,
                                    confidence=match.confidence,
                                    parsed_at=time.time(),
                                    track_type="music",
                                    primary_entity=None,
                                    is_canonical=True,
                                    is_best_guess=False,
                                    deezer_id=match.track.id,
                                    schema_version=PARSING_SCHEMA_VERSION,
                                )
                                await self._cache.set_parsing(youtube_title, youtube_id, entry)
                                
                                return {
                                    "artist": match.track.artist,
                                    "title": match.track.title,
                                    "confidence": match.confidence,
                                    "track_type": "music",
                                    "primary_entity": None,
                                    "is_canonical": True,
                                    "is_best_guess": False,
                                    "deezer_id": match.track.id,
                                }
                        
                        self._log_verbose(
                            2,
                            "⚠️ [Stage 2: FAIL] All 3 grounded queries failed to meet 75%% threshold",
                        )
                
                except Exception as exc:
                    LOG.warning(f"⚠️ [Stage 2: ERROR] Flash+Grounding failed: {exc}")
            elif self._verbose:
                grounding_status = "quota exhausted" if not self._gemini.can_use_grounding() else "Gemini unavailable"
                self._log_verbose(
                    2,
                    "⏭️ [Stage 2: SKIPPED] %s",
                    grounding_status,
                )
            
            # Stage 3: Flash + Grounding + Thinking → Direct extraction (best_guess)
            if self._gemini.is_available and self._gemini.can_use_grounding():
                self._log_verbose(
                    2,
                    "🎯 [Stage 3: Fallback Extraction] Extracting metadata directly...",
                )
                
                try:
                    # Collect all failed queries for context
                    failed_queries_context = []
                    if queries_response and "queries" in queries_response:
                        failed_queries_context.extend(queries_response["queries"][:9])
                    if grounded_response and "queries" in grounded_response:
                        failed_queries_context.extend(grounded_response["queries"][:3])
                    
                    fallback_response = await self._gemini.generate_fallback_metadata(
                        youtube_title,
                        failed_queries=failed_queries_context[:5]  # Pass top 5 failures
                    )
                    
                    if fallback_response:
                        artist = fallback_response.get("artist", "").strip()
                        title = fallback_response.get("title", "").strip()
                        track_type = fallback_response.get("track_type", "music")
                        primary_entity = fallback_response.get("primary_entity")
                        
                        if artist and title:
                            reasoning = fallback_response.get("reasoning", "N/A")
                            self._log_verbose(
                                2,
                                "✅ [Stage 3: SUCCESS] Extracted: %s - %s (track_type=%s, entity=%s)",
                                artist,
                                title,
                                track_type,
                                primary_entity or "N/A",
                            )
                            self._log_verbose(
                                2,
                                "🎯 [Stage 3] Reasoning: %s",
                                reasoning,
                                log_fn=LOG.debug,
                            )
                            
                            # Cache as best_guess (7-day TTL)
                            entry = ParsingEntry(
                                artist=artist,
                                title=title,
                                confidence=0.65,  # Medium confidence for unverified
                                parsed_at=time.time(),
                                track_type=track_type,
                                primary_entity=primary_entity,
                                is_canonical=False,
                                is_best_guess=True,
                                retry_after_days=7,  # Retry after 7 days
                                schema_version=PARSING_SCHEMA_VERSION,
                            )
                            await self._cache.set_parsing(youtube_title, youtube_id, entry)
                            
                            return {
                                "artist": artist,
                                "title": title,
                                "confidence": 0.65,
                                "track_type": track_type,
                                "primary_entity": primary_entity,
                                "is_canonical": False,
                                "is_best_guess": True,
                                "deezer_id": None,
                            }
                
                except Exception as exc:
                    LOG.warning(f"⚠️ [Stage 3: ERROR] Fallback extraction failed: {exc}")
            elif self._verbose:
                grounding_status = "quota exhausted" if not self._gemini.can_use_grounding() else "Gemini unavailable"
                self._log_verbose(
                    2,
                    "⏭️ [Stage 3: SKIPPED] %s",
                    grounding_status,
                )
            
            # Emergency: Regex fallback (retry after 1 day)
            self._log_verbose(
                2,
                "🚨 [Emergency: Regex Fallback] All AI stages failed, using regex...",
                log_fn=LOG.warning,
            )
            
            # Simple regex to extract "Artist - Title" or "Title by Artist"
            patterns = [
                r"^(.+?)\s*[-:]\s*(.+?)(?:\s*\[.*\]|\s*\(.*\)|$)",  # "Artist - Title [...]" or "Artist: Title"
                r"^(.+?)\s+by\s+(.+?)(?:\s*\[.*\]|\s*\(.*\)|$)",  # "Title by Artist"
            ]
            
            # Words that commonly appear in titles but are not artist names
            suspicious_artist_words = {
                "song", "theme", "music", "soundtrack", "ost", "opening", "ending",
                "cover", "remix", "version", "feat", "ft", "lyric", "lyrics",
                "official", "audio", "video", "mv", "full"
            }
            
            extracted_artist = None
            extracted_title = None
            
            for pattern in patterns:
                match = re.match(pattern, youtube_title, re.IGNORECASE)
                if match:
                    potential_artist = match.group(1).strip()
                    potential_title = match.group(2).strip()
                    
                    if potential_artist and potential_title:
                        # Check if the "artist" looks suspicious (likely part of title)
                        artist_words = set(potential_artist.lower().split())
                        is_suspicious = bool(artist_words & suspicious_artist_words)
                        
                        # Also check if artist is just a single common word
                        if len(artist_words) == 1 and potential_artist.lower() in suspicious_artist_words:
                            is_suspicious = True
                        
                        if is_suspicious:
                            if self._verbose >= 2:
                                LOG.debug(
                                    f"🔍 [Regex] Rejected suspicious artist '{potential_artist}', "
                                    f"using channel name as fallback"
                                )
                            # Use channel name as artist if available
                            if channel_name:
                                extracted_artist = channel_name
                                # Clean the title by removing the suspicious part
                                extracted_title = youtube_title.split(':', 1)[-1].split('-', 1)[-1].strip()
                                if not extracted_title:
                                    extracted_title = potential_title
                            else:
                                # No channel name available, skip this match
                                extracted_artist = None
                                extracted_title = None
                        else:
                            extracted_artist = potential_artist
                            extracted_title = potential_title
                        break
            
            # If regex found something valid, use it
            if extracted_artist and extracted_title:
                self._log_verbose(
                    2,
                    "🚨 [Emergency: Regex] Extracted: %s - %s (retry after 1 day)",
                    extracted_artist,
                    extracted_title,
                    log_fn=LOG.warning,
                )
                
                # Cache as fallback (1-day TTL for quota_exhausted)
                entry = ParsingEntry(
                    artist=extracted_artist,
                    title=extracted_title,
                    confidence=0.3,  # Low confidence for regex
                    parsed_at=time.time(),
                    track_type="music",
                    primary_entity=None,
                    is_canonical=False,
                    is_best_guess=False,
                    failure_reason="quota_exhausted",
                    retry_after_days=1,  # Retry tomorrow when quota resets
                    schema_version=PARSING_SCHEMA_VERSION,
                )
                await self._cache.set_parsing(youtube_title, youtube_id, entry)
                
                return {
                    "artist": extracted_artist,
                    "title": extracted_title,
                    "confidence": 0.3,
                    "track_type": "music",
                    "primary_entity": None,
                    "is_canonical": False,
                    "is_best_guess": False,
                    "deezer_id": None,
                }
            
            # Total failure - return None
            LOG.error(f"❌ [TOTAL FAILURE] Could not parse '{youtube_title[:60]}...'")
            return None

    async def get_mood_vector(
        self,
        artist: str,
        title: str,
        *,
        tags: Optional[Sequence[str]] = None,
        genre: Optional[str] = None,
        description: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        key = self._make_track_key(artist, title)
        entry = await self._cache.get_enrichment(artist, title)
        if entry and not force_refresh:
            return self._build_mood_payload(entry, track_key=key)

        if not self._gemini.is_available:
            LOG.debug("Gemini unavailable; skipping mood vector classification")
            return self._build_mood_payload(entry, track_key=key) if entry else None

        def _safe_float(value: Any, fallback: float = 0.5) -> float:
            try:
                if value is None or value == "":
                    return fallback
                return float(value)
            except (TypeError, ValueError):
                return fallback

        metadata_tags: List[str] = []
        if tags:
            metadata_tags.extend(str(tag) for tag in tags if isinstance(tag, str))
        if entry:
            metadata_tags.extend(entry.tags)
        if metadata_tags:
            metadata_tags = list(dict.fromkeys(metadata_tags))

        metadata = {
            "tags": metadata_tags,
            "genre": genre or (entry.genres[0] if entry and entry.genres else ""),
            "description": description or "",
        }

        try:
            response = await self._gemini.classify_mood_vector(metadata)
        except Exception as exc:
            LOG.warning("Gemini mood classification failed: %s", exc)
            return self._build_mood_payload(entry, track_key=key) if entry else None

        if not response:
            return self._build_mood_payload(entry, track_key=key) if entry else None

        energy = _safe_float(response.get("energy"))
        valence = _safe_float(response.get("valence"))
        tempo_norm = _safe_float(response.get("tempo"))
        confidence = _safe_float(response.get("confidence"))
        mood_label = str(response.get("mood", "")).strip() or None

        if entry:
            entry.mood_energy = energy
            entry.mood_valence = valence
            entry.mood_tempo = tempo_norm
            entry.mood_confidence = confidence
            if mood_label and not entry.mood:
                entry.mood = mood_label
            # NO vector synthesis - only background analysis can fill computed_simple_vibe
            await self._cache.set_enrichment(artist, title, entry)
            payload = self._build_mood_payload(entry, track_key=key)
            if payload is not None:
                payload.setdefault("source", "gemini")
                payload["confidence"] = confidence
                payload["tempo"] = tempo_norm
                payload["mood"] = mood_label or payload.get("mood")
            return payload

        vibe_vector = [energy, valence, 0.5, 0.5, 0.5]
        payload = {
            "id": key,
            "vector": vibe_vector,
            "dimensions": [
                "energy",
                "valence",
                "danceability",
                "acousticness",
                "instrumentalness",
            ],
            "energy": energy,
            "valence": valence,
            "danceability": vibe_vector[2],
            "acousticness": vibe_vector[3],
            "instrumentalness": vibe_vector[4],
            "tempo": tempo_norm,
            "confidence": confidence,
            "mood": mood_label,
            "source": "gemini",
        }
        return payload

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
