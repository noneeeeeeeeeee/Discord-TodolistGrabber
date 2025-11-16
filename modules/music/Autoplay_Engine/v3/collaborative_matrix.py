import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .cache_manager import CacheManager, CollaborativeSnapshot

LOG = logging.getLogger(__name__)

_TRACK_PREFIX = "track::"
_GENRE_PREFIX = "genre::"


class CollaborativeMatrix:
    """Manages collaborative embeddings and serves similarity lookups."""

    def __init__(
        self,
        cache_manager: CacheManager,
        *,
        default_source: Optional[Path | str] = None,
    ) -> None:
        self._cache = cache_manager
        self._default_source = Path(default_source) if default_source else None
        self._snapshot: Optional[CollaborativeSnapshot] = None
        self._track_vectors: Dict[str, List[float]] = {}
        self._genre_vectors: Dict[str, List[float]] = {}
        self._norm_cache: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def hydrate(self) -> bool:
        async with self._lock:
            if self._snapshot is not None:
                return True

            cached = await self._cache.get_collaborative_snapshot()
            if cached:
                self._ingest_snapshot(cached)
                return True

            if self._default_source and self._default_source.exists():
                loaded = await self.load_from_file(self._default_source, persist=True)
                return loaded

            return False

    async def load_from_file(self, file_path: Path | str, *, persist: bool = False) -> bool:
        path = Path(file_path)
        if not path.exists():
            LOG.warning("Collaborative matrix source missing: %s", path)
            return False

        try:
            data = await asyncio.to_thread(self._read_json, path)
        except Exception as exc:  # pragma: no cover - disk failure path
            LOG.error("Failed to load collaborative snapshot (%s): %s", path, exc)
            return False

        snapshot = self._build_snapshot(data)
        if snapshot is None:
            LOG.warning("Collaborative snapshot malformed: %s", path)
            return False

        async with self._lock:
            self._ingest_snapshot(snapshot)
            if persist:
                await self._cache.set_collaborative_snapshot(snapshot)
        LOG.info(
            "Collaborative matrix loaded (%s vectors, trained_at=%s, version=%s)",
            len(self._track_vectors) + len(self._genre_vectors),
            snapshot.trained_at,
            snapshot.version,
        )
        return True

    def get_track_vector(self, track_id: str) -> Optional[List[float]]:
        return self._track_vectors.get(track_id)

    def get_genre_vector(self, genre: str) -> Optional[List[float]]:
        return self._genre_vectors.get(genre.lower())

    def track_similarity(self, track_a: str, track_b: str) -> float:
        if track_a == track_b:
            return 1.0
        vector_a = self._track_vectors.get(track_a)
        vector_b = self._track_vectors.get(track_b)
        if not vector_a or not vector_b:
            return 0.0
        return self._cosine(
            vector_a,
            vector_b,
            f"{_TRACK_PREFIX}{track_a}",
            f"{_TRACK_PREFIX}{track_b}",
        )

    def similar_tracks(
        self,
        track_id: str,
        *,
        limit: int = 10,
        candidates: Optional[Sequence[str]] = None,
    ) -> List[Tuple[str, float]]:
        anchor = self._track_vectors.get(track_id)
        if not anchor:
            return []

        scores: List[Tuple[str, float]] = []
        if candidates:
            iterator: Iterable[Tuple[str, List[float]]] = (
                (candidate, self._track_vectors[candidate])
                for candidate in candidates
                if candidate in self._track_vectors
            )
        else:
            iterator = self._track_vectors.items()

        for other_id, vector in iterator:
            if other_id == track_id:
                continue
            score = self._cosine(
                anchor,
                vector,
                f"{_TRACK_PREFIX}{track_id}",
                f"{_TRACK_PREFIX}{other_id}",
            )
            if score <= 0:
                continue
            scores.append((other_id, score))

        scores.sort(key=lambda item: item[1], reverse=True)
        return scores[: max(0, limit)]

    def snapshot_metadata(self) -> Dict[str, Any]:
        snapshot = self._snapshot
        if not snapshot:
            return {"available": False}
        return {
            "available": True,
            "trained_at": snapshot.trained_at,
            "version": snapshot.version,
            "track_vectors": len(self._track_vectors),
            "genre_vectors": len(self._genre_vectors),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ingest_snapshot(self, snapshot: CollaborativeSnapshot) -> None:
        self._snapshot = snapshot
        track_vectors: Dict[str, List[float]] = {}
        genre_vectors: Dict[str, List[float]] = {}
        norm_cache: Dict[str, float] = {}

        for key, raw_vector in snapshot.embeddings.items():
            vector = self._sanitize_vector(raw_vector)
            if not vector:
                continue
            if key.startswith(_TRACK_PREFIX):
                track_id = key[len(_TRACK_PREFIX) :]
                track_vectors[track_id] = vector
                norm_cache[f"{_TRACK_PREFIX}{track_id}"] = self._vector_norm(vector)
            elif key.startswith(_GENRE_PREFIX):
                genre = key[len(_GENRE_PREFIX) :].lower()
                genre_vectors[genre] = vector
                norm_cache[f"{_GENRE_PREFIX}{genre}"] = self._vector_norm(vector)

        self._track_vectors = track_vectors
        self._genre_vectors = genre_vectors
        self._norm_cache = norm_cache

    def _build_snapshot(self, payload: Dict[str, Any]) -> Optional[CollaborativeSnapshot]:
        track_embeddings = payload.get("track_embeddings") or {}
        genre_embeddings = payload.get("genre_embeddings") or {}
        if not isinstance(track_embeddings, dict) and not isinstance(genre_embeddings, dict):
            return None

        embeddings: Dict[str, List[float]] = {}
        self._ingest_embeddings(embeddings, track_embeddings, _TRACK_PREFIX)
        self._ingest_embeddings(embeddings, genre_embeddings, _GENRE_PREFIX)

        trained_at = float(payload.get("trained_at") or time.time())
        version_value = payload.get("version")
        version = str(version_value).strip() if isinstance(version_value, str) else None
        return CollaborativeSnapshot(embeddings=embeddings, trained_at=trained_at, version=version)

    def _ingest_embeddings(
        self,
        target: Dict[str, List[float]],
        source: Dict[str, Any],
        prefix: str,
    ) -> None:
        if not isinstance(source, dict):
            return
        for key, values in source.items():
            vector = self._sanitize_vector(values)
            if not vector:
                continue
            target[f"{prefix}{key}"] = vector

    def _sanitize_vector(self, values: Any) -> List[float]:
        if not isinstance(values, (list, tuple)):
            return []
        vector: List[float] = []
        for value in values:
            try:
                vector.append(float(value))
            except (TypeError, ValueError):
                return []
        return vector

    def _cosine(self, a: Sequence[float], b: Sequence[float], key_a: str, key_b: str) -> float:
        norm_a = self._norm_cache.get(key_a)
        if norm_a is None:
            norm_a = self._vector_norm(a)
            self._norm_cache[key_a] = norm_a
        norm_b = self._norm_cache.get(key_b)
        if norm_b is None:
            norm_b = self._vector_norm(b)
            self._norm_cache[key_b] = norm_b

        if norm_a == 0 or norm_b == 0:
            return 0.0

        dot = sum(x * y for x, y in zip(a, b))
        return dot / (norm_a * norm_b)

    @staticmethod
    def _vector_norm(values: Sequence[float]) -> float:
        return math.sqrt(sum(component * component for component in values))

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)


__all__ = ["CollaborativeMatrix"]
