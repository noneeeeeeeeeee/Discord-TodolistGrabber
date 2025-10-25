import asyncio
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "MappingEntry",
    "EnrichmentEntry",
    "ParsingEntry",
    "MoodVectorEntry",
    "CollaborativeSnapshot",
    "CacheManager",
]

_SECONDS_PER_DAY = 86400


@dataclass
class MappingEntry:
    youtube_id: str
    url: str
    timestamp: float
    channel_name: Optional[str] = None
    verified: bool = False
    duration_ms: Optional[int] = None
    track_identifier: Optional[str] = None

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.timestamp) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "MappingEntry":
        return cls(
            youtube_id=str(payload.get("youtube_id", "")),
            url=str(payload.get("url", "")),
            timestamp=float(payload.get("timestamp", 0.0)),
            channel_name=payload.get("channel_name"),
            verified=bool(payload.get("verified", False)),
            duration_ms=payload.get("duration_ms"),
            track_identifier=(
                str(payload["track_identifier"]).strip()
                if payload.get("track_identifier")
                else None
            ),
        )


@dataclass
class EnrichmentEntry:
    tags: List[str]
    mood: Optional[str]
    listeners: int
    playcount: int
    duration_ms: Optional[int]
    fetched_at: float
    mood_vector_id: Optional[str] = None
    energy: Optional[str] = None

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.fetched_at) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "EnrichmentEntry":
        tags_raw = payload.get("tags") or []
        tags = [str(tag).lower() for tag in tags_raw if str(tag).strip()]
        return cls(
            tags=tags,
            mood=(str(payload["mood"]).strip() if payload.get("mood") else None),
            listeners=int(payload.get("listeners", 0) or 0),
            playcount=int(payload.get("playcount", 0) or 0),
            duration_ms=payload.get("duration_ms"),
            fetched_at=float(payload.get("fetched_at", 0.0)),
            mood_vector_id=(
                str(payload["mood_vector_id"]).strip()
                if payload.get("mood_vector_id")
                else None
            ),
            energy=(str(payload["energy"]).strip() if payload.get("energy") else None),
        )


@dataclass
class ParsingEntry:
    artist: str
    title: str
    confidence: float
    parsed_at: float

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.parsed_at) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ParsingEntry":
        return cls(
            artist=str(payload.get("artist", "")),
            title=str(payload.get("title", "")),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            parsed_at=float(payload.get("parsed_at", 0.0)),
        )


@dataclass
class MoodVectorEntry:
    energy: float
    valence: float
    tempo: float
    confidence: float
    mood: Optional[str]
    fetched_at: float

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.fetched_at) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "MoodVectorEntry":
        return cls(
            energy=float(payload.get("energy", 0.0) or 0.0),
            valence=float(payload.get("valence", 0.0) or 0.0),
            tempo=float(payload.get("tempo", 0.0) or 0.0),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            mood=(str(payload["mood"]).strip() if payload.get("mood") else None),
            fetched_at=float(payload.get("fetched_at", 0.0)),
        )


@dataclass
class CollaborativeSnapshot:
    embeddings: Dict[str, List[float]]
    trained_at: float
    version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CollaborativeSnapshot":
        embeddings = payload.get("embeddings") or {}
        safe_embeddings = {
            str(key): [float(v) for v in (values or [])]
            for key, values in embeddings.items()
        }
        return cls(
            embeddings=safe_embeddings,
            trained_at=float(payload.get("trained_at", 0.0)),
            version=(str(payload["version"]).strip() if payload.get("version") else None),
        )


