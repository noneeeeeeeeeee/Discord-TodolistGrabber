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
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import aiohttp

from .autoplayengine_v2 import AutoplayEngineV2, LASTFM_API_KEY_ENV
from .contextual_recommender import CandidateFeatures

LOG = logging.getLogger(__name__)

_LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
_DEFAULT_FETCH_LIMIT = 40
_HISTORY_LIMIT = 50
_PARALLEL_ENRICH_LIMIT = 5
_MIN_CONTENT_SIMILARITY = 0.05


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

    # ------------------------------------------------------------------
    # Public API expected by ``MusicPlayer``
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        return self._engine.is_available

    def clear_history(self, guild_id: Optional[int] = None) -> None:
        if guild_id is None:
            self._recent_history.clear()
        else:
            self._recent_history.pop(int(guild_id), None)

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

        await self._engine.record_feedback_event(
            guild_id=guild_id,
            user_id=user_id,
            track_id=track_id,
            event_type=event_type,
            metadata={key: value for key, value in metadata.items() if value is not None},
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
        seed_artist = parsed["artist"] if parsed else channel_name
        seed_title = parsed["title"] if parsed else raw_title

        if not seed_artist or not seed_title:
            LOG.debug("Unable to resolve seed metadata; aborting autoplay round")
            return []

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
        metadata_index = {entry.features.track_id: entry.metadata for entry in prepared_candidates}
        scored = self._engine.score_candidates(
            guild_id,
            features,
            seed_track_ids=[seed_track_id],
            session_mood_vector=session_vector,
            target_mood=target_mood,
        )

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
        records = await self._call_lastfm(params, source="track.getSimilar")
        if records:
            return records

        fallback_params = {
            "method": "artist.getTopTracks",
            "artist": artist,
            "limit": str(_DEFAULT_FETCH_LIMIT // 2),
            "api_key": self._lastfm_key,
            "format": "json",
            "autocorrect": "1",
        }
        return await self._call_lastfm(fallback_params, source="artist.getTopTracks")

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
        return trimmed

    async def _prepare_candidates(
        self,
        guild_id: int,
        records: Sequence[Dict[str, Any]],
    ) -> List[PreparedCandidate]:
        seen: set[str] = set()
        tasks: List[asyncio.Task[Optional[PreparedCandidate]]] = []
        for index, record in enumerate(records):
            artist = self._extract_artist(record)
            title = self._extract_title(record)
            if not artist or not title:
                continue
            track_id = self._track_id(artist, title)
            if track_id in seen:
                continue
            seen.add(track_id)
            tasks.append(
                asyncio.create_task(
                    self._prepare_single_candidate(guild_id, track_id, artist, title, record, index)
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
        return prepared

    async def _prepare_single_candidate(
        self,
        guild_id: int,
        track_id: str,
        artist: str,
        title: str,
        record: Dict[str, Any],
        rank_index: int,
    ) -> Optional[PreparedCandidate]:
        content_similarity = self._extract_match(record)
        if content_similarity < _MIN_CONTENT_SIMILARITY:
            content_similarity = _MIN_CONTENT_SIMILARITY

        session_similarity = min(1.0, 0.45 + content_similarity * 0.55)
        novelty = min(1.0, 0.25 + (rank_index / max(1, _DEFAULT_FETCH_LIMIT)))
        quality = self._extract_quality(record)
        diversity_penalty = self._diversity_penalty(guild_id, artist)

        enrichment: Optional[Dict[str, Any]] = None
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
    def _track_id(artist: str, title: str) -> str:
        return f"{artist.strip().lower()}::{title.strip().lower()}"


_autoplay_instance: Optional[LastFMAutoplayV2] = None


def get_lastfm_autoplay_v2(bot: Any) -> LastFMAutoplayV2:
    global _autoplay_instance
    if _autoplay_instance is None:
        _autoplay_instance = LastFMAutoplayV2(bot)
    return _autoplay_instance


__all__ = ["LastFMAutoplayV2", "get_lastfm_autoplay_v2"]
