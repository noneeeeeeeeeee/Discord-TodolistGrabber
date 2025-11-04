"""Autoplay Engine V2 entry point.

This module exposes ``LastFMAutoplayV2`` which mirrors the public surface of the
legacy autoplay implementation so the existing ``MusicPlayer`` integration can
switch between engines via ``Autoplay_Engine.config`` without additional glue
code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
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
    ) -> None:
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

        candidate_records = await self._fetch_candidate_records(seed_artist, seed_title)
        if not candidate_records:
            return []

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
        scored = self._engine.score_candidates(
            guild_id,
            features,
            seed_track_ids=[seed_track_id],
            session_mood_vector=session_vector,
            target_mood=target_mood,
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

            # Apply genre/tag penalties for recently skipped content
            if context.disliked_tags:
                meta = metadata_index.get(candidate.track_id, {})
                candidate_tags = meta.get("tags", [])

                # Calculate tag penalty (average of all matching disliked tags)
                tag_penalties = [
                    context.disliked_tags.get(tag.lower(), 0.0)
                    for tag in candidate_tags
                    if tag.lower() in context.disliked_tags
                ]

                if tag_penalties:
                    avg_penalty = sum(tag_penalties) / len(tag_penalties)
                    tag_multiplier = 1.0 - avg_penalty  # Convert penalty to multiplier
                    candidate.score *= tag_multiplier

                    if self._verbose >= 2 and avg_penalty > 0.1:
                        LOG.debug(
                            "👎 [Skip Penalty] Reducing score by %.1f%% for '%s' (disliked tags: %s)",
                            avg_penalty * 100,
                            candidate.title,
                            [
                                t
                                for t in candidate_tags
                                if t.lower() in context.disliked_tags
                            ][:3],
                        )

        # Re-sort after applying all penalties
        scored.sort(key=lambda c: c.score, reverse=True)

        if self._engine._verbose and scored:
            top = scored[0]
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

        results: List[Tuple[str, Any]] = []
        for candidate in scored:
            meta = metadata_index.get(candidate.track_id)
            if not meta:
                continue
            track_obj = await self._engine.resolve_track(
                meta["artist"],
                meta["title"],
                expected_duration_ms=expected_duration_ms,
            )
            if not track_obj:
                continue
            self._note_recommendation(guild_id, meta["artist"], meta["title"])
            results.append((candidate.track_id, track_obj))
            if len(results) >= max(1, limit):
                break

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
        artist: str,
        title: str,
    ) -> List[Dict[str, Any]]:
        if not self._lastfm_key:
            LOG.warning("Last.fm API key missing; cannot fetch candidates")
            return []

        params = {
            "method": "track.getSimilar",
            "artist": artist,
            "track": title,
            "limit": str(_DEFAULT_FETCH_LIMIT),
            "autocorrect": "1",
            "api_key": self._lastfm_key,
            "format": "json",
        }
        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][lastfm] track.getSimilar artist=%s title=%s limit=%s",
                artist,
                title,
                params["limit"],
            )
        records = await self._call_lastfm(params, source="track.getSimilar")
        if records:
            if self._engine._verbose:
                LOG.debug(
                    "[AutoplayV2][lastfm] similar returned %d records (samples=%s)",
                    len(records),
                    [
                        f"{self._extract_artist(r)}::{self._extract_title(r)}"
                        for r in records[:5]
                    ],
                )
            return records

        # Fallback 1: Get top tracks from the same artist
        fallback_params = {
            "method": "artist.getTopTracks",
            "artist": artist,
            "limit": str(_DEFAULT_FETCH_LIMIT // 2),
            "api_key": self._lastfm_key,
            "format": "json",
            "autocorrect": "1",
        }
        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][lastfm] fallback artist.getTopTracks artist=%s limit=%s",
                artist,
                fallback_params["limit"],
            )
        recs = await self._call_lastfm(fallback_params, source="artist.getTopTracks")

        # If top tracks returned results, use them
        if recs:
            if self._engine._verbose:
                LOG.debug(
                    "[AutoplayV2][lastfm] toptracks returned %d records (samples=%s)",
                    len(recs),
                    [
                        f"{self._extract_artist(r)}::{self._extract_title(r)}"
                        for r in recs[:5]
                    ],
                )
            return recs

        # Fallback 2: Get similar artists and fetch their top tracks for diversity
        similar_artist_params = {
            "method": "artist.getSimilar",
            "artist": artist,
            "limit": "10",  # Get 10 similar artists
            "api_key": self._lastfm_key,
            "format": "json",
            "autocorrect": "1",
        }
        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][lastfm] fallback2 artist.getSimilar artist=%s limit=10",
                artist,
            )

        similar_artists = await self._call_lastfm(
            similar_artist_params, source="artist.getSimilar"
        )

        if similar_artists:
            # Get top tracks from the first 3 similar artists
            all_tracks = []
            for similar_artist_record in similar_artists[:3]:
                similar_artist_name = self._extract_artist(similar_artist_record)
                if not similar_artist_name:
                    continue

                similar_tracks_params = {
                    "method": "artist.getTopTracks",
                    "artist": similar_artist_name,
                    "limit": str(_DEFAULT_FETCH_LIMIT // 6),  # ~16 tracks per artist
                    "api_key": self._lastfm_key,
                    "format": "json",
                    "autocorrect": "1",
                }

                similar_tracks = await self._call_lastfm(
                    similar_tracks_params, source="artist.getTopTracks"
                )
                all_tracks.extend(similar_tracks)

                if len(all_tracks) >= _DEFAULT_FETCH_LIMIT // 2:
                    break

            if all_tracks:
                if self._engine._verbose:
                    LOG.debug(
                        "[AutoplayV2][lastfm] similar artists fallback returned %d tracks from related artists",
                        len(all_tracks),
                    )
                return all_tracks[: _DEFAULT_FETCH_LIMIT // 2]

        # Last resort: return empty to trigger other mechanisms
        LOG.warning(
            "[AutoplayV2][lastfm] All Last.fm methods failed for artist=%s title=%s",
            artist,
            title,
        )
        return []

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
                        LOG.debug("Last.fm %s failed (%s)", source, response.status)
                        return []
                    payload = await response.json(content_type=None)
        except Exception as exc:
            LOG.warning("Last.fm %s request failed: %s", source, exc)
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
        return trimmed

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

        # Second pass: check cache and enqueue enrichment requests for missing ones
        enrichment_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        pending_enrichments: Dict[str, asyncio.Future] = {}
        tracks_needing_enrichment: List[Tuple[str, str, str]] = (
            []
        )  # track_id, artist, title

        for _, track_id, artist, title, _ in candidates_to_prepare:
            # Check if already enriched in cache
            cached = await self._engine._cache.get_enrichment(artist, title)
            if cached:
                # Build enrichment dict from cache
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
                tracks_needing_enrichment.append((track_id, artist, title))

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

            # Process completed enrichments (mood vector now included in enrichment response)
            for track_id, future in pending_enrichments.items():
                try:
                    if future.done() and not future.cancelled():
                        result = future.result()
                        if result:
                            artist, title = None, None
                            for _, tid, a, t, _ in candidates_to_prepare:
                                if tid == track_id:
                                    artist, title = a, t
                                    break

                            if artist and title:
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
                                mood_vector = result.get(
                                    "mood_vector"
                                )  # Already included from enrich_track()

                                enrichment_cache[track_id] = {
                                    "tags": tags,
                                    "mood": mood_value,
                                    "energy": energy,
                                    "mood_vector": mood_vector,
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
        for index, track_id, artist, title, record in candidates_to_prepare:
            enrichment = enrichment_cache.get(track_id)
            tasks.append(
                asyncio.create_task(
                    self._prepare_single_candidate(
                        guild_id, track_id, artist, title, record, index, enrichment
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
        if self._engine._verbose:
            LOG.debug(
                "[AutoplayV2][prepare] kept %d/%d candidates",
                len(prepared),
                len(records),
            )
        return prepared

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
        if enrichment:
            mood_payload = enrichment.get("mood_vector") or {}
            if isinstance(mood_payload, dict):
                mood_vector = mood_payload.get("vector") or None
                mood_label = (
                    mood_payload.get("mood")
                    or enrichment.get("mood")
                    or enrichment.get("energy")
                )

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
        )

        metadata = {
            "artist": artist,
            "title": title,
            "source": record.get("_source"),
            "playcount": record.get("playcount"),
            "listeners": record.get("listeners"),
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