class CacheManager:
    """Persistent cache for autoplay V2 subsystems."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        mapping_ttl_days: int = 360,
        enrichment_ttl_days: int = 180,
        parsing_ttl_days: int = 120,
        mood_ttl_days: int = 180,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._mapping_ttl = mapping_ttl_days * _SECONDS_PER_DAY
        self._enrichment_ttl = enrichment_ttl_days * _SECONDS_PER_DAY
        self._parsing_ttl = parsing_ttl_days * _SECONDS_PER_DAY
        self._mood_ttl = mood_ttl_days * _SECONDS_PER_DAY

        self._mapping_file = self._cache_dir / "mappings_v2.json"
        self._enrichment_file = self._cache_dir / "enrichment_v2.json"
        self._parsing_file = self._cache_dir / "parsing_v2.json"
        self._mood_file = self._cache_dir / "mood_vectors_v2.json"
        self._collab_file = self._cache_dir / "collaborative_embeddings.json"

        self._mapping_cache = self._load_map(self._mapping_file, MappingEntry)
        self._enrichment_cache = self._load_map(self._enrichment_file, EnrichmentEntry)
        self._parsing_cache = self._load_map(self._parsing_file, ParsingEntry)
        self._mood_cache = self._load_map(self._mood_file, MoodVectorEntry)
        self._collaborative_snapshot = self._load_collaborative()

        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Mapping cache
    # ------------------------------------------------------------------
    async def get_mapping(self, artist: str, title: str) -> Optional[MappingEntry]:
        key = self._normalize_key(artist, title)
        entry = self._mapping_cache.get(key)
        if not entry:
            return None
        if entry.is_expired(self._mapping_ttl):
            await self._delete_mapping(key)
            return None
        return entry

    async def set_mapping(self, artist: str, title: str, entry: MappingEntry) -> None:
        key = self._normalize_key(artist, title)
        async with self._lock:
            self._mapping_cache[key] = entry
            self._save_map(self._mapping_file, self._mapping_cache)

    async def delete_mapping(self, artist: str, title: str) -> None:
        key = self._normalize_key(artist, title)
        await self._delete_mapping(key)

    async def _delete_mapping(self, key: str) -> None:
        async with self._lock:
            if key in self._mapping_cache:
                self._mapping_cache.pop(key, None)
                self._save_map(self._mapping_file, self._mapping_cache)

    # ------------------------------------------------------------------
    # Enrichment cache
    # ------------------------------------------------------------------
    async def get_enrichment(self, artist: str, title: str) -> Optional[EnrichmentEntry]:
        key = self._normalize_key(artist, title)
        entry = self._enrichment_cache.get(key)
        if not entry:
            return None
        if entry.is_expired(self._enrichment_ttl):
            await self._delete_enrichment(key)
            return None
        return entry

    async def set_enrichment(
        self,
        artist: str,
        title: str,
        entry: EnrichmentEntry,
    ) -> None:
        key = self._normalize_key(artist, title)
        async with self._lock:
            self._enrichment_cache[key] = entry
            self._save_map(self._enrichment_file, self._enrichment_cache)

    async def _delete_enrichment(self, key: str) -> None:
        async with self._lock:
            if key in self._enrichment_cache:
                self._enrichment_cache.pop(key, None)
                self._save_map(self._enrichment_file, self._enrichment_cache)

    # ------------------------------------------------------------------
    # Parsing cache (Gemini)
    # ------------------------------------------------------------------
    async def get_parsing(self, raw_title: str, channel_name: str) -> Optional[ParsingEntry]:
        key = self._normalize_parse_key(raw_title, channel_name)
        entry = self._parsing_cache.get(key)
        if not entry:
            return None
        if entry.is_expired(self._parsing_ttl):
            await self._delete_parsing(key)
            return None
        return entry

    async def set_parsing(
        self,
        raw_title: str,
        channel_name: str,
        entry: ParsingEntry,
    ) -> None:
        key = self._normalize_parse_key(raw_title, channel_name)
        async with self._lock:
            self._parsing_cache[key] = entry
            self._save_map(self._parsing_file, self._parsing_cache)

    async def _delete_parsing(self, key: str) -> None:
        async with self._lock:
            if key in self._parsing_cache:
                self._parsing_cache.pop(key, None)
                self._save_map(self._parsing_file, self._parsing_cache)

    # ------------------------------------------------------------------
    # Mood vector cache
    # ------------------------------------------------------------------
    async def get_mood_vector(self, key_id: str) -> Optional[MoodVectorEntry]:
        entry = self._mood_cache.get(key_id)
        if not entry:
            return None
        if entry.is_expired(self._mood_ttl):
            await self._delete_mood_vector(key_id)
            return None
        return entry

    async def set_mood_vector(self, key_id: str, entry: MoodVectorEntry) -> None:
        async with self._lock:
            self._mood_cache[key_id] = entry
            self._save_map(self._mood_file, self._mood_cache)

    async def _delete_mood_vector(self, key_id: str) -> None:
        async with self._lock:
            if key_id in self._mood_cache:
                self._mood_cache.pop(key_id, None)
                self._save_map(self._mood_file, self._mood_cache)

    # ------------------------------------------------------------------
    # Collaborative embeddings
    # ------------------------------------------------------------------
    async def get_collaborative_snapshot(self) -> Optional[CollaborativeSnapshot]:
        return self._collaborative_snapshot

    async def set_collaborative_snapshot(
        self,
        snapshot: CollaborativeSnapshot,
    ) -> None:
        async with self._lock:
            self._collaborative_snapshot = snapshot
            self._save_json(self._collab_file, snapshot.to_dict())

    # ------------------------------------------------------------------
    # Maintenance helpers
    # ------------------------------------------------------------------
    async def purge_expired(self) -> None:
        now = time.time()
        stale_map = [
            key
            for key, entry in self._mapping_cache.items()
            if (now - entry.timestamp) > self._mapping_ttl
        ]
        stale_enrichment = [
            key
            for key, entry in self._enrichment_cache.items()
            if (now - entry.fetched_at) > self._enrichment_ttl
        ]
        stale_parsing = [
            key
            for key, entry in self._parsing_cache.items()
            if (now - entry.parsed_at) > self._parsing_ttl
        ]
        stale_mood = [
            key
            for key, entry in self._mood_cache.items()
            if (now - entry.fetched_at) > self._mood_ttl
        ]

        for key in stale_map:
            await self._delete_mapping(key)
        for key in stale_enrichment:
            await self._delete_enrichment(key)
        for key in stale_parsing:
            await self._delete_parsing(key)
        for key in stale_mood:
            await self._delete_mood_vector(key)

    def get_cache_stats(self) -> Dict[str, Any]:
        return {
            "mappings": len(self._mapping_cache),
            "enrichment": len(self._enrichment_cache),
            "parsing": len(self._parsing_cache),
            "mood_vectors": len(self._mood_cache),
            "mapping_ttl_days": self._mapping_ttl / _SECONDS_PER_DAY,
            "enrichment_ttl_days": self._enrichment_ttl / _SECONDS_PER_DAY,
            "parsing_ttl_days": self._parsing_ttl / _SECONDS_PER_DAY,
            "mood_ttl_days": self._mood_ttl / _SECONDS_PER_DAY,
            "collaborative_snapshot": bool(self._collaborative_snapshot),
        }

    # ------------------------------------------------------------------
    # Internal persistence helpers
    # ------------------------------------------------------------------
    def _load_map(self, file_path: Path, entry_cls: Any) -> Dict[str, Any]:
        if not file_path.exists():
            return {}
        try:
            with file_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return {}

        result: Dict[str, Any] = {}
        if not isinstance(data, dict):
            return result

        for key, raw_entry in data.items():
            if not isinstance(raw_entry, dict):
                continue
            try:
                entry = entry_cls.from_dict(raw_entry)
            except Exception:
                continue
            result[str(key)] = entry
        return result

    def _save_map(self, file_path: Path, entries: Dict[str, Any]) -> None:
        serialized = {key: entry.to_dict() for key, entry in entries.items()}
        self._save_json(file_path, serialized)

    def _load_collaborative(self) -> Optional[CollaborativeSnapshot]:
        if not self._collab_file.exists():
            return None
        try:
            with self._collab_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return CollaborativeSnapshot.from_dict(data)
        except Exception:
            return None

    def _save_json(self, file_path: Path, payload: Dict[str, Any]) -> None:
        tmp_path = file_path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            tmp_path.replace(file_path)
        except OSError:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _normalize_key(artist: str, title: str) -> str:
        return f"{artist.lower().strip()}::{title.lower().strip()}"

    @staticmethod
    def _normalize_parse_key(raw_title: str, channel_name: str) -> str:
        title_key = raw_title.lower().strip()
        channel_key = channel_name.lower().strip()
        return f"{title_key}||{channel_key}"
