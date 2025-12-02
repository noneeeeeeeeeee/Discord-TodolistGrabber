import asyncio
import json
import logging
import math
import time
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .cache_manager import CacheManager, CollaborativeSnapshot

LOG = logging.getLogger(__name__)

_TRACK_PREFIX = "track::"
_GENRE_PREFIX = "genre::"
_ARTIST_PREFIX = "artist::"

# Collaboration Graph boost values (per v3_reimplementation.md)
_COLLAB_SINGLE_BOOST = 0.2   # Artist B featured on 1 track with liked Artist A
_COLLAB_MULTI_BOOST = 0.4    # Artist B featured on 2+ tracks with liked Artist A


class CollaborativeMatrix:
    """Manages collaborative embeddings, similarity lookups, and collaboration graphs."""

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
        
        # Collaboration Graph (Apple Music style)
        # Maps: artist -> {collaborator -> collab_count}
        self._collaboration_graph: Dict[str, Dict[str, int]] = {}
        
        # Liked artists per guild for collaboration boost
        # Maps: guild_id -> set of liked artist names
        self._liked_artists: Dict[str, Set[str]] = {}

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

    async def get_popular_tracks(self, limit: int = 50) -> List[str]:
        """
        Get a list of track IDs from the matrix.
        Since the matrix is built from listening history, these are implicitly 'popular'.
        """
        keys = list(self._track_vectors.keys())
        if not keys:
            return []
        # Return a random sample to avoid analyzing the same tracks every time
        if len(keys) <= limit:
            return keys
        return random.sample(keys, limit)

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
            "collaboration_edges": sum(
                len(collabs) for collabs in self._collaboration_graph.values()
            ),
        }

    # ------------------------------------------------------------------
    # Collaboration Graph (Apple Music style)
    # ------------------------------------------------------------------
    def record_collaboration(
        self,
        primary_artist: str,
        collaborators: List[str],
    ) -> None:
        """
        Record collaborations between artists.
        
        Called when enriching tracks with featured artists.
        
        Args:
            primary_artist: Main artist on the track
            collaborators: List of featured/collaborating artists
        """
        primary_lower = primary_artist.lower()
        
        for collab in collaborators:
            collab_lower = collab.lower()
            if collab_lower == primary_lower:
                continue  # Skip self-reference
            
            # Bidirectional: A collaborated with B, B collaborated with A
            if primary_lower not in self._collaboration_graph:
                self._collaboration_graph[primary_lower] = {}
            if collab_lower not in self._collaboration_graph:
                self._collaboration_graph[collab_lower] = {}
            
            # Increment collaboration count
            self._collaboration_graph[primary_lower][collab_lower] = (
                self._collaboration_graph[primary_lower].get(collab_lower, 0) + 1
            )
            self._collaboration_graph[collab_lower][primary_lower] = (
                self._collaboration_graph[collab_lower].get(primary_lower, 0) + 1
            )

    def mark_artist_liked(self, guild_id: str, artist: str) -> None:
        """
        Mark an artist as liked for a guild (for collaboration boost).
        
        Args:
            guild_id: Guild identifier
            artist: Artist name
        """
        guild_str = str(guild_id)
        if guild_str not in self._liked_artists:
            self._liked_artists[guild_str] = set()
        self._liked_artists[guild_str].add(artist.lower())

    def get_collaboration_boost(
        self,
        guild_id: str,
        artist: str,
    ) -> float:
        """
        Calculate collaboration boost for an artist based on liked artists.
        
        Per v3_reimplementation.md:
        - Artist B featured on 1 track with liked Artist A: +0.2
        - Artist B featured on 2+ tracks with liked Artist A: +0.4
        - Collaborative tracks (A + B together): +0.4
        
        Args:
            guild_id: Guild identifier
            artist: Artist to check for collaboration boost
            
        Returns:
            Boost value (0.0, 0.2, or 0.4)
        """
        guild_str = str(guild_id)
        
        if guild_str not in self._liked_artists:
            return 0.0
        
        liked = self._liked_artists[guild_str]
        artist_lower = artist.lower()
        
        # If this artist is already liked, no additional boost
        if artist_lower in liked:
            return 0.0
        
        # Check if this artist has collaborated with any liked artist
        if artist_lower not in self._collaboration_graph:
            return 0.0
        
        artist_collabs = self._collaboration_graph[artist_lower]
        
        max_collab_count = 0
        for liked_artist in liked:
            if liked_artist in artist_collabs:
                max_collab_count = max(max_collab_count, artist_collabs[liked_artist])
        
        if max_collab_count >= 2:
            return _COLLAB_MULTI_BOOST  # +0.4 for 2+ collaborations
        elif max_collab_count >= 1:
            return _COLLAB_SINGLE_BOOST  # +0.2 for 1 collaboration
        
        return 0.0

    def get_collaborative_artists(
        self,
        artist: str,
        *,
        min_collabs: int = 1,
    ) -> List[Tuple[str, int]]:
        """
        Get list of artists who have collaborated with the given artist.
        
        Args:
            artist: Artist to find collaborators for
            min_collabs: Minimum collaboration count to include
            
        Returns:
            List of (artist_name, collab_count) tuples, sorted by count desc
        """
        artist_lower = artist.lower()
        
        if artist_lower not in self._collaboration_graph:
            return []
        
        collabs = [
            (name, count)
            for name, count in self._collaboration_graph[artist_lower].items()
            if count >= min_collabs
        ]
        
        # Sort by collaboration count (descending)
        collabs.sort(key=lambda x: x[1], reverse=True)
        return collabs

    def build_collaboration_clusters(
        self,
        seed_artist: str,
        *,
        depth: int = 2,
        min_collabs: int = 2,
    ) -> Set[str]:
        """
        Build a cluster of related artists through collaboration chains.
        
        Useful for genre bridging - discovering new artists through
        trusted connections.
        
        Args:
            seed_artist: Starting artist
            depth: How many hops to follow (1 = direct collabs, 2 = collabs of collabs)
            min_collabs: Minimum collaboration count to follow
            
        Returns:
            Set of artist names in the cluster
        """
        cluster: Set[str] = {seed_artist.lower()}
        frontier = {seed_artist.lower()}
        
        for _ in range(depth):
            next_frontier: Set[str] = set()
            
            for artist in frontier:
                collabs = self.get_collaborative_artists(artist, min_collabs=min_collabs)
                for collab_name, _ in collabs:
                    if collab_name not in cluster:
                        cluster.add(collab_name)
                        next_frontier.add(collab_name)
            
            frontier = next_frontier
            if not frontier:
                break
        
        return cluster

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
