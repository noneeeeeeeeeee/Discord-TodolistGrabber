"""Autoplay Engine V3 entry point.

This module exposes ``LastFMAutoplayV3`` which mirrors the public surface of the
legacy autoplay implementation so existing callers can toggle engines through
``Autoplay_Engine.config`` without additional glue code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, cast

import aiohttp

from .config import LASTFM_API_KEY_ENV, PRIORITY_ACTIVE, PRIORITY_BUFFER, PRIORITY_DAYDREAM
from .autoplayengine_v3 import AutoplayEngineV3
from .contextual_recommender import CandidateFeatures
from .context_tracker import ContextTracker
from .novelty_controller import NoveltyController, NoveltyConfig

LOG = logging.getLogger(__name__)

_LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
_DEFAULT_FETCH_LIMIT = 100  # Increased from 50 to get more diverse candidates
_HISTORY_LIMIT = 35
_PARALLEL_ENRICH_LIMIT = 5
_MIN_CONTENT_SIMILARITY = 0.05
_RECENT_TRACKS_FILTER_SIZE = 10  # Prevent repeating last 10 tracks
_ARTIST_COOLDOWN_SIZE = 5  # Don't pick same artist within last 5 tracks
_PUBLISHER_KEYWORDS = {
    "prime video",
    "watertower music",
    "universal pictures",
    "netflix",
    "crunchyroll",
    "disney",
    "official soundtrack",
    "soundtrack",
}
_DEFAULT_PRIMARY_GENRE = "soundtrack"
_DEFAULT_SECONDARY_GENRE = "musical"
_DISCOVERY_TAG_FALLBACKS = ["musical theatre", "show tunes", "broadway"]
_SAFE_PICK_SCORE_THRESHOLD = 0.7

# Phase 5: Telemetry configuration
_TELEMETRY_DIR = Path("cache/music/telemetry")
_TELEMETRY_FILE = "v3_training_data.jsonl"
_TELEMETRY_MAX_SIZE_MB = 50
_TELEMETRY_MAX_FILES = 5
_TELEMETRY_QUALITY_THRESHOLD = 8.0


@dataclass(slots=True)
class AutoplayTelemetryEvent:
    """
    Phase 5: Telemetry event for autoplay recommendation tracking.
    
    Tracks detailed metrics for each autoplay round to enable:
    - Pool composition analysis
    - Consensus breakdown monitoring
    - Diversity injection effectiveness
    - Deezer canonical verification rates
    - API health monitoring
    """
    timestamp: float
    guild_id: int
    seed_artist: str
    seed_title: str
    selected_artist: str
    selected_title: str
    
    # Pool composition: number of tracks from each pool
    pool_composition: Dict[str, int]  # {pool_a: 30, pool_b: 30, pool_c: 20, pool_d: 10, ...}
    
    # Consensus breakdown: distribution of consensus signals in session
    consensus_breakdown: Dict[str, int]  # {liked: 5, disliked: 2, weak_like: 3, neutral: 10}
    
    # Diversity tracking
    diversity_injection_count: int  # Number of forced discovery picks
    consecutive_safe_picks: int  # Current safe pick streak before this round
    
    # Deezer verification metrics
    deezer_canonical_rate: float  # % of tracks in session with is_canonical=True
    is_canonical: bool  # Whether seed track was Deezer-verified
    
    # API health
    deezer_api_errors: int  # Number of Deezer errors in this session
    gemini_quota_remaining: int  # Remaining grounding quota
    
    # Quality metrics
    selected_score: float  # Final score of selected track
    pool_size: int  # Total candidate pool size
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict for telemetry logging."""
        return {
            "timestamp": self.timestamp,
            "guild_id": self.guild_id,
            "seed": f"{self.seed_artist} - {self.seed_title}",
            "selected": f"{self.selected_artist} - {self.selected_title}",
            "pool_composition": self.pool_composition,
            "consensus_breakdown": self.consensus_breakdown,
            "diversity_injection_count": self.diversity_injection_count,
            "consecutive_safe_picks": self.consecutive_safe_picks,
            "deezer_canonical_rate": self.deezer_canonical_rate,
            "is_canonical": self.is_canonical,
            "deezer_api_errors": self.deezer_api_errors,
            "gemini_quota_remaining": self.gemini_quota_remaining,
            "selected_score": self.selected_score,
            "pool_size": self.pool_size,
        }


@dataclass(slots=True)
class PreparedCandidate:
    features: CandidateFeatures
    metadata: Dict[str, Any]


