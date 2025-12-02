import asyncio
import json
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "MappingEntry",
    "EnrichmentEntry",
    "ParsingEntry",
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
    title: Optional[str] = None
    preview_url: Optional[str] = None
    preview_duration_ms: Optional[int] = None
    preview_fetched_at: Optional[float] = None
    deezer_track_id: Optional[str] = None
    ingest_source: Optional[str] = None
    heuristic_score: float = 0.0
    title_similarity: Optional[float] = None
    artist_similarity: Optional[float] = None
    channel_similarity: Optional[float] = None
    engagement_score: Optional[float] = None
    duration_score: Optional[float] = None
    content_penalty: Optional[float] = None
    spam_penalty: Optional[float] = None
    spam_flags: List[str] = field(default_factory=list)
    search_rank: Optional[int] = None
    heuristic_version: int = 2

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.timestamp) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "MappingEntry":
        def _safe_float(value: Any) -> Optional[float]:
            try:
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        def _safe_int(value: Any) -> Optional[int]:
            try:
                if value is None:
                    return None
                return int(value)
            except (TypeError, ValueError):
                return None

        spam_flags_raw = payload.get("spam_flags")
        if isinstance(spam_flags_raw, list):
            spam_flags = [str(flag) for flag in spam_flags_raw if str(flag).strip()]
        else:
            spam_flags = []

        return cls(
            youtube_id=str(payload.get("youtube_id", "")),
            url=str(payload.get("url", "")),
            timestamp=float(payload.get("timestamp", 0.0)),
            channel_name=payload.get("channel_name"),
            verified=bool(payload.get("verified", False)),
            duration_ms=_safe_int(payload.get("duration_ms")),
            track_identifier=(
                str(payload["track_identifier"]).strip()
                if payload.get("track_identifier")
                else None
            ),
            title=(
                str(payload.get("title", "")).strip() if payload.get("title") else None
            ),
            preview_url=(
                str(payload.get("preview_url", "")).strip()
                if payload.get("preview_url")
                else None
            ),
            preview_duration_ms=_safe_int(payload.get("preview_duration_ms")),
            preview_fetched_at=_safe_float(payload.get("preview_fetched_at")),
            deezer_track_id=(
                str(payload.get("deezer_track_id", "")).strip()
                if payload.get("deezer_track_id")
                else None
            ),
            ingest_source=(
                str(payload.get("ingest_source", "")).strip()
                if payload.get("ingest_source")
                else None
            ),
            heuristic_score=float(payload.get("heuristic_score", 0.0) or 0.0),
            title_similarity=_safe_float(payload.get("title_similarity")),
            artist_similarity=_safe_float(payload.get("artist_similarity")),
            channel_similarity=_safe_float(payload.get("channel_similarity")),
            engagement_score=_safe_float(payload.get("engagement_score")),
            duration_score=_safe_float(payload.get("duration_score")),
            content_penalty=_safe_float(payload.get("content_penalty")),
            spam_penalty=_safe_float(payload.get("spam_penalty")),
            spam_flags=spam_flags,
            search_rank=_safe_int(payload.get("search_rank")),
            heuristic_version=int(payload.get("heuristic_version", 1) or 1),
        )


