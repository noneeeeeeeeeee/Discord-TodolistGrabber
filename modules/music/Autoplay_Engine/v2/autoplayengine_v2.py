import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from modules.enviromentfilegenerator import check_and_load_env_file
from .cache_manager import CacheManager, EnrichmentEntry, MoodVectorEntry, ParsingEntry
from .collaborative_matrix import CollaborativeMatrix
from .feedback_manager import FeedbackManager
from .gemini_service import GeminiService
from .contextual_recommender import (
    CandidateFeatures,
    ContextualRecommender,
    ScoredCandidate,
)
from .track_resolver import TrackResolver

LOG = logging.getLogger(__name__)
check_and_load_env_file()

@dataclass
class AvailabilityReport:
    lastfm: bool
    gemini: bool
    status: str


class AutoplayEngineV2:
    """Primary coordination class for Autoplay V2 services."""

    def __init__(
        self,
        *,
        cache_dir: Path | str = Path("cache/music"),
        gemini_service: Optional[GeminiService] = None,
        feedback_manager: Optional[FeedbackManager] = None,
        collaborative_matrix: Optional[CollaborativeMatrix] = None,
        track_resolver: Optional[TrackResolver] = None,
    ) -> None:
        cache_path = Path(cache_dir)
        self._cache = CacheManager(cache_path)
        self._gemini = gemini_service or GeminiService(cache_dir=cache_path)
        self._feedback = feedback_manager or FeedbackManager(cache_dir=cache_path)
        self._collaborative = collaborative_matrix or CollaborativeMatrix(self._cache)
        self._recommender = ContextualRecommender(self._collaborative)
        self._track_resolver = track_resolver or TrackResolver(self._cache)
        self._lastfm_key = os.getenv(LASTFM_API_KEY_ENV, "").strip()

        if not self._lastfm_key:
            LOG.warning(
                "Last.fm API key missing. Populate LASTFM_API_KEY in .env (see modules/enviromentfilegenerator.py)."
            )
        if not self._gemini.is_available:
            LOG.warning("Gemini unavailable during AutoplayEngineV2 init: %s", self._gemini.status)

    @property
    def is_available(self) -> bool:
        return bool(self._lastfm_key) and self._gemini.is_available

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

    async def parse_track(self, raw_title: str, channel_name: str) -> Optional[Dict[str, Any]]:
        cached = await self._cache.get_parsing(raw_title, channel_name)
        if cached:
            return {"artist": cached.artist, "title": cached.title, "confidence": cached.confidence}

        parsed = await self._try_gemini_parse(raw_title, channel_name)
        if not parsed:
            LOG.warning(
                "Gemini unable to parse track metadata (title=%r, channel=%r)",
                raw_title,
                channel_name,
            )
            return None

        entry = ParsingEntry(
            artist=parsed["artist"],
            title=parsed["title"],
            confidence=float(parsed.get("confidence", 1.0)),
            parsed_at=time.time(),
        )
        await self._cache.set_parsing(raw_title, channel_name, entry)
        return parsed

    async def enrich_track(
        self,
        artist: str,
        title: str,
        existing_tags: Optional[List[str]] = None,
        *,
        require_grounding: bool = False,
    ) -> Optional[Dict[str, Any]]:
        cached = await self._cache.get_enrichment(artist, title)
        if cached:
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "Enrichment cache hit: %s",
                    {
                        "artist": artist,
                        "title": title,
                        "tags": cached.tags[:5],
                        "mood": cached.mood,
                        "energy": cached.energy,
                        "mood_vector_id": cached.mood_vector_id,
                    },
                )
            return {
                "tags": cached.tags,
                "mood": cached.mood,
                "energy": cached.energy,
                "listeners": cached.listeners,
                "playcount": cached.playcount,
                "duration_ms": cached.duration_ms,
                "mood_vector": await self._cached_mood_vector_dict(cached.mood_vector_id),
            }

        if not self._gemini.is_available:
            LOG.debug("Gemini unavailable; skipping enrichment")
            return None

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

        tags = [str(tag).lower() for tag in response.get("tags", []) if isinstance(tag, str)]
        moods = [str(mood).strip() for mood in response.get("moods", []) if isinstance(mood, str) and mood.strip()]
        mood_value = moods[0] if moods else None
        energy = response.get("energy") if isinstance(response.get("energy"), str) else None

        mood_vector = await self.get_mood_vector(
            artist,
            title,
            tags=tags,
            genre=str(response.get("genre", "") or ""),
            description=str(response.get("description", "") or ""),
        )

        entry = EnrichmentEntry(
            tags=tags,
            mood=mood_value,
            listeners=0,
            playcount=0,
            duration_ms=None,
            fetched_at=time.time(),
            mood_vector_id=mood_vector.get("id") if mood_vector else None,
            energy=energy,
        )
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
                    "mood_vector_id": entry.mood_vector_id,
                },
            )
        return {
            "tags": tags,
            "mood": mood_value,
            "energy": energy,
            "listeners": 0,
            "playcount": 0,
            "duration_ms": None,
            "mood_vector": mood_vector,
        }

    async def hydrate_collaborative(self, source: Optional[Path | str] = None) -> bool:
        matrix = self._collaborative
        if source is not None:
            return await matrix.load_from_file(source, persist=True)
        return await matrix.hydrate()

    def score_candidates(
        self,
        guild_id: int | str,
        candidates: Sequence[CandidateFeatures],
        *,
        seed_track_ids: Optional[Iterable[str]] = None,
        session_mood_vector: Optional[Sequence[float]] = None,
        target_mood: Optional[str] = None,
    ) -> List[ScoredCandidate]:
        return self._recommender.score_candidates(
            guild_id,
            candidates,
            seed_track_ids=seed_track_ids,
            session_mood_vector=session_mood_vector,
            target_mood=target_mood,
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

    async def _try_gemini_parse(
        self,
        raw_title: str,
        channel_name: str,
    ) -> Optional[Dict[str, Any]]:
        if not self._gemini.is_available:
            LOG.debug("Gemini unavailable; skipping AI parse")
            return None

        try:
            response = await self._gemini.parse_track_metadata(raw_title, channel_name)
        except Exception as exc:
            LOG.warning("Gemini parse failed: %s", exc)
            return None

        if not response:
            return None

        artist = str(response.get("artist", "")).strip()
        title = str(response.get("title", "")).strip()
        if artist and title:
            payload: Dict[str, Any] = {"artist": artist, "title": title, "confidence": 0.92}
            return payload
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
        cached_entry: Optional[MoodVectorEntry] = None
        if not force_refresh:
            cached_entry = await self._cache.get_mood_vector(key)
            if cached_entry:
                return self._mood_entry_to_dict(key, cached_entry)

        if not self._gemini.is_available:
            LOG.debug("Gemini unavailable; skipping mood vector classification")
            return self._mood_entry_to_dict(key, cached_entry) if cached_entry else None

        metadata = {
            "tags": list(tags or []),
            "genre": genre or "",
            "description": description or "",
        }

        try:
            response = await self._gemini.classify_mood_vector(metadata)
        except Exception as exc:
            LOG.warning("Gemini mood classification failed: %s", exc)
            return self._mood_entry_to_dict(key, cached_entry) if cached_entry else None

        if not response:
            return self._mood_entry_to_dict(key, cached_entry) if cached_entry else None

        entry = MoodVectorEntry(
            energy=float(response.get("energy", 0.0) or 0.0),
            valence=float(response.get("valence", 0.0) or 0.0),
            tempo=float(response.get("tempo", 0.0) or 0.0),
            confidence=float(response.get("confidence", 0.0) or 0.0),
            mood=(str(response.get("mood", "")).strip() or None),
            fetched_at=time.time(),
        )
        await self._cache.set_mood_vector(key, entry)
        return self._mood_entry_to_dict(key, entry)

    async def _cached_mood_vector_dict(self, key_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key_id:
            return None
        entry = await self._cache.get_mood_vector(key_id)
        if not entry:
            return None
        return self._mood_entry_to_dict(key_id, entry)

    @staticmethod
    def _make_track_key(artist: str, title: str) -> str:
        return f"{artist.lower().strip()}::{title.lower().strip()}"

    @staticmethod
    def _mood_entry_to_dict(key: str, entry: MoodVectorEntry) -> Dict[str, Any]:
        vector = [entry.energy, entry.valence, entry.tempo]
        return {
            "id": key,
            "mood": entry.mood,
            "energy": entry.energy,
            "valence": entry.valence,
            "tempo": entry.tempo,
            "confidence": entry.confidence,
            "vector": vector,
        }