class LastFMAutoplayV3:
    """High-level orchestrator that feeds ``AutoplayEngineV3``."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._engine = AutoplayEngineV3()
        self._lastfm_key = os.getenv(LASTFM_API_KEY_ENV, "").strip()
        self._recent_history: Dict[int, List[Dict[str, str]]] = defaultdict(list)
        self._history_limit = _HISTORY_LIMIT
        self._collaborative_ready = False
        self._collaborative_lock = asyncio.Lock()
        self._enrich_semaphore = asyncio.Semaphore(_PARALLEL_ENRICH_LIMIT)
        self._http_timeout = aiohttp.ClientTimeout(total=12)

        # Issue #3 - Contextual Arc Recommender components
        verbosity = int(os.getenv("AUTOPLAY_V3_VERBOSITY", "0"))
        self._context_tracker: Dict[int, ContextTracker] = {}  # Per-guild context
        self._novelty_controller = NoveltyController(verbose=verbosity)
        self._verbose = verbosity

        # Cache performance tracking (reset per autoplay round)
        self._cache_stats = {
            "enrichment_hits": 0,
            "enrichment_misses": 0,
            "gemini_calls": 0,
        }

        # Phase 4: Diversity injection tracking (per-guild)
        self._consecutive_safe_picks: Dict[int, int] = defaultdict(int)

        # Phase 5: Telemetry initialization
        self._telemetry_dir = _TELEMETRY_DIR
        self._telemetry_dir.mkdir(parents=True, exist_ok=True)
        self._telemetry_enabled = os.getenv("AUTOPLAY_TELEMETRY_ENABLED", "1") == "1"
        
        # Processing lock and UX improvements (per-guild)
        self._is_processing: Dict[int, bool] = defaultdict(bool)
        self._warning_timer_expired: Dict[int, bool] = defaultdict(bool)
        self._warning_tasks: Dict[int, asyncio.Task] = {}
        
        # Deezer canonical rate tracking (per-guild)
        self._canonical_tracks: Dict[int, int] = defaultdict(int)
        self._total_tracks: Dict[int, int] = defaultdict(int)
        
        # Pool composition tracking (per-guild, reset each round)
        self._pool_composition: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        
        # Layer 4: JIT Buffer Architecture (per-guild)
        # _recommendation_buffer: Holds 5-slot buffer [safe, safe, safe, discovery, safe_harbor]
        # _filling_task: Async task that fills buffer in background
        # _buffer_fill_size: Size of buffer per guild (default: 5)
        self._recommendation_buffer: Dict[int, List[PreparedCandidate]] = defaultdict(list)
        self._filling_task: Dict[int, Optional[asyncio.Task]] = {}
        self._buffer_fill_size = 5

    def start(self) -> None:
        """Start the autoplay engine background workers."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._engine.start_analysis_workers())
        except RuntimeError:
            LOG.warning("Could not start autoplay engine workers: No running event loop.")

    # ------------------------------------------------------------------
    # Internal helpers for context tracking (Issue #3)
    # ------------------------------------------------------------------
    def _get_context_tracker(self, guild_id: int) -> ContextTracker:
        """Get or create context tracker for a guild."""
        if guild_id not in self._context_tracker:
            self._context_tracker[guild_id] = ContextTracker(
                history_size=15,
                verbose=self._verbose,
            )

        tracker = self._context_tracker[guild_id]

        # Check if session should reset due to inactivity
        if tracker.should_reset_session(idle_threshold_minutes=15):
            tracker.reset_session()

        return tracker

    # ------------------------------------------------------------------
    # V2.5+: Warning Timer Management
    # ------------------------------------------------------------------
    async def _start_warning_timer(self, guild_id: int, delay: float = 5.0) -> None:
        """
        Start a warning timer for slow recommendations.
        
        After `delay` seconds, sets a flag that MusicPlayer can check
        to show a "please wait" message.
        
        Args:
            guild_id: Discord guild ID
            delay: Seconds to wait before flagging (default: 5.0)
        """
        try:
            await asyncio.sleep(delay)
            self._warning_timer_expired[guild_id] = True
            if self._verbose:
                LOG.info(
                    "⏰ [Guild %d] Warning timer expired after %.1fs",
                    guild_id,
                    delay,
                )
        except asyncio.CancelledError:
            # Timer was cancelled because recommendation finished quickly
            if self._verbose >= 2:
                LOG.debug(
                    "⏰ [Guild %d] Warning timer cancelled (fast recommendation)",
                    guild_id,
                )
    
    def _cancel_warning_timer(self, guild_id: int) -> None:
        """Cancel the warning timer if recommendation finished quickly."""
        if guild_id in self._warning_tasks:
            task = self._warning_tasks[guild_id]
            if not task.done():
                task.cancel()
            del self._warning_tasks[guild_id]

    # ------------------------------------------------------------------
    # Public API expected by ``MusicPlayer``
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """Full system availability: Last.fm AND Gemini must be ready."""
        return self._engine.is_available

    def can_recommend(self) -> bool:
        """Can generate basic recommendations: only Last.fm required.

        Autoplay trigger should use this instead of is_available() to allow
        recommendations even when Gemini is temporarily rate-limited.
        """
        return self._engine.can_recommend

    # ------------------------------------------------------------------
    # V2.5+: Processing Lock & Warning Status API
    # ------------------------------------------------------------------
    def is_processing(self, guild_id: int) -> bool:
        """
        Check if autoplay is currently processing a recommendation for this guild.
        
        The MusicPlayer can use this to:
        - Queue user-added tracks instead of playing immediately
        - Show "recommendation in progress" status
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            True if recommendation is in progress, False otherwise
        """
        return self._is_processing.get(guild_id, False)
    
    def should_show_warning(self, guild_id: int) -> bool:
        """
        Check if the warning timer has expired for this guild.
        
        The MusicPlayer should check this periodically and show:
        "Autoplay is taking longer than usual as it's analyzing your preferences. 
        Thank you for waiting."
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            True if warning should be shown (5+ seconds elapsed), False otherwise
        """
        return self._warning_timer_expired.get(guild_id, False)
    
    def clear_warning(self, guild_id: int) -> None:
        """Clear the warning flag after message is shown."""
        self._warning_timer_expired[guild_id] = False
    
    def _cancel_buffer_fill_task(self, guild_id: int) -> None:
        """
        Cancel ongoing buffer fill task for this guild due to context change.
        
        This should be called when user feedback significantly changes the session
        context (e.g., skip, like, dislike) to ensure buffer is regenerated with
        new context.
        
        Args:
            guild_id: Discord guild ID
        """
        if guild_id in self._filling_task:
            task = self._filling_task[guild_id]
            if task and not task.done():
                task.cancel()
                if self._verbose >= 2:
                    LOG.debug(
                        "🚫 [JIT Buffer] Cancelled ongoing fill task for guild %d (context changed)",
                        guild_id,
                    )
            del self._filling_task[guild_id]
        
        # Clear buffer to force fresh recommendations
        if guild_id in self._recommendation_buffer:
            self._recommendation_buffer[guild_id].clear()

    async def _queue_analysis_for_track(self, artist: str, title: str, track_obj: Any) -> None:
        """Extract identifiers and enqueue analysis, preferring Deezer previews."""
        if not self._engine or not track_obj:
            return

        youtube_id = None
        preview_url = None
        preview_duration_ms = None
        deezer_track_id = None
        expected_duration_ms = None

        info = getattr(track_obj, "info", None)
        if isinstance(info, dict):
            youtube_id = info.get("identifier") or info.get("id")
            preview_url = info.get("preview_url") or info.get("previewUrl")
            preview_duration_ms = info.get("preview_duration_ms") or info.get("previewDurationMs")
            deezer_track_id = info.get("deezer_track_id") or info.get("deezerTrackId")
            duration_hint = info.get("length") or info.get("duration_ms")
            try:
                preview_duration_ms = int(preview_duration_ms) if preview_duration_ms else None
            except (TypeError, ValueError):
                preview_duration_ms = None
            try:
                expected_duration_ms = int(duration_hint) if duration_hint is not None else None
            except (TypeError, ValueError):
                expected_duration_ms = None
        if not youtube_id:
            youtube_id = getattr(track_obj, "identifier", None)

        if not youtube_id:
            return

        if (not preview_url or not preview_duration_ms) and hasattr(self._engine, "ensure_preview_metadata"):
            preview_url, preview_duration_ms, resolved_deezer_id = await self._engine.ensure_preview_metadata(
                artist,
                title,
                youtube_id=youtube_id,
                expected_duration_ms=expected_duration_ms,
            )
            if resolved_deezer_id and not deezer_track_id:
                deezer_track_id = resolved_deezer_id

        queued = self._engine.queue_audio_analysis(
            artist,
            title,
            youtube_id,
            preview_url=preview_url,
            preview_duration_ms=preview_duration_ms,
            deezer_track_id=deezer_track_id,
        )
        if queued and self._verbose >= 2:
            source = "Deezer preview"
            LOG.debug(
                "🎛️ [Analysis Queue] Scheduled %s - %s via %s (youtube_id=%s)",
                artist,
                title,
                source,
                youtube_id,
            )

    async def _write_telemetry_event(self, event: AutoplayTelemetryEvent) -> None:
        """
        Write telemetry event to JSONL file asynchronously.
        
        This runs in the background and won't block recommendation flow.
        Writes to: cache/music/telemetry/v3_training_data.jsonl
        """
        try:
            # Create telemetry directory if needed
            telemetry_dir = _TELEMETRY_DIR
            telemetry_dir.mkdir(parents=True, exist_ok=True)
            
            telemetry_file = telemetry_dir / _TELEMETRY_FILE
            
            # Append event as JSON line
            event_dict = event.to_dict()
            event_json = json.dumps(event_dict)
            
            # Use aiofiles for async file writing if available, else use sync
            try:
                import aiofiles
                async with aiofiles.open(telemetry_file, mode='a', encoding='utf-8') as f:
                    await f.write(event_json + '\n')
            except ImportError:
                # Fallback to sync write in thread pool
                import asyncio
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: telemetry_file.write_text(
                        telemetry_file.read_text(encoding='utf-8') + event_json + '\n'
                        if telemetry_file.exists()
                        else event_json + '\n',
                        encoding='utf-8'
                    )
                )
            
            if self._verbose >= 2:
                LOG.debug("📊 [Telemetry] Wrote event for guild %d", event.guild_id)
                
        except Exception as e:
            LOG.error("Failed to write telemetry event: %s", e)
            # Don't raise - telemetry failures shouldn't break recommendations

    def clear_history(self, guild_id: Optional[int] = None) -> None:
        if guild_id is None:
            self._recent_history.clear()
            self._context_tracker.clear()
        else:
            self._recent_history.pop(int(guild_id), None)
            # Reset context tracker but keep it initialized
            if guild_id in self._context_tracker:
                self._context_tracker[guild_id].reset_session()

    async def clear_guild_session(self, guild_id: int) -> bool:
        """
        Clear guild session data and telemetry when bot leaves VC.
        Called by MusicPlayer.reset_session_state().

        Returns:
            True if session was cleared successfully.
        """
        # Release ML session slot
        if self._engine and hasattr(self._engine, "_session_manager"):
            await self._engine._session_manager.release(guild_id)

        # Clear recent history
        self._recent_history.pop(guild_id, None)

        # Reset context tracker
        if guild_id in self._context_tracker:
            self._context_tracker[guild_id].reset_session()

        # Clear telemetry session file
        return await self._engine.clear_guild_session(guild_id)

    async def record_playback_feedback(
        self,
        guild_id: int,
        artist: str,
        title: str,
        progress_ratio: float,
        *,
        duration_ms: Optional[int] = None,
        primary_listener_bias: bool = False,
        feedback_type: Optional[str] = None,
        user_id: Optional[int] = None,
        num_likes: Optional[int] = None,
        num_dislikes: Optional[int] = None,
        num_active_listeners: Optional[int] = None,
    ) -> None:
        """
        Record playback feedback for a track with optional multi-user aggregation.

        Args:
            guild_id: Discord guild ID
            artist: Track artist
            title: Track title
            progress_ratio: How much was played (0.0-1.0)
            duration_ms: Track duration in milliseconds
            primary_listener_bias: Deprecated (kept for compatibility)
            feedback_type: "more_like_this", "less_like_this", or None
            user_id: Single user ID (legacy, for single-user feedback)
            num_likes: Count of like reactions (v2.5, overrides user_id if provided)
            num_dislikes: Count of dislike reactions (v2.5)
            num_active_listeners: Total active listeners (v2.5)
        """
        if not self.can_recommend():
            return

        track_id = self._track_id(artist, title)
        ratio = max(0.0, min(1.0, float(progress_ratio)))

        if feedback_type == "more_like_this":
            event_type = "like"
        elif feedback_type == "less_like_this":
            event_type = "dislike"
        elif ratio < 0.2:
            event_type = "hard_skip"
        elif ratio < 0.85:
            event_type = "skip"
        else:
            event_type = "finish"

        metadata: Dict[str, Any] = {
            "progress_ratio": ratio,
            "duration_ms": duration_ms,
            "primary_listener_bias": primary_listener_bias,
        }
        if feedback_type:
            metadata["feedback_type"] = feedback_type
        if user_id is not None:
            metadata["user_id"] = str(user_id)

        if self._engine._verbose:
            # Friendly feedback summary
            if event_type == "like":
                LOG.info(
                    "👍 [Feedback] User loved '%s' by '%s' (%.0f%% played) → boosting genre/artist for future picks",
                    title,
                    artist,
                    ratio * 100,
                )
            elif event_type == "dislike":
                LOG.info(
                    "👎 [Feedback] User disliked '%s' by '%s' (%.0f%% played) → reducing genre/artist weight",
                    title,
                    artist,
                    ratio * 100,
                )
            elif event_type == "hard_skip":
                LOG.info(
                    "⏭️ [Feedback] Hard skip on '%s' by '%s' (%.0f%% played) → strong penalty for similar tracks",
                    title,
                    artist,
                    ratio * 100,
                )
            elif event_type == "skip":
                LOG.info(
                    "⏩ [Feedback] Skipped '%s' by '%s' (%.0f%% played) → mild penalty",
                    title,
                    artist,
                    ratio * 100,
                )
            elif event_type == "quality_issue":
                LOG.info(
                    "⚠️ [Quality] Bad mapping for '%s' by '%s' → banned, cache cleared, Gemini refetch triggered",
                    title,
                    artist,
                )
            elif event_type == "more_like_this":
                LOG.info(
                    "💚 [UserFeedback] More like this: '%s' by '%s' → explicit positive preference (stronger than finish)",
                    title,
                    artist,
                )
            elif event_type == "less_like_this":
                LOG.info(
                    "💔 [UserFeedback] Less like this: '%s' by '%s' → explicit negative preference",
                    title,
                    artist,
                )
            elif event_type == "finish":
                LOG.info(
                    "✅ [Feedback] Finished '%s' by '%s' (%.0f%% complete) → positive signal for artist/genre",
                    title,
                    artist,
                    ratio * 100,
                )
            else:
                LOG.debug(
                    "[AutoplayV2][feedback] guild=%s track=%s event=%s ratio=%.3f",
                    guild_id,
                    track_id,
                    event_type,
                    ratio,
                )

        await self._engine.record_feedback_event(
            guild_id=guild_id,
            user_id=user_id,
            track_id=track_id,
            event_type=event_type,
            metadata={
                key: value for key, value in metadata.items() if value is not None
            },
        )

        # Issue #3: Record in context tracker for arc recommender with enrichment data
        tracker = self._get_context_tracker(guild_id)

        # Determine skip status and type from event
        # ONLY actual skips count as skips - more/less like this are explicit user feedback events
        was_skipped = event_type in ("hard_skip", "skip")
        skip_type = None

        if event_type == "hard_skip":
            skip_type = "hard"
        elif event_type == "skip":
            skip_type = "medium" if ratio < 0.5 else "soft"


        # Try to get youtube_id and deezer_id from cache for EfficientAT enrichment
        youtube_id = None
        deezer_id = None
        try:
            mapping_entry = await self._engine._cache.get_mapping(artist, title)
            if mapping_entry:
                youtube_id = mapping_entry.youtube_id
            
            parsing_entry = await self._engine._cache.get_parsing(artist, title)
            if parsing_entry:
                deezer_id = parsing_entry.deezer_id
        except Exception as e:
            LOG.debug(f"Unable to retrieve youtube_id/deezer_id from cache: {e}")

        # Try to get enrichment data from cache for accurate genre/mood tracking
        enrichment = await self._engine.enrich_track(
            artist, title, youtube_id=youtube_id, deezer_id=deezer_id
        )
        genres = enrichment.get("tags", [])[:5] if enrichment else []
        mood_vector = enrichment.get("mood_vector") if enrichment else None
        mood_label = enrichment.get("mood") if enrichment else None

        # Try to get track_type and primary_entity from parsing cache
        track_type = "music" 
        primary_entity = None

        # Attempt to retrieve from cache by checking the most recent parsing entries
        try:
            # TODO: Pass raw_title/channel_name through track_end_event signature for cache lookup
            pass
        except Exception as e:
            LOG.debug(f"Unable to retrieve track_type from cache: {e}")

        # Aggregate multi-user feedback
        # If explicit counts provided, use them. Otherwise infer from legacy single-user feedback
        if (
            num_likes is not None
            or num_dislikes is not None
            or num_active_listeners is not None
        ):
            # Multi-user mode: use provided counts
            final_num_likes = num_likes or 0
            final_num_dislikes = num_dislikes or 0
            final_num_active_listeners = num_active_listeners or 1
        else:
            # Legacy single-user mode: infer from feedback_type and event_type
            final_num_active_listeners = 1
            if feedback_type == "more_like_this" or event_type == "like":
                final_num_likes = 1
                final_num_dislikes = 0
            elif feedback_type == "less_like_this" or event_type == "dislike":
                final_num_likes = 0
                final_num_dislikes = 1
            else:
                # No explicit feedback, just skip/finish data
                final_num_likes = 0
                final_num_dislikes = 0

        tracker.record_play(
            track_id=track_id,
            artist=artist,
            title=title,
            genres=genres,
            mood_vector=mood_vector,
            mood_label=mood_label,
            was_skipped=was_skipped,
            skip_type=skip_type,
            progress_ratio=ratio,
            num_likes=final_num_likes,
            num_dislikes=final_num_dislikes,
            num_active_listeners=final_num_active_listeners,
            track_type=track_type,
            primary_entity=primary_entity,
        )
        
        # Layer 4: Cancel buffer fill task if context significantly changed
        # Skip, like, or dislike events require fresh recommendations
        if event_type in ("skip", "hard_skip", "like", "dislike", "more_like_this", "less_like_this"):
            self._cancel_buffer_fill_task(guild_id)
            if self._verbose >= 2:
                LOG.debug(
                    "🔄 [JIT Buffer] Context changed (%s) - buffer invalidated for guild %d",
                    event_type,
                    guild_id,
                )

        # Phase 5: Log telemetry data for ML training
        # Get session context for telemetry
        context_data = tracker.get_context()

        # Build telemetry entry
        telemetry_context = {
            "focus_genres": (
                context_data.focus_genres[:5] if context_data.focus_genres else []
            ),
            "liked_mood_vector": (
                list(context_data.liked_mood_vector)
                if context_data.liked_mood_vector
                else None
            ),
            "disliked_tags": (
                dict(
                    sorted(
                        context_data.disliked_tags.items(),
                        key=lambda x: x[1],
                        reverse=True,
                    )[:10]
                )
                if context_data.disliked_tags
                else {}
            ),
            "energy_trend": context_data.energy_trend,
        }

        telemetry_candidate = {
            "artist": artist,
            "title": title,
            "genres": genres[:5] if genres else [],
            "mood_vector": (
                list(mood_vector)
                if mood_vector and hasattr(mood_vector, "__iter__")
                else None
            ),
            "track_type": track_type,
            "primary_entity": primary_entity,
            "pool_source": None,  # Not available in feedback context
        }

        telemetry_outcome = {
            "progress_ratio": ratio,
            "num_likes": final_num_likes,
            "num_dislikes": final_num_dislikes,
            "num_active_listeners": final_num_active_listeners,
        }

        # Estimate quality 
        # High progress ratio + likes suggest good quality
        estimated_quality = 0.0
        if ratio >= 0.85:
            estimated_quality += 4.0
        elif ratio >= 0.5:
            estimated_quality += 2.0

        if final_num_likes > 0:
            like_ratio = final_num_likes / max(1, final_num_active_listeners)
            estimated_quality += like_ratio * 5.0

        if final_num_dislikes > 0:
            dislike_ratio = final_num_dislikes / max(1, final_num_active_listeners)
            estimated_quality -= dislike_ratio * 3.0

        telemetry_quality = {
            "heuristic_score": max(0.0, min(10.0, estimated_quality)),
            "spam_flags": [],  # Not available in feedback context
        }

        self._log_telemetry(
            context=telemetry_context,
            candidate=telemetry_candidate,
            outcome=telemetry_outcome,
            mapping_quality=telemetry_quality,
        )

    async def refresh_ingest_queue(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """Rebuild the ingest queue by scanning cached enrichment entries."""
        if not self._engine:
            return {
                "queued": 0,
                "missing_mapping": 0,
                "duplicates": 0,
                "queue_depth": 0,
            }
        return await self._engine.refresh_ingest_queue(limit=limit)

    async def reingest_track(
        self,
        artist: str,
        title: str,
        *,
        youtube_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Force a single track to re-enrich and enqueue analysis."""
        if not self._engine:
            return {
                "enriched": False,
                "queued_analysis": False,
                "youtube_source": youtube_id,
                "mapping_found": False,
            }
        return await self._engine.reingest_track(artist, title, youtube_id=youtube_id)

    async def get_recommendations_for_track(
        self,
        track_info: Dict[str, Any],
        limit: int = 10,
    ) -> List[Tuple[str, Any]]:
        if not self.can_recommend():
            LOG.debug("Autoplay V2 unavailable; skipping recommendation lookup")
            return []

        raw_title = str(track_info.get("title", "")).strip()
        channel_name = str(track_info.get("author", "")).strip()
        expected_duration_ms = track_info.get("length")
        guild_id = int(track_info.get("guild_id", 0) or 0)

        # V3 Session Management: Strict VIP Room
        # Try to acquire ML session slot
        has_ml_session = await self._engine._session_manager.acquire(guild_id)
        if not has_ml_session:
            if self._verbose:
                LOG.info(f"🚫 Autoplay denied for guild {guild_id} (Capacity reached: 2/2)")
            return []

        await self._engine.start_analysis_workers()

        # V2.5+: Set processing lock and start warning timer
        self._is_processing[guild_id] = True
        self._warning_timer_expired[guild_id] = False
        self._warning_tasks[guild_id] = asyncio.create_task(
            self._start_warning_timer(guild_id, delay=5.0)
        )
        
        try:
            if not raw_title:
                LOG.debug("Missing seed title; aborting autoplay round")
                return []

            parsed = await self._engine.parse_track(raw_title, channel_name)
            if self._engine._verbose:
                LOG.debug(
                    "[AutoplayV2][seed] raw_title=%r channel=%r -> artist=%s title=%s",
                    raw_title,
                    channel_name,
                    (parsed or {}).get("artist") or channel_name,
                    (parsed or {}).get("title") or raw_title,
                )
            seed_artist = parsed["artist"] if parsed else channel_name
            seed_title = parsed["title"] if parsed else raw_title

            if not seed_artist or not seed_title:
                LOG.debug("Unable to resolve seed metadata; aborting autoplay round")
                return []

            # Issue #3: Get session context for adaptive recommendation
            tracker = self._get_context_tracker(guild_id)
            context = tracker.get_context()

            if self._verbose >= 1:
                phase = self._novelty_controller.detect_exploration_phase(
                    skip_rate=context.skip_rate,
                    songs_since_novelty=context.songs_since_novelty,
                    session_duration_minutes=(context.last_activity - context.session_start)
                    / 60,
                )
                LOG.info(
                    "🎯 [Context] Session state: focus=%s, skip_rate=%.0f%%, streak=%d, phase=%s",
                    context.focus_genres[:2] if context.focus_genres else ["none"],
                    context.skip_rate * 100,
                    context.consecutive_skips,
                    phase.value,
                )

            await self._ensure_collaborative_ready()

            seed_track_id = self._track_id(seed_artist, seed_title)
            seed_mood = await self._engine.get_mood_vector(seed_artist, seed_title)
            session_vector: Optional[Sequence[float]] = None
            target_mood: Optional[str] = None
            if seed_mood:
                session_vector = seed_mood.get("vector")
                target_mood = seed_mood.get("mood")

            # Phase 2: Get seed track metadata for OST branching and canonical routing
            seed_track_type = parsed.get("track_type", "music") if parsed else "music"
            seed_entity = parsed.get("primary_entity") if parsed else None
            is_canonical = parsed.get("is_canonical", False) if parsed else False
            
            # Layer 4: Check JIT buffer and manage fill task
            buffer = self._recommendation_buffer.get(guild_id, [])
            existing_task = self._filling_task.get(guild_id)
            
            # If buffer has candidates, use them immediately
            if buffer and len(buffer) > 0:
                if self._verbose:
                    LOG.info(
                        "⚡ [JIT Buffer] Using buffered recommendations (%d available)",
                        len(buffer),
                    )
                
                # Pop from buffer
                prepared_candidate = buffer.pop(0)
                self._recommendation_buffer[guild_id] = buffer
                
                # Start background fill if buffer is getting low
                if len(buffer) <= 2 and (not existing_task or existing_task.done()):
                    self._filling_task[guild_id] = asyncio.create_task(
                        self._fill_recommendation_buffer(
                            guild_id,
                            seed_artist,
                            seed_title,
                            context,
                            tracker,
                            seed_track_type=seed_track_type,
                            seed_entity=seed_entity,
                            is_canonical=is_canonical,
                        )
                    )
                    if self._verbose >= 2:
                        LOG.debug("🔄 [JIT Buffer] Started background fill (buffer low)")
                
                # Resolve track from buffered candidate
                meta = prepared_candidate.metadata
                track_obj = await self._engine.resolve_track(
                    meta["artist"],
                    meta["title"],
                    expected_duration_ms=None,
                )
                
                if track_obj:
                    await self._queue_analysis_for_track(meta["artist"], meta["title"], track_obj)
                    self._note_recommendation(guild_id, meta["artist"], meta["title"])
                    return [(prepared_candidate.features.track_id, track_obj)]
                else:
                    # Buffered track failed resolution, fall through to normal flow
                    if self._verbose:
                        LOG.warning(
                            "❌ [JIT Buffer] Buffered track resolution failed, using normal flow"
                        )
            
            # Buffer empty or resolution failed - await existing task or start new one
            if existing_task and not existing_task.done():
                if self._verbose:
                    LOG.info("⏳ [JIT Buffer] Waiting for ongoing fill task...")
                try:
                    await asyncio.wait_for(existing_task, timeout=15.0)
                    # Check buffer again after task completes
                    buffer = self._recommendation_buffer.get(guild_id, [])
                    if buffer:
                        prepared_candidate = buffer.pop(0)
                        self._recommendation_buffer[guild_id] = buffer
                        
                        meta = prepared_candidate.metadata
                        track_obj = await self._engine.resolve_track(
                            meta["artist"],
                            meta["title"],
                            expected_duration_ms=None,
                        )
                        
                        if track_obj:
                            await self._queue_analysis_for_track(meta["artist"], meta["title"], track_obj)
                            self._note_recommendation(guild_id, meta["artist"], meta["title"])
                            return [(prepared_candidate.features.track_id, track_obj)]
                except asyncio.TimeoutError:
                    if self._verbose:
                        LOG.warning("⏱️ [JIT Buffer] Fill task timed out, using normal flow")
                except Exception as exc:
                    LOG.error("❌ [JIT Buffer] Fill task error: %s", exc)
            
            # Fall through to normal candidate fetching
            # (buffer empty, task failed, or first request)
            if self._verbose >= 2:
                LOG.debug("📥 [JIT Buffer] Using normal flow (buffer unavailable)")

            candidate_records = await self._fetch_candidate_records(
                guild_id=guild_id,
                seed_artist=seed_artist,
                seed_title=seed_title,
                seed_track_type=seed_track_type,
                seed_entity=seed_entity,
                is_canonical=is_canonical,
                context=context,
                tracker=tracker,
            )
            if not candidate_records:
                return []

            # Task 3.6: Apply artist diversity filter
            candidate_records, artist_diversity_pool = self._apply_artist_diversity_filter(
                candidate_records, max_per_artist=3
            )

            if self._engine._verbose:
                LOG.debug(
                    "[AutoplayV2][diversity] Kept %d candidates (filtered %d for artist echo prevention)",
                    len(candidate_records),
                    len(artist_diversity_pool),
                )

            prepared_candidates = await self._prepare_candidates(
                guild_id,
                candidate_records,
            )
            if not prepared_candidates:
                LOG.debug("No viable candidates after enrichment step")
                return []

            features = [entry.features for entry in prepared_candidates]
            metadata_index = {
                entry.features.track_id: entry.metadata for entry in prepared_candidates
            }

            # Phase 3: Extract last track's energy for flow scoring
            last_energy = None
            if tracker._history:
                last_track = tracker._history[-1]
                last_energy = last_track.energy

            scored = self._engine.score_candidates(
                guild_id,
                features,
                    seed_track_ids=[seed_track_id],
                session_mood_vector=session_vector,
                target_mood=target_mood,
                # Phase 3: Enhanced scoring context
                session_focus_genres=context.focus_genres,
                liked_mood_vector=context.liked_mood_vector,
                energy_trend=context.energy_trend,
                last_energy=last_energy,
            )

            # Issue #3: Apply novelty controller adjustments
            # Apply repetition penalties based on recent history
            for candidate in scored:
                repetition_penalty = tracker.compute_repetition_penalty(
                    candidate.track_id, tau=6
                )
                candidate.score *= repetition_penalty
                if self._verbose >= 2 and repetition_penalty < 0.9:
                    LOG.debug(
                        "🔄 [Novelty] Repetition penalty %.2f for '%s' (recently played)",
                        repetition_penalty,
                        candidate.title,
                    )

                # Phase 3 Task 4.5: Apply temporal-weighted genre/tag penalties for recently skipped content
                if context.disliked_tags:
                    # Get candidate features to access genres
                    candidate_features = next(
                        (f for f in features if f.track_id == candidate.track_id), None
                    )

                    if candidate_features and candidate_features.genres:
                        # Calculate tag penalty (average of all matching disliked tags)
                        tag_penalties = [
                            context.disliked_tags.get(genre, 0.0)
                            for genre in candidate_features.genres
                            if genre in context.disliked_tags
                        ]

                        if tag_penalties:
                            avg_penalty = sum(tag_penalties) / len(tag_penalties)
                            tag_multiplier = (
                                1.0 - avg_penalty
                            )  # Convert penalty to multiplier
                            candidate.score *= tag_multiplier

                            if self._verbose >= 2 and avg_penalty > 0.1:
                                LOG.debug(
                                    "👎 [Skip Penalty] Reducing score by %.1f%% for '%s' (disliked tags: %s)",
                                    avg_penalty * 100,
                                    candidate.title,
                                    [
                                        g
                                        for g in candidate_features.genres
                                        if g in context.disliked_tags
                                    ][:3],
                                )

            # Re-sort after applying all penalties
            scored.sort(key=lambda c: c.score, reverse=True)

            # Phase 4: Diversity injection - check if we need to force exploration
            consecutive_safe = self._consecutive_safe_picks.get(guild_id, 0)
            force_diversity = consecutive_safe >= 5

            if force_diversity and self._verbose:
                LOG.info(
                    "🌈 [Diversity Injection] Forcing discovery pick after %d safe picks",
                    consecutive_safe,
                )

            # Phase 4: Apply diversity injection filter if needed
            selection_pool = scored
            if force_diversity:
                # Filter to pool_d_discovery candidates with score >= 7.0
                discovery_candidates = [
                    c
                    for c in scored
                    if metadata_index.get(c.track_id, {}).get("pool_source")
                    == "pool_d_discovery"
                    and c.score >= 7.0
                ]

                if discovery_candidates:
                    selection_pool = discovery_candidates
                    if self._verbose:
                        LOG.info(
                            "🌈 [Diversity Injection] Filtered to %d discovery candidates (from %d total)",
                            len(discovery_candidates),
                            len(scored),
                        )
                else:
                    # Fallback: use any candidate with score >= 7.0
                    fallback_pool = [c for c in scored if c.score >= 7.0]
                    if fallback_pool:
                        selection_pool = fallback_pool
                        if self._verbose:
                            LOG.warning(
                                "🌈 [Diversity Injection] No discovery candidates, using fallback pool of %d",
                                len(fallback_pool),
                            )

            # Stochastic selection without additional safe gate retries
            results: List[Tuple[str, Any]] = []

            while len(results) < max(1, limit) and selection_pool:
                # Stochastic selection from top 5
                top_k = min(5, len(selection_pool))
                top_candidates = selection_pool[:top_k]

                # Calculate weights as score^2 for non-linear preference
                weights = [c.score**2 for c in top_candidates]
                total_weight = sum(weights)

                if total_weight <= 0:
                    # Fallback to uniform if all scores are 0 or negative
                    selected_candidate = top_candidates[0]
                else:
                    # Weighted random selection
                    selected_candidate = random.choices(
                        top_candidates, weights=weights, k=1
                    )[0]

                # Log stochastic selection if verbose
                if self._verbose >= 2 and top_k > 1:
                    LOG.debug(
                        "🎲 [Stochastic Selection] Picked '%s' (score=%.3f) from top %d candidates",
                        selected_candidate.title,
                        selected_candidate.score,
                        top_k,
                    )

                # Log the ACTUALLY selected candidate (not just the top one)
                if self._verbose and len(results) == 0:  # Only log the first pick
                    selected_meta = metadata_index.get(selected_candidate.track_id, {})
                    # Try to find mood label from the prepared candidate features
                    selected_feat = next(
                        (
                            e.features
                            for e in prepared_candidates
                            if e.features.track_id == selected_candidate.track_id
                        ),
                        None,
                    )
                    mood_desc = (
                        selected_feat.mood_label if selected_feat and selected_feat.mood_label else target_mood
                    ) or "unknown"

                    # Issue #3: Show exploration phase and reasoning
                    phase = self._novelty_controller.detect_exploration_phase(
                        skip_rate=context.skip_rate,
                        songs_since_novelty=context.songs_since_novelty,
                        session_duration_minutes=(context.last_activity - context.session_start)
                        / 60,
                    )

                    # Check if this is a novelty pick or core pick
                    artist_plays = tracker.get_artist_play_count(selected_meta.get("artist", ""))
                    is_new_artist = artist_plays == 0
                    exploration_marker = "🔍 NEW" if is_new_artist else "✨ FAMILIAR"

                    LOG.info(
                        "🎯 [Next Pick] %s: '%s' by '%s' (score=%.3f, mood=%s, phase=%s) after '%s'",
                        exploration_marker,
                        selected_meta.get("title", "Unknown"),
                        selected_meta.get("artist", "Unknown"),
                        selected_candidate.score,
                        mood_desc,
                        phase.value,
                        f"{seed_artist} - {seed_title}",
                    )

                    # Increment novelty counter if this is exploration
                    if is_new_artist:
                        tracker.reset_novelty_counter()
                    else:
                        tracker.increment_novelty_counter()

                # Safe gate - check quality threshold
                meta = metadata_index.get(selected_candidate.track_id)
                if not meta:
                    # Remove from pool and retry
                    selection_pool = [
                        c
                        for c in selection_pool
                        if c.track_id != selected_candidate.track_id
                    ]
                    continue

                # Resolve track
                track_obj = await self._engine.resolve_track(
                    meta["artist"],
                    meta["title"],
                    expected_duration_ms=None,
                )

                if not track_obj:
                    # Track resolution failed, remove and retry
                    selection_pool = [
                        c
                        for c in selection_pool
                        if c.track_id != selected_candidate.track_id
                    ]
                    if self._verbose:
                        LOG.debug(
                            "❌ [Resolution] Track resolution failed for '%s'", 
                            selected_candidate.title,
                        )
                    continue

                # Track accepted after resolution
                await self._queue_analysis_for_track(meta["artist"], meta["title"], track_obj)
                self._note_recommendation(guild_id, meta["artist"], meta["title"])
                results.append((selected_candidate.track_id, track_obj))

                # Update diversity injection counter
                if selected_candidate.score >= _SAFE_PICK_SCORE_THRESHOLD:
                    self._consecutive_safe_picks[guild_id] = consecutive_safe + 1
                    if self._verbose >= 2:
                        LOG.debug(
                            "📈 [Diversity Counter] Incremented to %d after safe pick",
                            self._consecutive_safe_picks[guild_id],
                        )
                elif force_diversity:
                    # Reset counter on diversity injection
                    self._consecutive_safe_picks[guild_id] = 0
                    if self._verbose:
                        LOG.info(
                            "🔄 [Diversity Counter] Reset to 0 after diversity injection"
                        )

                # Remove selected candidate from pool for next iteration
                selection_pool = [
                    c for c in selection_pool if c.track_id != selected_candidate.track_id
                ]

            # V3: Start background buffer fill for next recommendation
            if results and (guild_id not in self._filling_task or self._filling_task.get(guild_id, asyncio.Future()).done()):
                self._filling_task[guild_id] = asyncio.create_task(
                    self._fill_recommendation_buffer(
                        guild_id,
                        seed_artist,
                        seed_title,
                        context,
                        tracker,
                        seed_track_type=seed_track_type,
                        seed_entity=seed_entity,
                        is_canonical=is_canonical,
                    )
                )
                if self._verbose >= 2:
                    LOG.debug("🔄 [V3 Buffer] Started background fill for next recommendation")

            if not results:
                LOG.debug("Autoplay V2 produced no playable tracks after resolution")
            return results
            
        finally:
            # Release processing lock and cancel warning timer
            self._is_processing[guild_id] = False
            self._cancel_warning_timer(guild_id)
            if self._verbose >= 2:
                LOG.debug("🔓 [Guild %d] Processing lock released", guild_id)    
    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _ensure_collaborative_ready(self) -> None:
        if self._collaborative_ready:
            return
        async with self._collaborative_lock:
            if not self._collaborative_ready:
                self._collaborative_ready = await self._engine.hydrate_collaborative()

    async def _fetch_bootstrapped_pool(self) -> List[Dict[str, Any]]:
        """
        Fetch candidates from the bootstrapped charts.
        Used when cache size < 50 (System Cold Start) or as fallback.
        
        Sources (in priority order):
        1. Bootstrap manager's tracked set
        2. Enrichment cache (for tracks that have been processed)
        3. Direct Deezer chart fetch (emergency fallback)
        """
        if self._engine._verbose:
            LOG.info("🌱 [Progressive] Using Bootstrapped Charts strategy")
        
        pool = []
        
        # Source 1: Bootstrap manager's tracked set
        bootstrapped_keys = list(self._engine._bootstrap_manager._bootstrapped_tracks)
        for key in bootstrapped_keys:
            if "::" in key:
                artist, title = key.split("::", 1)
                pool.append({
                    "title": title,
                    "artist": artist,
                    "pool_source": "bootstrapped_charts"
                })
        
        # Source 2: Enrichment cache (tracks that have been processed but maybe not in bootstrap set)
        if hasattr(self._engine, "_cache") and hasattr(self._engine._cache, "_enrichment_cache"):
            for key in list(self._engine._cache._enrichment_cache.keys()):
                if key not in bootstrapped_keys and "::" in key:
                    artist, title = key.split("::", 1)
                    pool.append({
                        "title": title,
                        "artist": artist,
                        "pool_source": "enrichment_cache"
                    })
        
        # Source 3: Emergency fallback - fetch directly from Deezer if we have nothing
        if not pool:
            if self._engine._verbose:
                LOG.warning("🌱 [Progressive] No cached tracks, fetching Deezer charts directly")
            try:
                from .deezer_fetch import DeezerClient
                async with DeezerClient() as client:
                    tracks = await client.get_charts(limit=50)
                    for track in tracks:
                        pool.append({
                            "title": track.title,
                            "artist": track.artist,
                            "pool_source": "deezer_charts_direct"
                        })
                if self._engine._verbose and pool:
                    LOG.info("🌱 [Fallback] Fetched %d tracks directly from Deezer charts", len(pool))
            except Exception as e:
                LOG.error("❌ [Fallback] Failed to fetch Deezer charts: %s", e)
        
        if pool:
            # Sample 50 tracks if we have more
            if len(pool) > 50:
                pool = random.sample(pool, 50)
            if self._engine._verbose:
                LOG.info("🌱 [Progressive] Returning %d bootstrapped candidates", len(pool))
        else:
            if self._engine._verbose:
                LOG.warning("🌱 [Progressive] No bootstrapped tracks available")
            
        return pool

    async def _fetch_collaborative_pool(self, seed_track_id: str) -> List[Dict[str, Any]]:
        """
        Fetch candidates from the Collaborative Matrix.
        Used when cache size >= 200 (System Mature).
        """
        if self._engine._verbose:
            LOG.info("🤝 [Progressive] Using Collaborative Filtering strategy (>= 200 cached tracks)")
            
        # Get similar tracks from matrix
        similar = self._engine._collaborative.similar_tracks(seed_track_id, limit=50)
        
        pool = []
        for track_id, score in similar:
            if "::" in track_id:
                artist, title = track_id.split("::", 1)
                pool.append({
                    "title": title,
                    "artist": artist,
                    "pool_source": "collaborative_matrix",
                    "collaborative_score": score
                })
        return pool

    async def _fetch_candidate_records(
        self,
        *,
        guild_id: int,
        seed_artist: str,
        seed_title: str,
        seed_track_type: str = "music",
        seed_entity: Optional[str] = None,
        is_canonical: bool = False,
        context: Any,  # SessionContext from context_tracker
        tracker: Any,  
    ) -> List[Dict[str, Any]]:
        """
        Fetch candidate pool using Phase 2 diversification strategy.

        Routing Logic (Phase 2 Update):
        - If is_canonical=False (Deezer failed) → use degraded genre-based pools
        - Else if OST/Entity-based → use entity pools
        - Else if Artist-based:
            - Cold Start: < 3 songs history
            - Warm Start: >= 3 songs history

        Args:
            guild_id: Discord guild ID
            seed_artist: Seed track artist
            seed_title: Seed track title
            seed_track_type: Track type from parsing (ost, anime_opening, game_soundtrack, music)
            seed_entity: Primary entity for OST content (e.g., "Hazbin Hotel")
            is_canonical: Whether Deezer verified the artist/title (from waterfall)
            context: SessionContext with history and focus_genres
            tracker: ContextTracker instance for accessing _history

        Returns:
            List of track records with _source and pool_source tags
        """
        if not self._lastfm_key:
            LOG.warning("Last.fm API key missing; cannot fetch candidates")
            return []

        # --- PROGRESSIVE LOGIC INJECTION ---
        cached_count = 0
        if hasattr(self._engine, "_cache") and hasattr(self._engine._cache, "_enrichment_cache"):
             cached_count = len(self._engine._cache._enrichment_cache)

        # Case 1: System Cold Start (< 50 tracks)
        if cached_count < 50:
             return await self._fetch_bootstrapped_pool()

        # Case 3 Preparation: Hybrid (>= 200 tracks)
        collaborative_pool = []
        if cached_count >= 200:
             seed_track_id = self._track_id(seed_artist, seed_title)
             collaborative_pool = await self._fetch_collaborative_pool(seed_track_id)

        # Priority routing based on is_canonical flag
        candidates = []
        if not is_canonical:
            # STRICT: If Deezer failed to verify, we drop the track.
            # No degraded pools.
            if self._verbose:
                LOG.warning(
                    "🚫 [Strict Mode] Deezer verification failed for '%s'. Autoplay disabled for this track.",
                    seed_title,
                )
            candidates = []
        else:
            # OST branch detection (only when canonical)
            is_ost = seed_track_type in {"ost", "game_soundtrack", "anime_opening"}
            use_entity_branch = is_ost and seed_entity is not None

            history = self._recent_history.get(guild_id, [])
            history_size = len(history)

            if use_entity_branch:
                # Branch A: Entity-based fetch for OST content
                entity_key = cast(str, seed_entity)
                candidates = await self._fetch_entity_based_pools(
                    seed_artist=seed_artist,
                    seed_title=seed_title,
                    seed_entity=entity_key,
                    context=context,
                    tracker=tracker,
                )
            elif history_size < 3:
                # Branch A: Cold start
                candidates = await self._fetch_cold_start_pools(
                    seed_artist=seed_artist,
                    seed_title=seed_title,
                    context=context,
                )
            else:
                # V2.5+: Check exploration phase for Hot Pool vs Warm Pool
                phase = self._novelty_controller.detect_exploration_phase(
                    skip_rate=context.skip_rate,
                    songs_since_novelty=context.songs_since_novelty,
                    session_duration_minutes=(context.last_activity - context.session_start) / 60,
                )
                
                # Import ExplorationPhase for comparison
                from .novelty_controller import ExplorationPhase
                
                if phase == ExplorationPhase.STABLE:
                    # Branch B: Hot Pool (user satisfied, low diversity)
                    if self._verbose:
                        LOG.info(
                            "🔥 [Branch B: Hot Pool] User in STABLE phase (skip_rate=%.1f%%), using high-familiarity pool",
                            context.skip_rate * 100,
                        )
                    candidates = await self._fetch_hot_pool(
                        seed_artist=seed_artist,
                        seed_title=seed_title,
                        context=context,
                        tracker=tracker,
                    )
                else:
                    # Branch C: Warm Pool (exploration/boredom, high diversity)
                    if self._verbose:
                        LOG.info(
                            "🌈 [Branch C: Warm Pool] User in %s phase (skip_rate=%.1f%%), using high-diversity pool",
                            phase.value,
                            context.skip_rate * 100,
                        )
                    candidates = await self._fetch_warm_start_pools(
                        seed_artist=seed_artist,
                        seed_title=seed_title,
                        context=context,
                        tracker=tracker,
                    )
        
        # Merge Collaborative Pool if available
        if collaborative_pool:
             if self._verbose:
                 LOG.info("🤝 [Progressive] Merging %d collaborative candidates", len(collaborative_pool))
             candidates.extend(collaborative_pool)

        # Fallback to Bootstrapped Pool if all else fails (Daydreamer Fallback)
        if not candidates:
            if self._verbose:
                LOG.warning("⚠️ [Fallback] No candidates found via primary strategies. Attempting Daydreamer fallback.")
            bootstrapped = await self._fetch_bootstrapped_pool()
            if bootstrapped:
                candidates.extend(bootstrapped)
                if self._verbose:
                    LOG.info("🌱 [Fallback] Rescued session with %d Daydreamer tracks", len(bootstrapped))
             
        return candidates

    async def _fetch_entity_based_pools(
        self,
        *,
        seed_artist: str,
        seed_title: str,
        seed_entity: str,
        context: Any,
        tracker: Any,
    ) -> List[Dict[str, Any]]:
        """
        Fetch Branch A (entity-based) pools for OST content.

        Prong 1: tag.getTopTracks(entity) - 40 tracks
        Prong 2: track.getSimilar(seed) or fallback tag continuity - 30 tracks
        Prong 3: artist.getTopTracks(recent artists) or secondary genre - 30 tracks
        Prong 4: tag.getSimilar(entity) -> tag.getTopTracks(discovery tag) - 20 tracks
        Total: ~110-120 tracks
        """
        if self._engine._verbose:
            LOG.info(
                "🎬 [Branch A] Entity-based fetch for '%s' (entity=%s)",
                seed_title,
                seed_entity,
            )

        # Phase 7 Task 8.1: Parallel pool fetching with asyncio.gather
        focus_genres = getattr(context, "focus_genres", []) or []

        # Fetch all pools in parallel
        pool_a_task = self._fetch_tag_top_tracks(seed_entity, limit=40)
        pool_b_task = (
            self._fetch_track_similar(seed_artist, seed_title, limit=30)
            if seed_artist and not self._is_publisher_name(seed_artist)
            else asyncio.sleep(0, result=[])
        )
        pool_d_task = self._fetch_discovery_tag_tracks(
            seed_entity=seed_entity, focus_genres=focus_genres, limit=20
        )

        pool_a, pool_b_raw, pool_d = await asyncio.gather(
            pool_a_task, pool_b_task, pool_d_task
        )

        # Tag pool sources
        for record in pool_a:
            record["pool_source"] = "pool_a_entity_cluster"

        # Pool B: Continuity with fallback
        pool_b = pool_b_raw if isinstance(pool_b_raw, list) else []
        continuity_source = "pool_a_continuity"

        if not pool_b:
            fallback_tag = self._select_genre_tag(
                focus_genres, 0, _DEFAULT_PRIMARY_GENRE
            )
            if fallback_tag:
                if self._engine._verbose:
                    LOG.debug(
                        "[Branch A] Continuity fallback using tag.getTopTracks tag=%s (seed_artist=%s)",
                        fallback_tag,
                        seed_artist or "<none>",
                    )
                pool_b = await self._fetch_tag_top_tracks(fallback_tag, limit=30)
                continuity_source = "pool_a_continuity_fallback"
        for record in pool_b:
            record["pool_source"] = continuity_source

        # Pool C: Safe harbor (prefer recent non-publisher artists, fallback to secondary genre)
        recent_artists = [
            artist
            for artist in self._extract_recent_artists(tracker, max_artists=3)
            if not self._is_publisher_name(artist)
        ]
        pool_c: List[Dict[str, Any]] = []
        safe_harbor_source = "pool_b_familiarity"

        # Phase 7 Task 8.1: Fetch artist tracks in parallel
        if recent_artists:
            artist_tasks = [
                self._fetch_artist_top_tracks(artist, limit=10)
                for artist in recent_artists
            ]
            artist_results = await asyncio.gather(*artist_tasks)
            for artist_tracks in artist_results:
                pool_c.extend(artist_tracks)

        if not pool_c:
            fallback_tag = self._select_genre_tag(
                focus_genres, 1, _DEFAULT_SECONDARY_GENRE
            )
            if fallback_tag:
                if self._engine._verbose:
                    LOG.debug(
                        "[Branch A] Safe harbor fallback using tag.getTopTracks tag=%s (recent artists were publishers)",
                        fallback_tag,
                    )
                pool_c = await self._fetch_tag_top_tracks(fallback_tag, limit=30)
                safe_harbor_source = "pool_b_familiarity_genre"
        for record in pool_c:
            record["pool_source"] = safe_harbor_source

        for record in pool_d:
            record["pool_source"] = "pool_d_discovery"

        all_pools = pool_a + pool_b + pool_c + pool_d

        if self._engine._verbose:
            LOG.info(
                "🎬 [Branch A] Fetched %d tracks (Entity:%d, Continuity:%d, Safe:%d, Discovery:%d)",
                len(all_pools),
                len(pool_a),
                len(pool_b),
                len(pool_c),
                len(pool_d),
            )

        return all_pools

    async def _fetch_cold_start_pools(
        self,
        *,
        seed_artist: str,
        seed_title: str,
        context: Any,
    ) -> List[Dict[str, Any]]:
        """
        Task 3.3: Fetch Branch B cold start pools (<3 songs history).

        Pool A: track.getSimilar(seed) - 60 tracks
        Pool C: tag.getTopTracks(focus_genres) - 30 tracks
        Total: ~90 tracks

        Seed-centric strategy for new sessions.
        """
        if self._engine._verbose:
            LOG.info("❄️ [Cold Start] Seed-centric fetch for '%s'", seed_title)

        # Phase 7 Task 8.1: Parallel pool fetching
        pool_a_task = self._fetch_track_similar(seed_artist, seed_title, limit=60)

        # Pool C: Safe harbor from top focus genre (only fetch when we have a genre)
        if context.focus_genres:
            top_genre = context.focus_genres[0]
            pool_a, pool_c = await asyncio.gather(
                pool_a_task,
                self._fetch_tag_top_tracks(top_genre, limit=30),
            )
        else:
            pool_a = await pool_a_task
            pool_c: List[Dict[str, Any]] = []

        for record in pool_a:
            record["pool_source"] = "pool_a_continuity"

        for record in pool_c:
            record["pool_source"] = "pool_c_safe_harbor"

        all_pools = pool_a + pool_c

        if self._engine._verbose:
            LOG.info(
                "❄️ [Cold Start] Fetched %d tracks (A:%d, C:%d)",
                len(all_pools),
                len(pool_a),
                len(pool_c),
            )

        return all_pools

    async def _fetch_hot_pool(
        self,
        *,
        seed_artist: str,
        seed_title: str,
        context: Any,
        tracker: Any,
    ) -> List[Dict[str, Any]]:
        """
        V2.5+: Fetch "Hot Pool" for STABLE phase (user is satisfied).
        
        This is a low-diversity, high-familiarity pool used when the user
        is in a flow state (low skip rate, long session). Emphasizes:
        - Strong similarity to current track (40 tracks)
        - Familiar artists from recent history (30 tracks)  
        - Core genre stability (30 tracks)
        - Minimal discovery (10 tracks, 10% novelty)
        
        Pool Strategy:
        - Prong 1: track.getSimilar(seed, 40) - HIGH similarity
        - Prong 2: artist.getTopTracks(recent_artists, 30) - HIGH familiarity
        - Prong 3: tag.getTopTracks(primary_genre, 30) - HIGH coherence
        - Prong 4: tag.getSimilar(primary_genre) -> topTracks(10) - MINIMAL discovery
        Total: ~110 tracks (90% familiarity + 10% novelty)
        
        Args:
            seed_artist: Current track artist
            seed_title: Current track title
            context: SessionContext with focus_genres
            tracker: ContextTracker for recent artists
            
        Returns:
            List of track records with pool_source='hot_pool_*'
        """
        if self._engine._verbose:
            LOG.info(
                "🔥 [Hot Pool] High-familiarity fetch for STABLE session: '%s'",
                seed_title,
            )

        focus_genres = getattr(context, "focus_genres", []) or []
        primary_genre = focus_genres[0] if len(focus_genres) > 0 else "rock"
        recent_artists = self._extract_recent_artists(tracker, max_artists=3)

        # Parallel fetch all prongs
        prong_1_task = self._fetch_track_similar(seed_artist, seed_title, limit=40)
        prong_2_tasks = [
            self._fetch_artist_top_tracks(artist, limit=10) for artist in recent_artists[:3]
        ]
        prong_3_task = self._fetch_tag_top_tracks(primary_genre, limit=30)
        
        # Prong 4: Minimal discovery via similar tags
        prong_4_similar_tags_task = self._fetch_similar_tags(primary_genre, limit=3)

        base_results = await asyncio.gather(
            prong_1_task,
            *prong_2_tasks,
            prong_3_task,
            prong_4_similar_tags_task,
            return_exceptions=True,
        )

        # Extract and filter pool_1 (ensure only dicts)
        pool_1_result = base_results[0] if isinstance(base_results[0], list) else []
        pool_1: List[Dict[str, Any]] = []
        if isinstance(pool_1_result, list):
            for item in pool_1_result:
                if isinstance(item, dict):
                    pool_1.append(item)
        
        # Combine Prong 2 results (recent artists) - ensure only dicts
        pool_2: List[Dict[str, Any]] = []
        for i in range(1, min(len(recent_artists) + 1, len(base_results))):
            result = base_results[i]
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict):
                        pool_2.append(item)
        
        # Extract and filter pool_3 (ensure only dicts)
        pool_3_idx = len(recent_artists) + 1
        pool_3_result = base_results[pool_3_idx] if pool_3_idx < len(base_results) else []
        pool_3: List[Dict[str, Any]] = []
        if isinstance(pool_3_result, list):
            for item in pool_3_result:
                if isinstance(item, dict):
                    pool_3.append(item)
        
        similar_tags_idx = pool_3_idx + 1
        similar_tags_result = base_results[similar_tags_idx] if similar_tags_idx < len(base_results) else []
        similar_tags = similar_tags_result if isinstance(similar_tags_result, list) else []

        # Prong 4: Fetch minimal discovery from first similar tag
        pool_4: List[Dict[str, Any]] = []
        if similar_tags and len(similar_tags) > 0:
            discovery_tag = similar_tags[0]
            if isinstance(discovery_tag, str):
                pool_4_result = await self._fetch_tag_top_tracks(discovery_tag, limit=10)
                if isinstance(pool_4_result, list):
                    pool_4 = pool_4_result

        # Tag pool sources
        for record in pool_1:
            if isinstance(record, dict):
                record["pool_source"] = "hot_pool_similarity"
        for record in pool_2:
            if isinstance(record, dict):
                record["pool_source"] = "hot_pool_familiarity"
        for record in pool_3:
            if isinstance(record, dict):
                record["pool_source"] = "hot_pool_coherence"
        for record in pool_4:
            if isinstance(record, dict):
                record["pool_source"] = "hot_pool_discovery"

        all_pools = pool_1 + pool_2 + pool_3 + pool_4

        if self._engine._verbose:
            LOG.info(
                "🔥 [Hot Pool] Fetched %d tracks (Similarity:%d, Familiarity:%d, Coherence:%d, Discovery:%d)",
                len(all_pools),
                len(pool_1),
                len(pool_2),
                len(pool_3),
                len(pool_4),
            )

        return all_pools

    async def _fetch_warm_start_pools(
        self,
        *,
        seed_artist: str,
        seed_title: str,
        context: Any,
        tracker: Any,
    ) -> List[Dict[str, Any]]:
        """
        Task 3.4: Fetch Branch B warm start pools (>=3 songs history).

        Pool A: track.getSimilar(seed) - 30 tracks
        Pool B: artist.getTopTracks(recent_artists) - 30 tracks
        Pool C: tag.getTopTracks(focus_genres) - 30 tracks
        Pool D: artist.getSimilar(most_liked_artist) -> topTracks - 20 tracks
        Total: ~110 tracks

        Profile-aware strategy with discovery component.
        """
        if self._engine._verbose:
            LOG.info("🔥 [Warm Start] Profile-aware fetch for '%s'", seed_title)

        # Phase 7 Task 8.1: Parallel pool fetching
        pool_a_task = self._fetch_track_similar(seed_artist, seed_title, limit=30)

        # Pool B: Familiarity from recent artists (fetch in parallel)
        recent_artists = self._extract_recent_artists(tracker, max_artists=3)
        pool_b_tasks = [
            self._fetch_artist_top_tracks(artist, limit=10) for artist in recent_artists
        ]

        # Pool C: Genre safe harbor
        pool_c_task = asyncio.sleep(0, result=[])
        if context.focus_genres:
            top_genre = context.focus_genres[0]
            pool_c_task = self._fetch_tag_top_tracks(top_genre, limit=30)

        # Execute Pool A, B, C in parallel
        pool_a, pool_b_results, pool_c = await asyncio.gather(
            pool_a_task,
            (
                asyncio.gather(*pool_b_tasks)
                if pool_b_tasks
                else asyncio.sleep(0, result=[])
            ),
            pool_c_task,
        )

        # Flatten Pool B results
        pool_b = []
        if isinstance(pool_b_results, list):
            for artist_tracks in pool_b_results:
                if isinstance(artist_tracks, list):
                    pool_b.extend(artist_tracks)

        for record in pool_a:
            record["pool_source"] = "pool_a_continuity"

        for record in pool_b:
            record["pool_source"] = "pool_b_familiarity"

        for record in pool_c:
            record["pool_source"] = "pool_c_safe_harbor"

        # Pool D: Discovery from similar artists (sequential due to dependencies)
        pool_d = []
        most_liked_artist = self._extract_most_liked_artist(tracker)
        if most_liked_artist:
            similar_artists = await self._fetch_artist_similar(
                most_liked_artist, limit=5
            )

            # Fetch discovery tracks in parallel
            discovery_tasks = []
            for artist_record in similar_artists[:5]:  # Limit to 5 similar artists
                artist_name = self._extract_artist(artist_record)
                if artist_name:
                    discovery_tasks.append(
                        self._fetch_artist_top_tracks(artist_name, limit=4)
                    )

            if discovery_tasks:
                discovery_results = await asyncio.gather(*discovery_tasks)
                for discovery_tracks in discovery_results:
                    pool_d.extend(discovery_tracks)
                    if len(pool_d) >= 20:
                        pool_d = pool_d[:20]  # Trim to limit
                        break

        for record in pool_d:
            record["pool_source"] = "pool_d_discovery"

        all_pools = pool_a + pool_b + pool_c + pool_d

        if self._engine._verbose:
            LOG.info(
                "🔥 [Warm Start] Fetched %d tracks (A:%d, B:%d, C:%d, D:%d)",
                len(all_pools),
                len(pool_a),
                len(pool_b),
                len(pool_c),
                len(pool_d),
            )

        return all_pools

    def _extract_recent_artists(self, tracker: Any, max_artists: int = 3) -> List[str]:
        """
        Extract recent artist names from session context.

        Args:
            tracker: ContextTracker instance
            max_artists: Maximum number of artists to return

        Returns:
            List of artist names (most recent first, deduplicated)
        """
        seen = set()
        recent = []

        # Access _history from tracker
        for track in reversed(tracker._history):
            if track.artist and track.artist not in seen:
                seen.add(track.artist)
                recent.append(track.artist)
                if len(recent) >= max_artists:
                    break

        return recent

    def _is_publisher_name(self, name: Optional[str]) -> bool:
        if not name:
            return False
        normalized = name.lower().strip()
        if not normalized:
            return False
        return any(keyword in normalized for keyword in _PUBLISHER_KEYWORDS)

    def _select_genre_tag(
        self,
        focus_genres: Sequence[str],
        index: int,
        default: str,
    ) -> Optional[str]:
        if focus_genres and index < len(focus_genres):
            candidate = str(focus_genres[index]).strip()
            if candidate:
                return candidate
        return default

    def _extract_most_liked_artist(self, tracker: Any) -> Optional[str]:
        """
        Extract artist with highest like count from session context.

        Args:
            tracker: ContextTracker instance

        Returns:
            Artist name with most likes, or None
        """
        from collections import defaultdict

        artist_likes: Dict[str, int] = defaultdict(int)

        # Count likes per artist
        for track in tracker._history:
            if track.artist and track.num_likes > 0:
                artist_likes[track.artist] += track.num_likes

        if not artist_likes:
            return None

        # Return artist with most likes
        return max(artist_likes.items(), key=lambda x: x[1])[0]

    def _apply_artist_diversity_filter(
        self,
        records: List[Dict[str, Any]],
        max_per_artist: int = 3,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """

        Groups tracks by artist, keeps max 3 per artist (sorted by playcount),
        stores remaining in artist_diversity_pool for future use.

        Args:
            records: List of track records from Last.fm API
            max_per_artist: Maximum tracks per artist (default: 3)

        Returns:
            Tuple of (filtered_records, overflow_pool)
        """
        from collections import defaultdict

        # Group by artist
        artist_tracks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for record in records:
            artist = self._extract_artist(record)
            if artist:
                artist_tracks[artist].append(record)

        # Filter: keep max 3 per artist (sorted by playcount)
        filtered = []
        overflow = []

        for artist, tracks in artist_tracks.items():
            # Sort by playcount (descending)
            sorted_tracks = sorted(
                tracks,
                key=lambda r: int(r.get("playcount", 0) or 0),
                reverse=True,
            )

            # Keep top N, overflow the rest
            filtered.extend(sorted_tracks[:max_per_artist])
            overflow.extend(sorted_tracks[max_per_artist:])

        return filtered, overflow
    
    async def _fetch_safe_harbor_pool(
        self,
        guild_id: int,
        tracker: ContextTracker,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Fetch safe harbor tracks from hierarchical artist sources.
        
        Strategy:
        1. REPLAYED artists (songs user explicitly replayed)
        2. LIKED artists (high finish rate, no skips)
        3. FINISHED artists (completed songs from session)
        
        Filter by popularity (playcount) to avoid B-sides and deep cuts.
        
        Args:
            guild_id: Discord guild ID
            tracker: ContextTracker instance for this guild
            limit: Maximum tracks to fetch (default: 10)
            
        Returns:
            List of safe harbor track records
        """
        safe_harbor_tracks: List[Dict[str, Any]] = []
        
        # Get artist pools from tracker history
        context = tracker.get_context()
        
        # Priority 1: Replayed artists (super-like signal)
        replayed_artists = []
        for track in tracker._history:
            if track.event_type == "replay":
                replayed_artists.append(track.artist)
        
        # Priority 2: Liked artists (finished with high ratio, no skips)
        liked_artists = []
        artist_stats: Dict[str, Dict[str, int]] = {}
        for track in tracker._history:
            artist = track.artist
            if artist not in artist_stats:
                artist_stats[artist] = {"finished": 0, "skipped": 0, "total": 0}
            
            artist_stats[artist]["total"] += 1
            if track.event_type == "finish":
                artist_stats[artist]["finished"] += 1
            elif track.was_skipped:
                artist_stats[artist]["skipped"] += 1
        
        # Filter artists with >75% finish rate and 0 skips
        for artist, stats in artist_stats.items():
            if stats["total"] >= 2:  # Need at least 2 plays
                finish_rate = stats["finished"] / stats["total"]
                if finish_rate >= 0.75 and stats["skipped"] == 0:
                    liked_artists.append(artist)
        
        # Priority 3: Finished artists (any completed song)
        finished_artists = []
        for track in tracker._history:
            if track.event_type == "finish" and track.artist not in replayed_artists and track.artist not in liked_artists:
                finished_artists.append(track.artist)
        
        # Deduplicate while preserving order
        replayed_artists = list(dict.fromkeys(replayed_artists))
        liked_artists = list(dict.fromkeys(liked_artists))
        finished_artists = list(dict.fromkeys(finished_artists))
        
        if self._verbose >= 2:
            LOG.debug(
                "🏰 [Safe Harbor] Artist pools: %d replayed, %d liked, %d finished",
                len(replayed_artists),
                len(liked_artists),
                len(finished_artists),
            )
        
        # Fetch top tracks from each artist pool (hierarchical)
        async def fetch_artist_top_tracks(artist: str, limit: int) -> List[Dict[str, Any]]:
            """Fetch artist's top tracks and filter by popularity."""
            tracks = await self._fetch_artist_top_tracks(artist, limit=limit)
            
            # Filter by playcount to avoid B-sides (keep top 50% by playcount)
            if tracks:
                sorted_tracks = sorted(
                    tracks,
                    key=lambda t: int(t.get("playcount", 0) or 0),
                    reverse=True,
                )
                # Keep top half (popular tracks only)
                cutoff = max(1, len(sorted_tracks) // 2)
                popular_tracks = sorted_tracks[:cutoff]
                
                # Mark source pool for debugging
                for track in popular_tracks:
                    track["_safe_harbor_source"] = f"artist:{artist}"
                
                return popular_tracks
            return []
        
        # Fetch from each pool until limit reached
        for artist in replayed_artists[:3]:  # Top 3 replayed artists
            if len(safe_harbor_tracks) >= limit:
                break
            tracks = await fetch_artist_top_tracks(artist, limit=5)
            safe_harbor_tracks.extend(tracks)
        
        if len(safe_harbor_tracks) < limit:
            for artist in liked_artists[:5]:  # Top 5 liked artists
                if len(safe_harbor_tracks) >= limit:
                    break
                tracks = await fetch_artist_top_tracks(artist, limit=3)
                safe_harbor_tracks.extend(tracks)
        
        if len(safe_harbor_tracks) < limit:
            for artist in finished_artists[:10]:  # Top 10 finished artists
                if len(safe_harbor_tracks) >= limit:
                    break
                tracks = await fetch_artist_top_tracks(artist, limit=2)
                safe_harbor_tracks.extend(tracks)
        
        # Trim to limit
        safe_harbor_tracks = safe_harbor_tracks[:limit]
        
        if self._verbose:
            LOG.info(
                "🏰 [Safe Harbor] Fetched %d tracks from %d artists",
                len(safe_harbor_tracks),
                len(set(self._extract_artist(t) for t in safe_harbor_tracks)),
            )
        
        return safe_harbor_tracks
    
    async def _fill_recommendation_buffer(
        self,
        guild_id: int,
        seed_artist: str,
        seed_title: str,
        context: Any,  # SessionContext
        tracker: ContextTracker,
        *,
        seed_track_type: str = "music",
        seed_entity: Optional[str] = None,
        is_canonical: bool = False,
    ) -> None:
        """
        Fill recommendation buffer with V3 Apple Music 5-slot strategy.
        
        V3 Buffer composition (NoveltyController-driven):
        - CORE slot (50%): High-confidence picks (score >= 7.5)
        - SIMILAR slot (30%): Related picks (score 7.0-7.5)
        - BRIDGE slot (15%): Connecting picks (score 6.5-7.0)
        - DISCOVERY slot (5%): Exploration picks (score 6.0-6.5)
        - SAFE_HARBOR slot (20% in REDISCOVERY): Replayed/liked artists
        
        Proportions dynamically adjust based on exploration phase detected by NoveltyController.
        
        This runs in background and results are consumed by get_recommendations_for_track.
        
        Args:
            guild_id: Discord guild ID
            seed_artist: Seed track artist
            seed_title: Seed track title
            context: SessionContext from tracker
            tracker: ContextTracker instance
        """
        try:
            if self._verbose:
                LOG.info(
                    "🔄 [V3 Buffer] Starting buffer fill for guild %d (seed: %s - %s)",
                    guild_id,
                    seed_artist,
                    seed_title,
                )
            
            # V3: Calculate session duration for phase detection
            session_duration_minutes = (context.last_activity - context.session_start) / 60.0
            
            # V3: Detect exploration phase using NoveltyController
            phase = self._novelty_controller.detect_exploration_phase(
                skip_rate=context.skip_rate,
                songs_since_novelty=context.songs_since_novelty,
                session_duration_minutes=session_duration_minutes,
            )
            proportions = self._novelty_controller.get_candidate_proportions(phase)
            
            if self._verbose:
                LOG.info(
                    "🔮 [V3 Buffer] Phase: %s, Proportions: core=%.0f%% similar=%.0f%% bridge=%.0f%% discovery=%.0f%% safe_harbor=%.0f%%",
                    phase.name,
                    proportions["core"] * 100,
                    proportions["similar"] * 100,
                    proportions["bridge"] * 100,
                    proportions["discovery"] * 100,
                    proportions["safe_harbor"] * 100,
                )
            
            # Fetch candidate pool
            seed_track_type = "music"  # Simplified for buffer fill
            candidate_records = await self._fetch_candidate_records(
                guild_id=guild_id,
                seed_artist=seed_artist,
                seed_title=seed_title,
                seed_track_type=seed_track_type,
                seed_entity=seed_entity,
                is_canonical=is_canonical,
                context=context,
                tracker=tracker,
            )
            
            if not candidate_records:
                if self._verbose:
                    LOG.warning("🔄 [V3 Buffer] No candidates fetched, buffer fill aborted")
                return
            
            # Prepare and score candidates
            prepared_candidates = await self._prepare_candidates(guild_id, candidate_records)
            if not prepared_candidates:
                if self._verbose:
                    LOG.warning("🔄 [V3 Buffer] No viable candidates after enrichment")
                return
            
            features = [entry.features for entry in prepared_candidates]
            seed_track_id = self._track_id(seed_artist, seed_title)
            
            # Score candidates
            scored = self._engine.score_candidates(
                guild_id,
                features,
                seed_track_ids=[seed_track_id],
                session_mood_vector=context.current_mood_vector,
                target_mood=None,
                session_focus_genres=context.focus_genres,
                liked_mood_vector=context.liked_mood_vector,
                energy_trend=context.energy_trend,
                last_energy=None,
            )
            
            # Create mapping from track_id to (PreparedCandidate, score)
            # scored and prepared_candidates should be parallel lists in same order
            candidate_map = {}
            for scored_candidate, prepared_entry in zip(scored, prepared_candidates):
                candidate_map[scored_candidate.track_id] = (prepared_entry, scored_candidate.score)
            
            if self._verbose >= 2:
                LOG.debug(
                    f"🔍 [V3 Buffer Debug] Created {len(candidate_map)} candidate mappings from "
                    f"{len(scored)} scored and {len(prepared_candidates)} prepared"
                )
            
            # V3: Classify candidates into 5 pools by score ranges (0-1 scale)
            core_pool: List[PreparedCandidate] = []      # score >= 0.55 (highly compatible)
            similar_pool: List[PreparedCandidate] = []   # 0.50 <= score < 0.55 (safe picks)
            bridge_pool: List[PreparedCandidate] = []    # 0.45 <= score < 0.50 (moderate variety)
            discovery_pool: List[PreparedCandidate] = [] # 0.40 <= score < 0.45 (adventurous)
            
            for candidate, score in candidate_map.values():
                if score >= 0.55:
                    core_pool.append(candidate)
                elif score >= 0.50:
                    similar_pool.append(candidate)
                elif score >= 0.45:
                    bridge_pool.append(candidate)
                elif score >= 0.40:
                    discovery_pool.append(candidate)
            
            if self._verbose >= 2:
                LOG.debug(
                    "🎯 [V3 Buffer] Pools: core=%d similar=%d bridge=%d discovery=%d",
                    len(core_pool),
                    len(similar_pool),
                    len(bridge_pool),
                    len(discovery_pool),
                )
            elif self._verbose >= 1:
                # Show pool distribution summary at verbose=1
                if candidate_map:
                    max_score = max((score for _, score in candidate_map.values()))
                    LOG.info(
                        "🎯 [V3 Buffer] Pools: core=%d similar=%d bridge=%d discovery=%d (top_score=%.3f)",
                        len(core_pool),
                        len(similar_pool),
                        len(bridge_pool),
                        len(discovery_pool),
                        max_score,
                    )
                else:
                    LOG.warning("⚠️ [V3 Buffer] No candidates mapped (candidate_map is empty)")
            
            # V3: Allocate slots based on proportions
            target_buffer_size = self._buffer_fill_size
            buffer: List[PreparedCandidate] = []
            
            slot_keys = ["core", "similar", "bridge", "discovery", "safe_harbor"]
            raw_counts = {
                slot: target_buffer_size * proportions.get(slot, 0.0)
                for slot in slot_keys
            }
            slot_counts = {slot: int(raw_counts[slot]) for slot in slot_keys}
            assigned = sum(slot_counts.values())
            remainder = target_buffer_size - assigned

            if remainder > 0:
                ordered = sorted(
                    slot_keys,
                    key=lambda slot: (raw_counts[slot] - slot_counts[slot], raw_counts[slot]),
                    reverse=True,
                )
                for slot in ordered:
                    if remainder == 0:
                        break
                    slot_counts[slot] += 1
                    remainder -= 1

            core_min = 1
            discovery_min = 1 if proportions.get("discovery", 0.0) > 0 else 0
            if slot_counts["core"] < core_min:
                slot_counts["core"] = core_min
            if slot_counts["discovery"] < discovery_min:
                slot_counts["discovery"] = discovery_min

            total_slots = sum(slot_counts.values())
            if total_slots < target_buffer_size:
                additive_order = ["core", "similar", "bridge", "discovery", "safe_harbor"]
                while total_slots < target_buffer_size:
                    for slot in additive_order:
                        slot_counts[slot] += 1
                        total_slots += 1
                        if total_slots >= target_buffer_size:
                            break
            elif total_slots > target_buffer_size:
                reducible_order = ["safe_harbor", "similar", "bridge", "discovery", "core"]
                while total_slots > target_buffer_size:
                    for slot in reducible_order:
                        min_allowed = core_min if slot == "core" else discovery_min if slot == "discovery" else 0
                        if slot_counts[slot] > min_allowed:
                            slot_counts[slot] -= 1
                            total_slots -= 1
                            break
                    else:
                        break

            core_count = slot_counts["core"]
            similar_count = slot_counts["similar"]
            bridge_count = slot_counts["bridge"]
            discovery_count = slot_counts["discovery"]
            safe_harbor_count = slot_counts["safe_harbor"]
            
            # Fill CORE slot
            if core_pool:
                buffer.extend(random.sample(core_pool, min(core_count, len(core_pool))))
            elif similar_pool:
                # Fallback: use similar pool if core empty
                buffer.extend(random.sample(similar_pool, min(core_count, len(similar_pool))))
            
            # Fill SIMILAR slot
            if similar_pool:
                # Avoid duplicates from core fallback
                available = [c for c in similar_pool if c not in buffer]
                buffer.extend(random.sample(available, min(similar_count, len(available))))
            
            # Fill BRIDGE slot
            if bridge_pool:
                buffer.extend(random.sample(bridge_pool, min(bridge_count, len(bridge_pool))))
            
            # Fill DISCOVERY slot
            if discovery_pool:
                buffer.extend(random.sample(discovery_pool, min(discovery_count, len(discovery_pool))))
            
            # Fill SAFE_HARBOR slot (if phase requires it)
            if safe_harbor_count > 0:
                safe_harbor_records = await self._fetch_safe_harbor_pool(
                    guild_id, tracker, limit=safe_harbor_count * 2  # Fetch extra for filtering
                )
                if safe_harbor_records:
                    # Prepare safe harbor candidates
                    safe_harbor_prepared = await self._prepare_candidates(
                        guild_id, safe_harbor_records
                    )
                    if safe_harbor_prepared:
                        # Pick randomly up to safe_harbor_count
                        buffer.extend(
                            random.sample(
                                safe_harbor_prepared,
                                min(safe_harbor_count, len(safe_harbor_prepared)),
                            )
                        )
            
            # Shuffle buffer to avoid predictable patterns
            random.shuffle(buffer)
            
            # Store buffer
            self._recommendation_buffer[guild_id] = buffer
            
            if self._verbose:
                LOG.info(
                    "✅ [V3 Buffer] Filled with %d tracks (phase: %s) for guild %d",
                    len(buffer),
                    phase.name,
                    guild_id,
                )
        
        except asyncio.CancelledError:
            if self._verbose >= 2:
                LOG.debug("🚫 [V3 Buffer] Fill task cancelled for guild %d", guild_id)
            raise
        except Exception as exc:
            LOG.error("❌ [V3 Buffer] Fill task failed for guild %d: %s", guild_id, exc)

    # ------------------------------------------------------------------
    # Legacy fallback methods (kept for backward compatibility)
    # ------------------------------------------------------------------

    async def _call_lastfm(
        self,
        params: Dict[str, str],
        *,
        source: str,
    ) -> List[Dict[str, Any]]:
        try:
            async with aiohttp.ClientSession(timeout=self._http_timeout) as session:
                async with session.get(_LASTFM_API_URL, params=params) as response:
                    if response.status != 200:
                        LOG.warning(
                            "❌ Last.fm %s failed (HTTP %d) - artist=%s, track=%s",
                            source,
                            response.status,
                            params.get("artist", "N/A"),
                            params.get("track", params.get("tag", "N/A")),
                        )
                        return []
                    payload = await response.json(content_type=None)
        except Exception as exc:
            LOG.warning(
                "❌ Last.fm %s request failed: %s - artist=%s, track=%s",
                source,
                exc,
                params.get("artist", "N/A"),
                params.get("track", params.get("tag", "N/A")),
            )
            return []

        # Check for Last.fm API errors in response
        if "error" in payload:
            error_code = payload.get("error", "unknown")
            error_msg = payload.get("message", "No error message")
            LOG.warning(
                "❌ Last.fm %s API error %s: %s - artist=%s, track=%s",
                source,
                error_code,
                error_msg,
                params.get("artist", "N/A"),
                params.get("track", params.get("tag", "N/A")),
            )
            return []

        if source == "track.getSimilar":
            tracks = payload.get("similartracks", {}).get("track", [])
        elif source == "artist.getTopTracks":
            tracks = payload.get("toptracks", {}).get("track", [])
        elif source == "artist.getSimilar":
            # For artist.getSimilar, we return artist records, not track records
            artists = payload.get("similarartists", {}).get("artist", [])
            if isinstance(artists, dict):
                artists = [artists]
            return artists if isinstance(artists, list) else []
        else:
            tracks = []

        if isinstance(tracks, dict):
            tracks = [tracks]
        if not isinstance(tracks, list):
            if self._engine._verbose:
                LOG.debug(
                    "⚠️ Last.fm %s returned invalid format (not list/dict) - artist=%s, track=%s",
                    source,
                    params.get("artist", "N/A"),
                    params.get("track", params.get("tag", "N/A")),
                )
            return []

        trimmed: List[Dict[str, Any]] = []
        for record in tracks:
            if not isinstance(record, dict):
                continue
            record_copy = dict(record)
            record_copy.setdefault("_source", source)
            trimmed.append(record_copy)
            if len(trimmed) >= _DEFAULT_FETCH_LIMIT:
                break

        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][lastfm] parsed %d %s records",
                len(trimmed),
                source,
            )

        # Log warning if no tracks found (might be obscure artist/track)
        if len(trimmed) == 0:
            LOG.warning(
                "⚠️ Last.fm %s returned 0 results - artist='%s', track='%s' (obscure/misspelled?)",
                source,
                params.get("artist", "N/A"),
                params.get("track", params.get("tag", "N/A")),
            )

        return trimmed

    # ------------------------------------------------------------------
    # Last.fm API Helper Methods 
    # ------------------------------------------------------------------

    async def _fetch_track_similar(
        self,
        artist: str,
        title: str,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Fetch similar tracks using Last.fm's track.getSimilar method.

        Args:
            artist: Artist name
            title: Track title
            limit: Maximum number of results (default: 30)

        Returns:
            List of track records with _source='track.getSimilar'
        """
        if not self._lastfm_key:
            return []

        params = {
            "method": "track.getSimilar",
            "artist": artist,
            "track": title,
            "limit": str(limit),
            "autocorrect": "1",
            "api_key": self._lastfm_key,
            "format": "json",
        }

        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][pool] track.getSimilar artist=%s title=%s limit=%d",
                artist,
                title,
                limit,
            )

        return await self._call_lastfm(params, source="track.getSimilar")

    async def _fetch_artist_top_tracks(
        self,
        artist: str,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Fetch artist's top tracks using Last.fm's artist.getTopTracks method.

        Args:
            artist: Artist name
            limit: Maximum number of results (default: 30)

        Returns:
            List of track records with _source='artist.getTopTracks'
        """
        if not self._lastfm_key:
            return []

        params = {
            "method": "artist.getTopTracks",
            "artist": artist,
            "limit": str(limit),
            "autocorrect": "1",
            "api_key": self._lastfm_key,
            "format": "json",
        }

        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][pool] artist.getTopTracks artist=%s limit=%d",
                artist,
                limit,
            )

        return await self._call_lastfm(params, source="artist.getTopTracks")

    async def _fetch_tag_top_tracks(
        self,
        tag: str,
        limit: int = 40,
    ) -> List[Dict[str, Any]]:
        """
        Fetch top tracks by tag/genre using Last.fm's tag.getTopTracks method.

        This is used for:
        - OST/entity-based recommendations (e.g., tag='Hazbin Hotel')
        - Genre-based safe harbor (e.g., tag='electronic')

        Args:
            tag: Tag name (genre, entity, etc.)
            limit: Maximum number of results (default: 40)

        Returns:
            List of track records with _source='tag.getTopTracks'
        """
        if not self._lastfm_key:
            return []

        params = {
            "method": "tag.getTopTracks",
            "tag": tag,
            "limit": str(limit),
            "api_key": self._lastfm_key,
            "format": "json",
        }

        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][pool] tag.getTopTracks tag=%s limit=%d",
                tag,
                limit,
            )

        # Tag API has different response structure
        try:
            async with aiohttp.ClientSession(timeout=self._http_timeout) as session:
                async with session.get(_LASTFM_API_URL, params=params) as response:
                    if response.status != 200:
                        LOG.debug(
                            "Last.fm tag.getTopTracks failed (%s)", response.status
                        )
                        return []
                    payload = await response.json(content_type=None)
        except Exception as exc:
            LOG.warning("Last.fm tag.getTopTracks request failed: %s", exc)
            return []

        # Extract tracks from tag API response
        tracks = payload.get("tracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not isinstance(tracks, list):
            return []

        trimmed: List[Dict[str, Any]] = []
        for record in tracks:
            if not isinstance(record, dict):
                continue
            record_copy = dict(record)
            record_copy.setdefault("_source", "tag.getTopTracks")
            trimmed.append(record_copy)
            if len(trimmed) >= limit:
                break

        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][pool] tag.getTopTracks returned %d tracks for tag=%s",
                len(trimmed),
                tag,
            )

        return trimmed

    async def _fetch_similar_tags(
        self,
        tag: str,
        limit: int = 5,
    ) -> List[str]:
        if not self._lastfm_key or not tag:
            return []

        params = {
            "method": "tag.getSimilar",
            "tag": tag,
            "limit": str(limit),
            "api_key": self._lastfm_key,
            "format": "json",
        }

        try:
            async with aiohttp.ClientSession(timeout=self._http_timeout) as session:
                async with session.get(_LASTFM_API_URL, params=params) as response:
                    if response.status != 200:
                        if self._engine._verbose:
                            LOG.debug(
                                "Last.fm tag.getSimilar failed (%s) for tag=%s",
                                response.status,
                                tag,
                            )
                        return []
                    payload = await response.json(content_type=None)
        except Exception as exc:
            LOG.debug("Last.fm tag.getSimilar request failed for %s: %s", tag, exc)
            return []

        tags = payload.get("similartags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        if not isinstance(tags, list):
            return []

        results: List[str] = []
        for entry in tags:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            results.append(name)
            if len(results) >= limit:
                break

        return results

    async def _fetch_discovery_tag_tracks(
        self,
        *,
        seed_entity: str,
        focus_genres: Sequence[str],
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        normalized_entity = seed_entity.lower().strip()
        discovery_tags = await self._fetch_similar_tags(seed_entity, limit=5)

        # Build a prioritized list of tags to try
        sequence: List[str] = []
        sequence.extend(discovery_tags)
        sequence.extend(str(g).strip() for g in focus_genres[:2] if g)
        sequence.extend(_DISCOVERY_TAG_FALLBACKS)

        seen: set[str] = set()
        for tag in sequence:
            candidate = str(tag).strip()
            if not candidate:
                continue
            candidate_lower = candidate.lower()
            if candidate_lower == normalized_entity:
                continue
            if candidate_lower in seen:
                continue
            seen.add(candidate_lower)

            tracks = await self._fetch_tag_top_tracks(candidate, limit=limit)
            if tracks:
                if self._engine._verbose:
                    LOG.debug(
                        "[Branch A] Discovery tag '%s' yielded %d tracks",
                        candidate,
                        len(tracks),
                    )
                return tracks

        return []

    async def _fetch_artist_similar(
        self,
        artist: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Fetch similar artists using Last.fm's artist.getSimilar method.

        Returns artist records (not track records). Use this for Pool D discovery
        by fetching similar artists, then getting their top tracks.

        Args:
            artist: Artist name
            limit: Maximum number of results (default: 20)

        Returns:
            List of artist records with _source='artist.getSimilar'
        """
        if not self._lastfm_key:
            return []

        params = {
            "method": "artist.getSimilar",
            "artist": artist,
            "limit": str(limit),
            "autocorrect": "1",
            "api_key": self._lastfm_key,
            "format": "json",
        }

        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][pool] artist.getSimilar artist=%s limit=%d",
                artist,
                limit,
            )

        return await self._call_lastfm(params, source="artist.getSimilar")

    async def _prepare_candidates(
        self,
        guild_id: int,
        records: Sequence[Dict[str, Any]],
    ) -> List[PreparedCandidate]:
        seen: set[str] = set()
        candidates_to_prepare: List[tuple[int, str, str, str, Dict[str, Any]]] = []

        # Get recent history to filter out immediate repetitions and artist cooldown
        history = self._recent_history.get(guild_id, [])
        recent_track_ids = {
            entry.get("track_id") for entry in history[-_RECENT_TRACKS_FILTER_SIZE:]
        }
        recent_artists = {
            entry.get("artist")
            for entry in history[-_ARTIST_COOLDOWN_SIZE:]
            if entry.get("artist")
        }

        # First pass: collect all unique candidates, filter out episodes and recent plays
        for index, record in enumerate(records):
            artist = self._extract_artist(record)
            title = self._extract_title(record)
            if not artist or not title:
                continue

            # Filter out obvious episode/non-music content
            if self._is_episode_content(title):
                if self._engine._verbose:
                    LOG.debug(
                        "[AutoplayV2][filter] Rejected episode content: '%s' by '%s'",
                        title,
                        artist,
                    )
                continue

            track_id = self._track_id(artist, title)

            # Skip if this track was recently played
            if track_id in recent_track_ids:
                if self._engine._verbose:
                    LOG.debug(
                        "[AutoplayV2][filter] Rejected recently played: '%s' by '%s'",
                        title,
                        artist,
                    )
                continue

            # Skip if this artist was played in the last N tracks (cooldown)
            artist_normalized = artist.lower().strip()
            if artist_normalized in recent_artists:
                if self._engine._verbose:
                    LOG.debug(
                        "[AutoplayV2][filter] Rejected artist cooldown: '%s' by '%s' (played recently)",
                        title,
                        artist,
                    )
                continue

            if track_id in seen:
                continue
            seen.add(track_id)
            candidates_to_prepare.append((index, track_id, artist, title, record))

        # Second pass: Check cache for enrichment (Last.fm already provides clean metadata, no parsing needed)
        enrichment_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        pending_enrichments: Dict[str, asyncio.Future] = {}
        tracks_needing_enrichment: List[Tuple[str, str, str]] = (
            []
        )  

        # Store clean metadata for later use
        parsed_metadata: Dict[str, Tuple[str, str]] = (
            {}
        ) 

        # Reset cache stats for this round
        self._cache_stats = {
            "enrichment_hits": 0,
            "enrichment_misses": 0,
            "gemini_calls": 0,
        }

        for _, track_id, artist, title, _ in candidates_to_prepare:
            # Last.fm provides clean artist/title - use directly
            clean_artist = artist
            clean_title = title
            parsed_metadata[track_id] = (clean_artist, clean_title)

            # Check if already enriched in cache using CLEAN keys
            cached = await self._engine._cache.get_enrichment(clean_artist, clean_title)
            if cached:
                self._cache_stats["enrichment_hits"] += 1
                # Build V3 enrichment dict from cached entry
                # V3 uses computed_simple_vibe (5D: energy, valence, danceability, acousticness, brightness)
                mood_vector = None
                if cached.computed_simple_vibe and len(cached.computed_simple_vibe) >= 3:
                    # Map 5D vibe to mood_vector for compatibility
                    vibe = cached.computed_simple_vibe
                    mood_vector = {
                        "energy": vibe[0],  # energy
                        "valence": vibe[1],  # valence
                        "tempo": cached.computed_tempo or 0.0,  # BPM from analysis
                        "confidence": 0.9 if cached.analysis_verified else 0.5,
                        "vector": vibe[:3],  # First 3 dimensions
                    }
                
                enrichment_cache[track_id] = {
                    "tags": cached.tags,
                    "mood": cached.mood,
                    "energy": cached.activity_affinity,  # Cultural context
                    "mood_vector": mood_vector,
                    "computed_embedding": cached.computed_embedding,
                    "computed_simple_vibe": cached.computed_simple_vibe,
                    "computed_tempo": cached.computed_tempo,
                    "computed_loudness": cached.computed_loudness,
                    "computed_key": cached.computed_key,
                    "computed_mode": cached.computed_mode,
                    "analysis_verified": cached.analysis_verified,
                }
            else:
                # Collect for batch enrichment
                self._cache_stats["enrichment_misses"] += 1
                tracks_needing_enrichment.append((track_id, clean_artist, clean_title))

        # Log batch enrichment stats
        if self._engine._verbose and (enrichment_cache or tracks_needing_enrichment):
            total = len(enrichment_cache) + len(tracks_needing_enrichment)
            LOG.info(
                "📦 [Batch Enrichment] %d total candidates: %d from cache, %d need Gemini",
                total,
                len(enrichment_cache),
                len(tracks_needing_enrichment),
            )

        # Enqueue all enrichment requests at once to maximize batching
        if tracks_needing_enrichment:
            for track_id, artist, title in tracks_needing_enrichment:
                try:
                    future = await self._engine._gemini._enqueue_enrichment_future(
                        artist,
                        title,
                        existing_tags=[],
                        allow_grounding=False,
                    )
                    pending_enrichments[track_id] = future
                except Exception as exc:
                    LOG.debug("Failed to enqueue enrichment for %s: %s", track_id, exc)
                    enrichment_cache[track_id] = None

        # Wait for all pending enrichments to complete (they batch automatically)
        if pending_enrichments:
            await asyncio.gather(*pending_enrichments.values(), return_exceptions=True)

            # Track Gemini usage for this batch
            self._cache_stats["gemini_calls"] += 1

            # CRITICAL FIX: Store ALL batch results in cache (not just the selected track)
            await self._store_batch_enrichments(tracks_needing_enrichment)

            # Process completed enrichments (mood vector now included in enrichment response)
            for track_id, future in pending_enrichments.items():
                try:
                    if future.done() and not future.cancelled():
                        result = future.result()
                        if result:
                            # Get clean artist/title from parsed metadata
                            clean_artist, clean_title = parsed_metadata.get(
                                track_id, (None, None)
                            )

                            if clean_artist and clean_title:
                                cached_entry = await self._engine._cache.get_enrichment(
                                    clean_artist, clean_title
                                )
                                if cached_entry:
                                    # Build V3 enrichment dict from cached entry
                                    mood_vector = None
                                    if cached_entry.computed_simple_vibe and len(cached_entry.computed_simple_vibe) >= 3:
                                        vibe = cached_entry.computed_simple_vibe
                                        mood_vector = {
                                            "energy": vibe[0],
                                            "valence": vibe[1],
                                            "tempo": cached_entry.computed_tempo or 0.0,
                                            "confidence": 0.9 if cached_entry.analysis_verified else 0.5,
                                            "vector": vibe[:3],
                                        }
                                    
                                    enrichment_cache[track_id] = {
                                        "tags": cached_entry.tags,
                                        "mood": cached_entry.mood,
                                        "energy": cached_entry.activity_affinity,
                                        "mood_vector": mood_vector,
                                        "computed_embedding": cached_entry.computed_embedding,
                                        "computed_simple_vibe": cached_entry.computed_simple_vibe,
                                        "computed_tempo": cached_entry.computed_tempo,
                                        "computed_loudness": cached_entry.computed_loudness,
                                        "computed_key": cached_entry.computed_key,
                                        "computed_mode": cached_entry.computed_mode,
                                        "analysis_verified": cached_entry.analysis_verified,
                                    }
                                else:
                                    tags = [
                                        str(tag).lower()
                                        for tag in result.get("tags", [])
                                        if isinstance(tag, str)
                                    ]
                                    moods = [
                                        str(mood).strip()
                                        for mood in result.get("moods", [])
                                        if isinstance(mood, str) and mood.strip()
                                    ]
                                    mood_value = moods[0] if moods else None
                                    energy = (
                                        result.get("energy")
                                        if isinstance(result.get("energy"), str)
                                        else None
                                    )
                                    enrichment_cache[track_id] = {
                                        "tags": tags,
                                        "mood": mood_value,
                                        "energy": energy,
                                        "mood_vector": result.get("mood_vector"),
                                    }
                            else:
                                enrichment_cache[track_id] = None
                        else:
                            enrichment_cache[track_id] = None
                except Exception as exc:
                    LOG.debug(
                        "Failed to process enrichment result for %s: %s", track_id, exc
                    )
                    enrichment_cache[track_id] = None

        # Third pass: prepare candidates with pre-fetched enrichment
        tasks: List[asyncio.Task[Optional[PreparedCandidate]]] = []
        for index, track_id, raw_artist, raw_title, record in candidates_to_prepare:
            enrichment = enrichment_cache.get(track_id)
            clean_artist, clean_title = parsed_metadata.get(
                track_id, (raw_artist, raw_title)
            )
            tasks.append(
                asyncio.create_task(
                    self._prepare_single_candidate(
                        guild_id,
                        track_id,
                        clean_artist,
                        clean_title,
                        record,
                        index,
                        enrichment,
                    )
                )
            )

        prepared: List[PreparedCandidate] = []
        for task in tasks:
            try:
                result = await task
            except Exception as exc: 
                LOG.debug("Candidate preparation failed: %s", exc)
                continue
            if result is not None:
                prepared.append(result)

        # V3: Check for candidates missing audio analysis (analysis_verified)
        # Per design: "If not enriched, process it as prio 1. User waits."
        candidates_needing_analysis = []
        for prepared_entry in prepared:
            track_id = prepared_entry.features.track_id
            enrichment = enrichment_cache.get(track_id)
            if not enrichment or not enrichment.get("analysis_verified"):
                artist, title = parsed_metadata.get(
                    track_id, (prepared_entry.features.artist, prepared_entry.features.title)
                )
                candidates_needing_analysis.append((artist, title))
        
        # Request P1 enrichment for candidates missing audio analysis
        if candidates_needing_analysis and hasattr(self._engine, '_enrichment_worker'):
            if self._verbose >= 1:
                LOG.info(
                    "⏳ [P1 Enrichment] %d candidates need audio analysis, requesting priority enrichment...",
                    len(candidates_needing_analysis)
                )
            
            # Batch request P1 (BUFFER priority) enrichment
            enriched_count = await self._engine._enrichment_worker.enrich_batch_for_buffer(
                candidates_needing_analysis,
                max_wait=30.0,  # Wait up to 30s for enrichment
            )
            
            if self._verbose >= 1:
                LOG.info(
                    "✅ [P1 Enrichment] Enriched %d/%d candidates",
                    enriched_count, len(candidates_needing_analysis)
                )
            
            # Re-fetch enrichment data for newly enriched candidates
            if enriched_count > 0:
                for prepared_entry in prepared:
                    track_id = prepared_entry.features.track_id
                    enrichment = enrichment_cache.get(track_id)
                    if not enrichment or not enrichment.get("analysis_verified"):
                        artist, title = parsed_metadata.get(
                            track_id, (prepared_entry.features.artist, prepared_entry.features.title)
                        )
                        # Re-check cache for updated entry
                        cached = await self._engine._cache.get_enrichment(artist, title)
                        if cached and cached.analysis_verified:
                            # Update features with new audio analysis data
                            if cached.computed_embedding:
                                prepared_entry.features.computed_embedding = cached.computed_embedding
                                prepared_entry.features.computed_embedding_model = cached.computed_embedding_model
                                prepared_entry.features.computed_embedding_dim = len(cached.computed_embedding)
                            if cached.computed_simple_vibe:
                                prepared_entry.features.computed_simple_vibe = cached.computed_simple_vibe
                            if cached.computed_tempo is not None:
                                prepared_entry.features.computed_tempo = cached.computed_tempo
                            if cached.computed_loudness is not None:
                                prepared_entry.features.computed_loudness = cached.computed_loudness
                            if cached.computed_key is not None:
                                prepared_entry.features.computed_key = cached.computed_key
                            if cached.computed_mode is not None:
                                prepared_entry.features.computed_mode = cached.computed_mode
        
        # Log cache performance summary
        if self._verbose >= 1:
            total_checks = (
                self._cache_stats["enrichment_hits"]
                + self._cache_stats["enrichment_misses"]
            )
            hit_rate = (
                (self._cache_stats["enrichment_hits"] / total_checks * 100)
                if total_checks > 0
                else 0
            )
            LOG.info(
                "📊 [Cache Performance] Enrichment: %d/%d hits (%.1f%%), Gemini calls: %d",
                self._cache_stats["enrichment_hits"],
                total_checks,
                hit_rate,
                self._cache_stats["gemini_calls"],
            )

        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][prepare] kept %d/%d candidates",
                len(prepared),
                len(records),
            )
        return prepared

    async def _store_batch_enrichments(
        self, tracks_needing_enrichment: List[Tuple[str, str, str]]
    ) -> None:
        """Store all batch enrichment results in cache (fixes 98% quota waste bug)."""
        batch_results = self._engine._gemini.get_last_batch_results()
        if not batch_results:
            return

        stored_count = 0
        for track_id, artist, title in tracks_needing_enrichment:
            normalized_key = f"{artist.strip().lower()}::{title.strip().lower()}"
            payload = batch_results.get(normalized_key)

            if not payload:
                continue

            try:
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
                    str(tag).lower().strip()
                    for tag in payload.get("tags", [])
                    if isinstance(tag, str) and str(tag).strip()
                ]
                if tags:
                    tags = list(dict.fromkeys(tags))

                moods = [
                    str(m).strip()
                    for m in payload.get("moods", [])
                    if isinstance(m, str) and m.strip()
                ]
                mood_value = moods[0] if moods else None

                raw_genres = payload.get("genres")
                genres: List[str] = []
                if isinstance(raw_genres, list):
                    genres = [
                        str(genre).strip()
                        for genre in raw_genres
                        if isinstance(genre, str) and str(genre).strip()
                    ]
                elif isinstance(raw_genres, str) and raw_genres.strip():
                    genres = [raw_genres.strip()]
                elif isinstance(payload.get("genre"), str) and payload["genre"].strip():
                    genres = [payload["genre"].strip()]

                # V3: Only load computed audio analysis fields
                raw_simple_vibe = (
                    payload.get("computed_simple_vibe")
                    or payload.get("computed_vibe_vector")
                    or payload.get("vibe_vector")
                )
                computed_simple_vibe: Optional[List[float]] = None
                if isinstance(raw_simple_vibe, list):
                    sanitized: List[float] = []
                    for value in raw_simple_vibe:
                        parsed = _safe_float(value)
                        if parsed is None:
                            sanitized = []
                            break
                        sanitized.append(parsed)
                    if len(sanitized) >= 5:
                        computed_simple_vibe = sanitized[:5]

                computed_loudness = _safe_float(payload.get("computed_loudness"))
                computed_tempo = _safe_float(payload.get("computed_tempo"))
                computed_key = _safe_int(payload.get("computed_key"))
                computed_mode = _safe_int(payload.get("computed_mode"))

                bpm_val = payload.get("bpm")
                key_val = payload.get("key")
                activity_val = payload.get("activity_affinity")
                intensity_val = payload.get("emotional_intensity")
                daypart_val = payload.get("daypart_affinity")

                from .cache_manager import EnrichmentEntry

                enrichment_entry = EnrichmentEntry(
                    tags=tags,
                    mood=mood_value,
                    fetched_at=time.time(),
                    bpm=_safe_int(bpm_val),
                    key=(
                        str(key_val).strip()
                        if isinstance(key_val, str) and key_val.strip()
                        else None
                    ),
                    activity_affinity=(
                        str(activity_val).strip()
                        if isinstance(activity_val, str) and activity_val.strip()
                        else None
                    ),
                    emotional_intensity=_safe_float(intensity_val),
                    daypart_affinity=(
                        str(daypart_val).strip()
                        if isinstance(daypart_val, str) and daypart_val.strip()
                        else None
                    ),
                    genres=genres,
                    computed_simple_vibe=computed_simple_vibe,
                    computed_loudness=computed_loudness,
                    computed_tempo=computed_tempo,
                    computed_key=computed_key,
                    computed_mode=computed_mode,
                )

                # V3: No Gemini vibe estimates - wait for Librosa/MobileNet analysis

                await self._engine._cache.set_enrichment(
                    artist, title, enrichment_entry
                )
                stored_count += 1
            except Exception as exc:
                LOG.debug(
                    f"Failed to store batch enrichment for {normalized_key}: {exc}"
                )

        if stored_count > 0:
            LOG.info(
                f"💾 [Batch Storage] Cached {stored_count}/{len(tracks_needing_enrichment)} enrichments from batch"
            )

    async def _prepare_single_candidate(
        self,
        guild_id: int,
        track_id: str,
        artist: str,
        title: str,
        record: Dict[str, Any],
        rank_index: int,
        pre_fetched_enrichment: Optional[Dict[str, Any]] = None,
    ) -> Optional[PreparedCandidate]:
        content_similarity = self._extract_match(record)
        if content_similarity < _MIN_CONTENT_SIMILARITY:
            content_similarity = _MIN_CONTENT_SIMILARITY

        session_similarity = min(1.0, 0.45 + content_similarity * 0.55)
        novelty = min(1.0, 0.25 + (rank_index / max(1, _DEFAULT_FETCH_LIMIT)))
        quality = self._extract_quality(record)
        diversity_penalty = self._diversity_penalty(guild_id, artist)

        # Use pre-fetched enrichment if provided, otherwise fetch individually
        enrichment: Optional[Dict[str, Any]] = pre_fetched_enrichment

        if enrichment is None:
            # Fall back to individual enrichment only if not pre-fetched
            # Try to get youtube_id and deezer_id from cache
            youtube_id = None
            deezer_id = None
            try:
                mapping_entry = await self._engine._cache.get_mapping(artist, title)
                if mapping_entry:
                    youtube_id = mapping_entry.youtube_id
                
                parsing_entry = await self._engine._cache.get_parsing(artist, title)
                if parsing_entry:
                    deezer_id = parsing_entry.deezer_id
            except Exception as e:
                LOG.debug(f"Unable to retrieve youtube_id/deezer_id for {artist} - {title}: {e}")
            
            async with self._enrich_semaphore:
                try:
                    enrichment = await self._engine.enrich_track(
                        artist, title, youtube_id=youtube_id, deezer_id=deezer_id
                    )
                except Exception as exc:  # pragma: no cover - defensive logging
                    LOG.debug("Enrichment failed for %s - %s: %s", artist, title, exc)

        mood_vector = None
        mood_label = None
        genres = None
        energy = None
        
        # 9D Dual-Vector Architecture (EfficientAT-computed)
        computed_loudness = None
        computed_tempo = None
        computed_key = None
        computed_mode = None

        if enrichment:
            mood_payload = enrichment.get("mood_vector") or {}
            if isinstance(mood_payload, dict):
                mood_vector = mood_payload.get("vector") or None
                mood_label = (
                    mood_payload.get("mood")
                    or enrichment.get("mood")
                    or enrichment.get("energy")
                )

            # Phase 3: Extract genres and energy for enhanced scoring
            tags = enrichment.get("tags") or []
            if tags and isinstance(tags, list):
                genres = [str(tag).lower() for tag in tags[:5]]  # Top 5 genres

            energy_val = enrichment.get("energy")
            if energy_val is not None:
                # Energy is string in cache ("high", "medium", "low")
                # Convert to numeric for scoring
                energy_map = {"low": 0.3, "medium": 0.6, "high": 0.9}
                if isinstance(energy_val, str):
                    energy = energy_map.get(energy_val.lower(), 0.6)
                elif isinstance(energy_val, (int, float)):
                    energy = float(energy_val)
            
            # Extract 9D dual-vector features from enrichment (EfficientAT-computed)
            computed_loudness = enrichment.get("computed_loudness")
            computed_tempo = enrichment.get("computed_tempo")
            computed_key = enrichment.get("computed_key")
            computed_mode = enrichment.get("computed_mode")

        features = CandidateFeatures(
            track_id=track_id,
            artist=artist,
            title=title,
            content_similarity=content_similarity,
            session_similarity=session_similarity,
            novelty=novelty,
            quality=quality,
            feedback_multiplier=1.0,
            diversity_penalty=diversity_penalty,
            extra_weight=1.0,
            mood_vector=mood_vector,
            mood_label=mood_label,
            genres=genres,
            energy=energy,
            # 9D Dual-Vector Architecture (Flow features)
            computed_loudness=computed_loudness,
            computed_tempo=computed_tempo,
            computed_key=computed_key,
            computed_mode=computed_mode,
        )

        metadata = {
            "artist": artist,
            "title": title,
            "source": record.get("_source"),
            "playcount": record.get("playcount"),
            "listeners": record.get("listeners"),
            "pool_source": record.get(
                "pool_source"
            ),  # Phase 4: For diversity injection
        }
        if self._engine._verbose >= 2:
            LOG.debug(
                "[AutoplayV2][candidate] %s -> sim=%.3f sess=%.3f nov=%.3f qual=%.3f div=%.3f mood=%s",
                track_id,
                content_similarity,
                session_similarity,
                novelty,
                quality,
                diversity_penalty,
                features.mood_label,
            )
        return PreparedCandidate(features=features, metadata=metadata)

    def _note_recommendation(self, guild_id: int, artist: str, title: str) -> None:
        normalized_artist = artist.lower().strip()
        track_id = self._track_id(artist, title)
        history = self._recent_history.setdefault(guild_id, [])
        history.append({"artist": normalized_artist, "track_id": track_id})
        if len(history) > self._history_limit:
            history.pop(0)

    def _diversity_penalty(self, guild_id: int, artist: str) -> float:
        history = self._recent_history.get(guild_id)
        if not history:
            return 1.0
        artist_key = artist.lower().strip()
        repeats = sum(1 for entry in history if entry.get("artist") == artist_key)
        return max(0.35, 1.0 / (1 + repeats))

    @staticmethod
    def _extract_artist(record: Dict[str, Any]) -> str:
        artist = record.get("artist")
        if isinstance(artist, dict):
            artist = artist.get("name")
        return str(artist or "").strip()

    @staticmethod
    def _extract_title(record: Dict[str, Any]) -> str:
        for key in ("name", "title", "track"):
            value = record.get(key)
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def _extract_match(record: Dict[str, Any]) -> float:
        raw = record.get("match")
        if raw is None:
            return 0.5
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _extract_quality(record: Dict[str, Any]) -> float:
        for key in ("playcount", "listeners"):
            raw = record.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            return max(0.3, min(1.0, 0.4 + min(value, 750000.0) / 1_500_000.0))
        return 0.5

    @staticmethod
    def _is_episode_content(title: str) -> bool:
        """Check if a title appears to be episode/series content rather than music."""
        import re as _re

        title_lower = title.lower()

        # Episode indicators - expanded patterns
        episode_patterns = [
            r"\bepisode\s+\d+\b",
            r"\bep\.?\s*\d+\b",
            r"\be\d+\b",
            r"\bs\d+e\d+\b",  # S01E01 format
            r"\bseason\s+\d+\b",
            r"\bchapter\s+\d+\b",
            r"\bpart\s+\d+\b",
            r"\bpt\.?\s*\d+\b",
            r"\bpilot\b",
            r"\bmidseason\b",
            r"\b\d+x\d+\b",  # 1x01 format
            r"\b#\d+\b",  # #1, #2, etc.
            r"\bvol\.?\s*\d+\b",  # Volume numbers (often series)
        ]

        for pattern in episode_patterns:
            if _re.search(pattern, title_lower):
                return True

        # Context-aware "finale" detection - only flag if combined with series indicators
        # This prevents false positives like "Sinner's Finale" or "Springtrap Finale"
        if "finale" in title_lower:
            series_context_keywords = [
                "episode",
                "season",
                "series",
                "chapter",
                "part",
                "animated",
                "animation",
                "show",
                "full episode",
            ]
            # Only flag as episode if "finale" appears with series context
            if any(keyword in title_lower for keyword in series_context_keywords):
                return True

        # TV/Series indicators (but allow official music videos)
        if (
            "official music video" not in title_lower
            and "music video" not in title_lower
        ):
            tv_patterns = [
                r"\btv\s+show\b",
                r"\btv\s+series\b",
                r"\bweb\s+series\b",
                r"\bmini\s+series\b",
                r"\bfull\s+episode\b",
                r"\bfull\s+movie\b",
            ]
            for pattern in tv_patterns:
                if _re.search(pattern, title_lower):
                    return True

        # Non-music content keywords (expanded)
        non_music_keywords = [
            "animation meme",  # Often fan content, not music
            "animatic",
            "comic dub",
            "fan dub",
            "audio drama",
            "audiobook",
            "podcast",
            "voice over",
            "voiceover",
            "gameplay",
            "walkthrough",
            "playthrough",
            "let's play",
            "gaming",
            "stream",
            "vlog",
            "behind the scenes",
            "making of",
            "documentary",
            "interview",
            "talk show",
            "news",
            "trailer",
            "teaser",
            "preview",
            "sneak peek",
        ]

        for keyword in non_music_keywords:
            if keyword in title_lower:
                return True

        return False

    # ------------------------------------------------------------------
    # Telemetry logging for v3 ML training data
    # ------------------------------------------------------------------
    def _log_telemetry(
        self,
        context: Dict[str, Any],
        candidate: Dict[str, Any],
        outcome: Dict[str, Any],
        mapping_quality: Dict[str, Any],
    ) -> None:
        """Log telemetry data for future ML training (Phase 5 Task 6.3)."""
        if not self._telemetry_enabled:
            return

        # Quality gating (Phase 5 Task 6.2)
        heuristic_score = mapping_quality.get("heuristic_score", 0.0)
        spam_flags = mapping_quality.get("spam_flags", [])

        if heuristic_score < _TELEMETRY_QUALITY_THRESHOLD or len(spam_flags) > 0:
            # Skip low-quality or spam tracks
            return

        telemetry_entry = {
            "context": context,
            "candidate": candidate,
            "outcome": outcome,
            "mapping_quality": mapping_quality,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        # Write to JSONL file
        telemetry_path = self._telemetry_dir / _TELEMETRY_FILE
        try:
            with open(telemetry_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(telemetry_entry) + "\n")

            # Phase 5 Task 6.4: Check for log rotation
            self._rotate_telemetry_if_needed(telemetry_path)

        except Exception as exc:
            LOG.debug(f"Failed to write telemetry: {exc}")

    def _rotate_telemetry_if_needed(self, telemetry_path: Path) -> None:
        """Rotate telemetry log if it exceeds size limit (Phase 5 Task 6.4)."""
        try:
            if not telemetry_path.exists():
                return

            file_size_mb = telemetry_path.stat().st_size / (1024 * 1024)

            if file_size_mb > _TELEMETRY_MAX_SIZE_MB:
                # Rotate: rename current file with timestamp
                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                rotated_name = f"v3_training_data_{timestamp}.jsonl"
                rotated_path = self._telemetry_dir / rotated_name

                telemetry_path.rename(rotated_path)
                LOG.info(
                    f"📊 [Telemetry] Rotated log to {rotated_name} ({file_size_mb:.1f} MB)"
                )

                # Clean up old files (keep last N)
                self._cleanup_old_telemetry_files()

        except Exception as exc:
            LOG.debug(f"Failed to rotate telemetry: {exc}")

    def _cleanup_old_telemetry_files(self) -> None:
        """Keep only the last N rotated telemetry files."""
        try:
            # Find all rotated files
            pattern = "v3_training_data_*.jsonl"
            rotated_files = sorted(
                self._telemetry_dir.glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

            # Delete files beyond the limit
            for old_file in rotated_files[_TELEMETRY_MAX_FILES:]:
                old_file.unlink()
                LOG.debug(f"📊 [Telemetry] Deleted old log: {old_file.name}")

        except Exception as exc:
            LOG.debug(f"Failed to cleanup telemetry files: {exc}")

    @staticmethod
    def _track_id(artist: str, title: str) -> str:
        return f"{artist.strip().lower()}::{title.strip().lower()}"


_autoplay_instance: Optional[LastFMAutoplayV3] = None


def get_lastfm_autoplay_v3(bot: Any) -> LastFMAutoplayV3:
    global _autoplay_instance
    if _autoplay_instance is None:
        _autoplay_instance = LastFMAutoplayV3(bot)
    return _autoplay_instance


__all__ = ["LastFMAutoplayV3", "get_lastfm_autoplay_v3"]
