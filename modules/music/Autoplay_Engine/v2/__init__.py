"""Autoplay Engine V2 entry point.

This module exposes ``LastFMAutoplayV2`` which mirrors the public surface of the
legacy autoplay implementation so the existing ``MusicPlayer`` integration can
switch between engines via ``Autoplay_Engine.config`` without additional glue
code.
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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import aiohttp

from .autoplayengine_v2 import AutoplayEngineV2, LASTFM_API_KEY_ENV
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

# Phase 5: Telemetry configuration
_TELEMETRY_DIR = Path("cache/music/telemetry")
_TELEMETRY_FILE = "v3_training_data.jsonl"
_TELEMETRY_MAX_SIZE_MB = 50
_TELEMETRY_MAX_FILES = 5
_TELEMETRY_QUALITY_THRESHOLD = 8.0


@dataclass(slots=True)
class PreparedCandidate:
    features: CandidateFeatures
    metadata: Dict[str, Any]


class LastFMAutoplayV2:
    """High-level orchestrator that feeds ``AutoplayEngineV2``."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._engine = AutoplayEngineV2()
        self._lastfm_key = os.getenv(LASTFM_API_KEY_ENV, "").strip()
        self._recent_history: Dict[int, List[Dict[str, str]]] = defaultdict(list)
        self._history_limit = _HISTORY_LIMIT
        self._collaborative_ready = False
        self._collaborative_lock = asyncio.Lock()
        self._enrich_semaphore = asyncio.Semaphore(_PARALLEL_ENRICH_LIMIT)
        self._http_timeout = aiohttp.ClientTimeout(total=12)

        # Issue #3 - Contextual Arc Recommender components
        verbosity = int(os.getenv("AUTOPLAY_V2_VERBOSITY", "0"))
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
        if not self.is_available():
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

        # More/Less Like This are NOT skips - they're explicit user preference signals
        # They get recorded separately with their own weighting based on VC participant count

        # Try to get enrichment data from cache for accurate genre/mood tracking
        enrichment = await self._engine.enrich_track(artist, title)
        genres = enrichment.get("tags", [])[:5] if enrichment else []
        mood_vector = enrichment.get("mood_vector") if enrichment else None
        mood_label = enrichment.get("mood") if enrichment else None

        # Phase 0.5: Try to get track_type and primary_entity from parsing cache
        # We need to match the track by artist/title from cache
        track_type = "music"  # Default fallback
        primary_entity = None

        # Attempt to retrieve from cache by checking the most recent parsing entries
        # (This is a best-effort approach since we don't have raw_title/channel_name here)
        # In future, we should pass these through the call chain
        try:
            # Check if we have this track in cache
            # For now, we'll default to "music" type unless we find evidence otherwise
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

        # Estimate quality (we don't have full mapping quality here, use heuristics)
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

    async def get_recommendations_for_track(
        self,
        track_info: Dict[str, Any],
        limit: int = 10,
    ) -> List[Tuple[str, Any]]:
        if not self.is_available():
            LOG.debug("Autoplay V2 unavailable; skipping recommendation lookup")
            return []

        raw_title = str(track_info.get("title", "")).strip()
        channel_name = str(track_info.get("author", "")).strip()
        expected_duration_ms = track_info.get("length")
        guild_id = int(track_info.get("guild_id", 0) or 0)

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

        # Phase 2: Get seed track metadata for OST branching
        seed_track_type = parsed.get("track_type", "music") if parsed else "music"
        seed_entity = parsed.get("primary_entity") if parsed else None

        candidate_records = await self._fetch_candidate_records(
            guild_id=guild_id,
            seed_artist=seed_artist,
            seed_title=seed_title,
            seed_track_type=seed_track_type,
            seed_entity=seed_entity,
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

        if self._engine._verbose and selection_pool:
            top = selection_pool[0]
            top_meta = metadata_index.get(top.track_id, {})
            # Try to find mood label from the prepared candidate features
            top_feat = next(
                (
                    e.features
                    for e in prepared_candidates
                    if e.features.track_id == top.track_id
                ),
                None,
            )
            mood_desc = (
                top_feat.mood_label if top_feat and top_feat.mood_label else target_mood
            ) or "unknown"

            # Issue #3: Show exploration phase and reasoning
            phase = self._novelty_controller.detect_exploration_phase(
                skip_rate=context.skip_rate,
                songs_since_novelty=context.songs_since_novelty,
                session_duration_minutes=(context.last_activity - context.session_start)
                / 60,
            )

            # Check if this is a novelty pick or core pick
            artist_plays = tracker.get_artist_play_count(top_meta.get("artist", ""))
            is_new_artist = artist_plays == 0
            exploration_marker = "🔍 NEW" if is_new_artist else "✨ FAMILIAR"

            LOG.info(
                "🎯 [Next Pick] %s: '%s' by '%s' (score=%.3f, mood=%s, phase=%s) after '%s'",
                exploration_marker,
                top_meta.get("title", "Unknown"),
                top_meta.get("artist", "Unknown"),
                top.score,
                mood_desc,
                phase.value,
                f"{seed_artist} - {seed_title}",
            )

            # Increment novelty counter if this is exploration
            if is_new_artist:
                tracker.reset_novelty_counter()
            else:
                tracker.increment_novelty_counter()

        # Phase 4: Stochastic selection with safe gate
        results: List[Tuple[str, Any]] = []
        max_retries = 3
        retry_count = 0

        while (
            len(results) < max(1, limit)
            and selection_pool
            and retry_count <= max_retries
        ):
            # Phase 4 Task 5.3: Stochastic selection from top 5
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

            # Phase 4 Task 5.4: Safe gate - check quality threshold
            meta = metadata_index.get(selected_candidate.track_id)
            if not meta:
                # Remove from pool and retry
                selection_pool = [
                    c
                    for c in selection_pool
                    if c.track_id != selected_candidate.track_id
                ]
                retry_count += 1
                continue

            # Resolve track
            track_obj = await self._engine.resolve_track(
                meta["artist"],
                meta["title"],
                expected_duration_ms=expected_duration_ms,
            )

            if not track_obj:
                # Track resolution failed, remove and retry
                selection_pool = [
                    c
                    for c in selection_pool
                    if c.track_id != selected_candidate.track_id
                ]
                retry_count += 1
                if self._verbose:
                    LOG.debug(
                        "❌ [Safe Gate] Track resolution failed for '%s', retrying (%d/%d)",
                        selected_candidate.title,
                        retry_count,
                        max_retries,
                    )
                continue

            # Safe gate quality check
            quality_threshold = (
                8.0 if retry_count == 0 else 7.0
            )  # Lower threshold on retries
            if (
                selected_candidate.score < quality_threshold
                and retry_count < max_retries
            ):
                # Score too low, remove and retry
                selection_pool = [
                    c
                    for c in selection_pool
                    if c.track_id != selected_candidate.track_id
                ]
                retry_count += 1
                if self._verbose:
                    LOG.info(
                        "⚠️ [Safe Gate] Score %.3f below threshold %.1f for '%s', retrying (%d/%d)",
                        selected_candidate.score,
                        quality_threshold,
                        selected_candidate.title,
                        retry_count,
                        max_retries,
                    )
                continue

            # Track passed safe gate
            self._note_recommendation(guild_id, meta["artist"], meta["title"])
            results.append((selected_candidate.track_id, track_obj))

            # Phase 4 Task 5.1/5.2: Update diversity injection counter
            if selected_candidate.score >= 8.0:
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
            retry_count = 0  # Reset retry count for next track

        if not results:
            LOG.debug("Autoplay V2 produced no playable tracks after resolution")
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _ensure_collaborative_ready(self) -> None:
        if self._collaborative_ready:
            return
        async with self._collaborative_lock:
            if not self._collaborative_ready:
                self._collaborative_ready = await self._engine.hydrate_collaborative()

    async def _fetch_candidate_records(
        self,
        *,
        guild_id: int,
        seed_artist: str,
        seed_title: str,
        seed_track_type: str = "music",
        seed_entity: Optional[str] = None,
        context: Any,  # SessionContext from context_tracker
        tracker: Any,  # ContextTracker instance
    ) -> List[Dict[str, Any]]:
        """
        Fetch candidate pool using Phase 2 diversification strategy.

        Branch A (OST/Entity-based): For OST/anime/game soundtracks with entity
        Branch B (Artist-based): For standard music
            - Cold Start: < 3 songs history
            - Warm Start: >= 3 songs history

        Args:
            guild_id: Discord guild ID
            seed_artist: Seed track artist
            seed_title: Seed track title
            seed_track_type: Track type from parsing (ost, anime_opening, game_soundtrack, music)
            seed_entity: Primary entity for OST content (e.g., "Hazbin Hotel")
            context: SessionContext with history and focus_genres
            tracker: ContextTracker instance for accessing _history

        Returns:
            List of track records with _source and pool_source tags
        """
        if not self._lastfm_key:
            LOG.warning("Last.fm API key missing; cannot fetch candidates")
            return []

        # Task 3.1: OST branch detection
        is_ost = seed_track_type in {"ost", "game_soundtrack", "anime_opening"}
        use_entity_branch = is_ost and seed_entity is not None

        history = self._recent_history.get(guild_id, [])
        history_size = len(history)

        if use_entity_branch:
            # Branch A: Entity-based fetch for OST content
            return await self._fetch_entity_based_pools(
                seed_artist=seed_artist,
                seed_title=seed_title,
                seed_entity=seed_entity,
                context=context,
                tracker=tracker,
            )
        elif history_size < 3:
            # Branch B: Cold start
            return await self._fetch_cold_start_pools(
                seed_artist=seed_artist,
                seed_title=seed_title,
                context=context,
            )
        else:
            # Branch B: Warm start
            return await self._fetch_warm_start_pools(
                seed_artist=seed_artist,
                seed_title=seed_title,
                context=context,
                tracker=tracker,
            )

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
        Task 3.2: Fetch Branch A (entity-based) pools for OST content.

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

        # Pool C: Safe harbor from top focus genre
        pool_c_task = asyncio.sleep(0, result=[])
        if context.focus_genres:
            top_genre = context.focus_genres[0]
            pool_c_task = self._fetch_tag_top_tracks(top_genre, limit=30)

        pool_a, pool_c = await asyncio.gather(pool_a_task, pool_c_task)

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
        Task 3.6: Enforce artist diversity by limiting tracks per artist.

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
    # Last.fm API Helper Methods (Phase 2: Task 3.7)
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
        )  # track_id, artist, title

        # Store clean metadata for later use
        parsed_metadata: Dict[str, Tuple[str, str]] = (
            {}
        )  # track_id -> (clean_artist, clean_title)

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
                enrichment_cache[track_id] = {
                    "tags": cached.tags,
                    "mood": cached.mood,
                    "energy": cached.energy,
                    "mood_vector": await self._engine._cached_mood_vector_dict(
                        cached.mood_vector_id
                    ),
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
                    # Enqueue enrichment (returns future, doesn't start task yet)
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
                                    enrichment_cache[track_id] = {
                                        "tags": cached_entry.tags,
                                        "mood": cached_entry.mood,
                                        "energy": cached_entry.energy,
                                        "mood_vector": await self._engine._cached_mood_vector_dict(
                                            cached_entry.mood_vector_id
                                        ),
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
            # Use clean keys from parsed metadata
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
            except Exception as exc:  # pragma: no cover - defensive
                LOG.debug("Candidate preparation failed: %s", exc)
                continue
            if result is not None:
                prepared.append(result)

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
                # Build enrichment entry (same logic as enrich_track)
                tags = [
                    str(tag).lower()
                    for tag in payload.get("tags", [])
                    if isinstance(tag, str)
                ]
                moods = [
                    str(m).strip()
                    for m in payload.get("moods", [])
                    if isinstance(m, str) and m.strip()
                ]
                mood_value = moods[0] if moods else None
                energy = (
                    payload.get("energy")
                    if isinstance(payload.get("energy"), str)
                    else None
                )

                # Extract mood vector if present
                mood_vector_id = None
                mood_vector_data = payload.get("mood_vector")
                if mood_vector_data and isinstance(mood_vector_data, dict):
                    try:
                        from .cache_manager import MoodVectorEntry

                        mood_vector_entry = MoodVectorEntry(
                            energy=float(mood_vector_data.get("energy", 0.0) or 0.0),
                            valence=float(mood_vector_data.get("valence", 0.0) or 0.0),
                            tempo=float(mood_vector_data.get("tempo", 0.0) or 0.0),
                            confidence=float(
                                mood_vector_data.get("confidence", 0.0) or 0.0
                            ),
                            mood=(
                                str(mood_vector_data.get("mood", "")).strip() or None
                            ),
                            fetched_at=time.time(),
                        )
                        await self._engine._cache.set_mood_vector(
                            normalized_key, mood_vector_entry
                        )
                        mood_vector_id = normalized_key
                    except Exception as exc:
                        LOG.debug(
                            f"Failed to parse mood_vector for {normalized_key}: {exc}"
                        )

                # Extract extended metadata
                bpm_val = payload.get("bpm")
                key_val = payload.get("key")
                activity_val = payload.get("activity_affinity")
                intensity_val = payload.get("emotional_intensity")
                daypart_val = payload.get("daypart_affinity")

                from .cache_manager import EnrichmentEntry

                enrichment_entry = EnrichmentEntry(
                    tags=tags,
                    mood=mood_value,
                    listeners=0,  # TODO: Fetch from Last.fm
                    playcount=0,  # TODO: Fetch from Last.fm
                    duration_ms=None,  # TODO: Fetch from Last.fm
                    fetched_at=time.time(),
                    mood_vector_id=mood_vector_id,
                    energy=energy,
                    bpm=int(bpm_val) if bpm_val and str(bpm_val).isdigit() else None,
                    key=str(key_val).strip() if key_val else None,
                    activity_affinity=(
                        str(activity_val).strip() if activity_val else None
                    ),
                    emotional_intensity=(
                        float(intensity_val) if intensity_val is not None else None
                    ),
                    daypart_affinity=(
                        str(daypart_val).strip() if daypart_val else None
                    ),
                )

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
            async with self._enrich_semaphore:
                try:
                    enrichment = await self._engine.enrich_track(artist, title)
                except Exception as exc:  # pragma: no cover - defensive logging
                    LOG.debug("Enrichment failed for %s - %s: %s", artist, title, exc)

        mood_vector = None
        mood_label = None
        genres = None
        energy = None

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
            if re.search(pattern, title_lower):
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
                if re.search(pattern, title_lower):
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
    # Phase 5: Telemetry logging for v3 ML training data
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


_autoplay_instance: Optional[LastFMAutoplayV2] = None


def get_lastfm_autoplay_v2(bot: Any) -> LastFMAutoplayV2:
    global _autoplay_instance
    if _autoplay_instance is None:
        _autoplay_instance = LastFMAutoplayV2(bot)
    return _autoplay_instance


__all__ = ["LastFMAutoplayV2", "get_lastfm_autoplay_v2"]