@dataclass
class EnrichmentEntry:
    """
    V3 Enrichment Entry - Audio analysis results from Librosa + EfficientAT.
    
    V3 Architecture: Quality > Speed
    - All numeric vibes come from Librosa/MobileNet audio analysis
    - Gemini provides ONLY cultural context (tags, mood text, activity/daypart affinity)
    - No Gemini-based numeric estimates (removed in V3 cleanup)
    """
    tags: List[str]
    mood: Optional[str]  # Text description from Gemini (cultural context only)
    fetched_at: float
    
    # Extended metadata for V3 (from Deezer)
    bpm: Optional[int] = None  # Deezer metadata, NOT computed
    key: Optional[str] = None  # Deezer metadata (text like "C minor")
    genres: List[str] = field(default_factory=list)  # Deezer genres
    
    # ============================================================================
    # Cultural Context (Gemini) - Text-only, no numeric estimates
    # ============================================================================
    # NOTE: activity_affinity and daypart_affinity are kept for future use
    # They are populated by Gemini but NOT used in recommendation scoring currently
    activity_affinity: Optional[str] = None  # e.g., "workout", "study", "party"
    daypart_affinity: Optional[str] = None  # e.g., "morning", "evening", "night"
    emotional_intensity: Optional[float] = None  # 0.0-1.0 (kept for metadata)
    
    # ============================================================================
    # V3 Architecture - Librosa + EfficientAT MobileNet
    # ============================================================================
    # FLOW VECTOR (4D): For DJ-quality transitions and harmonic mixing
    computed_tempo: Optional[float] = None  # BPM as float (e.g., 120.0)
    computed_loudness: Optional[float] = None  # Loudness in dB (e.g., -5.883)
    computed_key: Optional[int] = None  # 0-11 (C=0, C#=1, D=2, ..., B=11)
    computed_mode: Optional[int] = None  # 0=minor, 1=major
    
    # ML MODE: Learned high-dimensional embedding (512D-2048D)
    # Used when analysis_mode="ml" with EfficientAT (MobileNetV3)
    computed_embedding: Optional[List[float]] = None  # 512D-2048D learned embedding
    computed_embedding_model: Optional[str] = None  # "mn10_as"
    computed_embedding_dim: Optional[int] = None  # Actual dimension (e.g., 1024, 2048)
    
    # NON-ML MODE: Simplified 5D vibe vector from Librosa features
    # Used when analysis_mode="non-ml" - no neural network required
    # 5D: [energy, valence, danceability, acousticness, brightness]
    computed_simple_vibe: Optional[List[float]] = None

    # Analysis pipeline bookkeeping
    analysis_verified: bool = False
    analysis_in_progress: bool = False
    last_analysis_attempt: Optional[float] = None

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.fetched_at) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "EnrichmentEntry":
        """
        Parse enrichment entry from cache.
        
        V3 Architecture: Only loads computed audio analysis fields.
        Legacy fields (energy string, mood_*, estimated_*) are ignored for backward compat.
        """
        tags_raw = payload.get("tags") or []
        tags = [str(tag).lower() for tag in tags_raw if str(tag).strip()]
        
        # Parse genres
        genres_raw = payload.get("genres") or []
        genres = [str(genre).strip() for genre in genres_raw if str(genre).strip()]
        
        # Parse computed_simple_vibe (5D from Librosa)
        computed_simple_vibe = None
        csv_raw = payload.get("computed_simple_vibe")
        if isinstance(csv_raw, list):
            try:
                cleaned = [max(0.0, min(1.0, float(v))) for v in csv_raw[:5]]
                if len(cleaned) == 5:
                    computed_simple_vibe = cleaned
            except (TypeError, ValueError):
                pass
        
        # Parse computed_embedding (512D-2048D from MobileNet)
        computed_embedding = None
        emb_raw = payload.get("computed_embedding")
        if isinstance(emb_raw, list) and len(emb_raw) >= 512:
            try:
                computed_embedding = [float(v) for v in emb_raw]
            except (TypeError, ValueError):
                pass
        
        return cls(
            tags=tags,
            mood=(str(payload["mood"]).strip() if payload.get("mood") else None),
            fetched_at=float(payload.get("fetched_at", 0.0)),
            bpm=(int(payload["bpm"]) if payload.get("bpm") else None),
            key=(str(payload["key"]).strip() if payload.get("key") else None),
            genres=genres,
            activity_affinity=(
                str(payload["activity_affinity"]).strip()
                if payload.get("activity_affinity")
                else None
            ),
            daypart_affinity=(
                str(payload["daypart_affinity"]).strip()
                if payload.get("daypart_affinity")
                else None
            ),
            emotional_intensity=(
                float(payload["emotional_intensity"])
                if payload.get("emotional_intensity") is not None
                else None
            ),
            computed_tempo=(
                float(payload["computed_tempo"])
                if payload.get("computed_tempo") is not None
                else None
            ),
            computed_loudness=(
                float(payload["computed_loudness"])
                if payload.get("computed_loudness") is not None
                else None
            ),
            computed_key=(
                int(payload["computed_key"])
                if payload.get("computed_key") is not None
                else None
            ),
            computed_mode=(
                int(payload["computed_mode"])
                if payload.get("computed_mode") is not None
                else None
            ),
            computed_embedding=computed_embedding,
            computed_embedding_model=(
                str(payload["computed_embedding_model"]).strip()
                if payload.get("computed_embedding_model")
                else None
            ),
            computed_embedding_dim=(
                int(payload["computed_embedding_dim"])
                if payload.get("computed_embedding_dim") is not None
                else None
            ),
            computed_simple_vibe=computed_simple_vibe,
            analysis_verified=bool(payload.get("analysis_verified", False)),
            analysis_in_progress=bool(payload.get("analysis_in_progress", False)),
            last_analysis_attempt=(
                float(payload["last_analysis_attempt"])
                if payload.get("last_analysis_attempt") is not None
                else None
            ),
        )


@dataclass
class ParsingEntry:
    artist: str
    title: str
    confidence: float
    parsed_at: float
    track_type: str = "music"  # "music", "ost", "game_soundtrack", "anime_opening"
    primary_entity: Optional[str] = None  # Franchise/show/game name for OST content
    
    # Phase 0.5: Resilient Parsing with Deezer-First Strategy
    is_canonical: bool = False  # Deezer verified (HIGH confidence, indefinite TTL)
    is_best_guess: bool = False  # Grounding verified (MEDIUM confidence, 7-day TTL)
    deezer_id: Optional[str] = None  # Deezer track ID for future enrichment
    
    # Self-healing cache fields
    retry_after_days: Optional[int] = None  # Adaptive TTL (1-30 days for fallback entries)
    failure_reason: Optional[str] = None  # "deezer_timeout", "no_match", "quota_exhausted", etc.
    last_retry_attempt: Optional[float] = None  # Timestamp of last retry attempt
    
    schema_version: int = 2  # Bumped to v2 for Phase 0.5

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.time() - self.parsed_at) > ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ParsingEntry":
        # Backward compatibility: default to "music" if track_type missing
        track_type = payload.get("track_type", "music")
        if not isinstance(track_type, str) or track_type not in {
            "music",
            "ost",
            "game_soundtrack",
            "anime_opening",
        }:
            track_type = "music"

        # Schema v2 fields (Phase 0.5)
        is_canonical = bool(payload.get("is_canonical", False))
        is_best_guess = bool(payload.get("is_best_guess", False))
        deezer_id = payload.get("deezer_id")
        retry_after_days = payload.get("retry_after_days")
        failure_reason = payload.get("failure_reason")
        last_retry_attempt = payload.get("last_retry_attempt")
        schema_version = int(payload.get("schema_version", 1))

        return cls(
            artist=str(payload.get("artist", "")),
            title=str(payload.get("title", "")),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            parsed_at=float(payload.get("parsed_at", 0.0)),
            track_type=track_type,
            primary_entity=(
                str(payload["primary_entity"]).strip()
                if payload.get("primary_entity")
                else None
            ),
            is_canonical=is_canonical,
            is_best_guess=is_best_guess,
            deezer_id=str(deezer_id).strip() if deezer_id else None,
            retry_after_days=int(retry_after_days) if retry_after_days else None,
            failure_reason=str(failure_reason).strip() if failure_reason else None,
            last_retry_attempt=float(last_retry_attempt) if last_retry_attempt else None,
            schema_version=schema_version,
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
            version=(
                str(payload["version"]).strip() if payload.get("version") else None
            ),
        )


class CacheManager:
    """Persistent cache for autoplay V3 subsystems."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        mapping_ttl_days: int = 360,
        enrichment_ttl_days: int = 180,
        parsing_ttl_days: int = 120,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._mapping_ttl = mapping_ttl_days * _SECONDS_PER_DAY
        self._enrichment_ttl = enrichment_ttl_days * _SECONDS_PER_DAY
        self._parsing_ttl = parsing_ttl_days * _SECONDS_PER_DAY

        self._mapping_file = self._cache_dir / "mappings_v2.json"
        self._enrichment_file = self._cache_dir / "enrichment_v2.json"
        self._parsing_file = self._cache_dir / "parsing_v2.json"
        self._collab_file = self._cache_dir / "collaborative_embeddings.json"
        self._ingest_queue_file = self._cache_dir / "analysis_queue.json"

        self._mapping_cache = self._load_map(self._mapping_file, MappingEntry)
        self._enrichment_cache = self._load_map(self._enrichment_file, EnrichmentEntry)
        self._parsing_cache = self._load_map(self._parsing_file, ParsingEntry)
        self._collaborative_snapshot = self._load_collaborative()

        self._lock = asyncio.Lock()
        self._ingest_lock = threading.Lock()

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
    async def get_enrichment(
        self, artist: str, title: str
    ) -> Optional[EnrichmentEntry]:
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

    async def delete_enrichment_entry(self, artist: str, title: str) -> None:
        key = self._normalize_key(artist, title)
        await self._delete_enrichment(key)

    async def _delete_enrichment(self, key: str) -> None:
        async with self._lock:
            if key in self._enrichment_cache:
                self._enrichment_cache.pop(key, None)
                self._save_map(self._enrichment_file, self._enrichment_cache)

    # ------------------------------------------------------------------
    # Parsing cache (Gemini) - Phase 0.5: Self-Healing with Adaptive TTL
    # ------------------------------------------------------------------
    async def get_parsing(
        self, raw_title: str, channel_name: str
    ) -> Optional[ParsingEntry]:
        """
        Retrieve parsing entry with self-healing TTL logic.
        
        TTL Strategy:
        - Canonical (Deezer verified): Never expire (indefinite TTL)
        - Best guess (Grounding verified): 7-day TTL
        - Fallback (unverified): Adaptive 1-30 day TTL based on failure_reason
        """
        key = self._normalize_parse_key(raw_title, channel_name)
        entry = self._parsing_cache.get(key)
        if not entry:
            return None

        # Canonical entries never expire (indefinite TTL)
        if entry.is_canonical:
            return entry

        # Best guess entries expire after 7 days
        if entry.is_best_guess:
            age_days = (time.time() - entry.parsed_at) / _SECONDS_PER_DAY
            if age_days > 7:
                # Log self-healing retry trigger
                import logging
                LOG = logging.getLogger(__name__)
                LOG.info(
                    f"🔄 Best guess entry stale ({age_days:.1f} days), re-parsing: "
                    f"'{raw_title}' (channel: {channel_name})"
                )
                await self._delete_parsing(key)
                return None  # Trigger re-parse
            return entry

        # Fallback entries use adaptive TTL based on failure reason
        if entry.retry_after_days is not None:
            age_days = (time.time() - entry.parsed_at) / _SECONDS_PER_DAY
            if age_days > entry.retry_after_days:
                # Log self-healing retry trigger
                import logging
                LOG = logging.getLogger(__name__)
                LOG.info(
                    f"🔄 Fallback entry stale (TTL {entry.retry_after_days} days, age {age_days:.1f} days), "
                    f"re-parsing: '{raw_title}' (failure_reason: {entry.failure_reason})"
                )
                await self._delete_parsing(key)
                return None  # Trigger self-healing retry
            return entry

        # Legacy entries (no retry_after_days) default to 120 days
        if entry.is_expired(self._parsing_ttl):
            await self._delete_parsing(key)
            return None
        return entry

    def calculate_adaptive_ttl(self, failure_reason: str) -> int:
        """
        Calculate TTL in days based on failure type for self-healing.
        
        TTL Map:
        - deezer_timeout: 1 day (network issue, retry soon)
        - no_match: 7 days (might get indexed later)
        - quota_exhausted: 1 day (grounding resets daily)
        - grounding_failed: 3 days
        - unofficial_content: 30 days (unlikely to change)
        - nightcore/mashup: 30 days (remixes don't get canonical metadata)
        - cover: 14 days (covers might get official releases)
        
        Returns:
            Number of days until retry
        """
        ttl_map = {
            "deezer_timeout": 1,
            "no_match": 7,
            "quota_exhausted": 1,
            "grounding_failed": 3,
            "unofficial_content": 30,
            "nightcore": 30,
            "mashup": 30,
            "cover": 14,
            "fan_made": 30,
        }
        return ttl_map.get(failure_reason, 7)  # Default 7 days

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

    async def delete_parsing(self, raw_title: str, channel_name: str) -> None:
        key = self._normalize_parse_key(raw_title, channel_name)
        await self._delete_parsing(key)

    async def _delete_parsing(self, key: str) -> None:
        async with self._lock:
            if key in self._parsing_cache:
                self._parsing_cache.pop(key, None)
                self._save_map(self._parsing_file, self._parsing_cache)

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
    async def clear_mappings(self) -> None:
        """Clear all Last.fm -> YouTube mapping entries (disk + memory)."""
        async with self._lock:
            self._mapping_cache.clear()
            self._save_map(self._mapping_file, self._mapping_cache)

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

        for key in stale_map:
            await self._delete_mapping(key)
        for key in stale_enrichment:
            await self._delete_enrichment(key)
        for key in stale_parsing:
            await self._delete_parsing(key)

    def get_cache_stats(self) -> Dict[str, Any]:
        return {
            "mappings": len(self._mapping_cache),
            "enrichment": len(self._enrichment_cache),
            "parsing": len(self._parsing_cache),
            "mapping_ttl_days": self._mapping_ttl / _SECONDS_PER_DAY,
            "enrichment_ttl_days": self._enrichment_ttl / _SECONDS_PER_DAY,
            "parsing_ttl_days": self._parsing_ttl / _SECONDS_PER_DAY,
            "collaborative_snapshot": bool(self._collaborative_snapshot),
        }

    # ------------------------------------------------------------------
    # Persistent ingest queue helpers
    # ------------------------------------------------------------------
    def load_ingest_queue_state(self) -> Dict[str, List[Dict[str, Any]]]:
        """Load pending/in-progress ingest jobs from disk."""

        with self._ingest_lock:
            if not self._ingest_queue_file.exists():
                return {"pending": [], "in_progress": []}
            try:
                with self._ingest_queue_file.open("r", encoding="utf-8") as handle:
                    raw_state = json.load(handle)
            except (OSError, ValueError, TypeError):
                return {"pending": [], "in_progress": []}

        def _sanitize_list(payload: Any) -> List[Dict[str, Any]]:
            cleaned: List[Dict[str, Any]] = []
            if not isinstance(payload, list):
                return cleaned
            for item in payload:
                if not isinstance(item, dict):
                    continue
                track_id = str(item.get("track_id", "")).strip()
                youtube_url = str(item.get("youtube_url", "")).strip()
                if not track_id or not youtube_url:
                    continue
                payload = {
                    "track_id": track_id,
                    "youtube_url": youtube_url,
                    "attempts": int(item.get("attempts", 0) or 0),
                    "enqueued_at": float(item.get("enqueued_at", time.time()) or time.time()),
                    "last_error": item.get("last_error"),
                }
                preview_url = str(item.get("preview_url", "")).strip()
                if preview_url:
                    payload["preview_url"] = preview_url
                try:
                    preview_duration = item.get("preview_duration_ms")
                    if preview_duration is not None:
                        payload["preview_duration_ms"] = int(preview_duration)
                except (TypeError, ValueError):
                    pass
                deezer_track_id = str(item.get("deezer_track_id", "")).strip()
                if deezer_track_id:
                    payload["deezer_track_id"] = deezer_track_id
                cleaned.append(payload)
            return cleaned

        pending = _sanitize_list(raw_state.get("pending")) if isinstance(raw_state, dict) else []
        in_progress = _sanitize_list(raw_state.get("in_progress")) if isinstance(raw_state, dict) else []
        return {"pending": pending, "in_progress": in_progress}

    def persist_ingest_queue_state(
        self,
        pending: List[Dict[str, Any]],
        in_progress: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist ingest queue state for crash-safe recovery."""

        payload = {
            "pending": pending or [],
            "in_progress": in_progress or [],
            "saved_at": time.time(),
        }
        with self._ingest_lock:
            self._save_json(self._ingest_queue_file, payload)

    def clear_ingest_queue_state(self) -> None:
        with self._ingest_lock:
            if self._ingest_queue_file.exists():
                self._ingest_queue_file.unlink(missing_ok=True)

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
