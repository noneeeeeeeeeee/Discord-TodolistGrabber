"""
V3 Autoplay Engine - Vector Search Index

Content-based similarity matching using audio vectors.
Uses audio embeddings (EfficientAT) and feature vectors for
audio content similarity when cache has 500+ analyzed songs.

This module provides:
- Vector indexing for fast similarity search
- Multi-feature similarity scoring (physics + semantics + librarian layers)
- Batch similarity computation
- Dynamic index updates as songs are analyzed

Note: This is NOT collaborative filtering in the traditional sense.
This performs CONTENT-BASED similarity using audio features.
For user behavior-based recommendations (transition patterns, 
skip behavior), see collaborative_recommender.py.
"""

import asyncio
import inspect
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from .cache_manager import CacheManager, get_cache_manager
from .constants import EventType, SongMetadata, V3Config
from .event_bus import EventBus, EventPayload

logger = logging.getLogger(__name__)


@dataclass
class SimilarityResult:
    """Result from similarity search."""
    song_id: str
    score: float
    metadata: Optional[SongMetadata] = None
    
    def __lt__(self, other: "SimilarityResult") -> bool:
        return self.score > other.score  # Higher score = more similar


class VectorIndex:
    """
    Simple vector index for similarity search.
    
    Uses cosine similarity for matching. For larger datasets,
    this could be replaced with a proper ANN library like FAISS.
    """
    
    def __init__(self, dimension: int):
        """
        Initialize vector index.
        
        Args:
            dimension: Vector dimension
        """
        self.dimension = dimension
        self._vectors: dict[str, np.ndarray] = {}
        self._normalized: dict[str, np.ndarray] = {}
    
    def add(self, song_id: str, vector: list[float]) -> None:
        """Add a vector to the index."""
        if len(vector) != self.dimension:
            # Pad or truncate to match dimension
            if len(vector) < self.dimension:
                vector = vector + [0.0] * (self.dimension - len(vector))
            else:
                vector = vector[:self.dimension]
        
        vec = np.array(vector, dtype=np.float32)
        self._vectors[song_id] = vec
        
        # Store normalized version for cosine similarity
        norm = np.linalg.norm(vec)
        if norm > 0:
            self._normalized[song_id] = vec / norm
        else:
            self._normalized[song_id] = vec
    
    def remove(self, song_id: str) -> None:
        """Remove a vector from the index."""
        self._vectors.pop(song_id, None)
        self._normalized.pop(song_id, None)
    
    def search(
        self,
        query: list[float],
        k: int = 10,
        exclude: Optional[set[str]] = None
    ) -> list[tuple[str, float]]:
        """
        Search for similar vectors.
        
        Args:
            query: Query vector
            k: Number of results
            exclude: Song IDs to exclude
            
        Returns:
            List of (song_id, similarity_score) tuples
        """
        if not self._normalized:
            return []
        
        exclude = exclude or set()
        
        # Normalize query
        query_vec = np.array(query[:self.dimension], dtype=np.float32)
        if len(query) < self.dimension:
            query_vec = np.pad(query_vec, (0, self.dimension - len(query)))
        
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm
        
        # Compute similarities
        results = []
        for song_id, vec in self._normalized.items():
            if song_id in exclude:
                continue
            
            # Cosine similarity (dot product of normalized vectors)
            similarity = float(np.dot(query_vec, vec))
            results.append((song_id, similarity))
        
        # Sort by similarity and return top k
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:k]
    
    def __len__(self) -> int:
        return len(self._vectors)


class VectorSearcher:
    """
    Content-based similarity search using audio features.
    
    Builds vector representations of songs from:
    - EfficientAT embeddings (128-dim semantic vectors) - Semantics Layer
    - Audio features (BPM, key, energy, etc.) - Physics Layer
    - Librarian features (genres, moods as bag-of-words) - Librarian Layer
    
    Only activates when cache has 500+ analyzed songs for
    meaningful similarity matching.
    
    This module provides MATHEMATICAL AUDIO SIMILARITY, not behavioral
    collaborative filtering. For behavior-based recommendations, see
    the CollaborativeRecommender in collaborative_recommender.py.
    """
    
    # Configuration
    EMBEDDING_DIM = 128
    FEATURE_DIM = 64
    MIN_SONGS_FOR_ACTIVATION = 500
    
    # Feature weights by layer
    EMBEDDING_WEIGHT = 0.5      # Semantics layer (EfficientAT)
    AUDIO_WEIGHT = 0.25         # Physics layer (Librosa)
    LIBRARIAN_WEIGHT = 0.25     # Librarian layer (Gemini genres/moods)
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        cache: Optional[CacheManager] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize vector searcher.
        
        Args:
            config: V3 configuration
            cache: Cache manager for song data
            event_bus: Event bus for notifications
        """
        self.config = config or V3Config()
        self.cache = cache or get_cache_manager()
        self.event_bus = event_bus or EventBus()
        
        # Vector indices
        self._embedding_index = VectorIndex(self.EMBEDDING_DIM)
        self._feature_index = VectorIndex(self.FEATURE_DIM)
        
        # Genre/mood vocabulary for bag-of-words
        self._genre_vocab: dict[str, int] = {}
        self._mood_vocab: dict[str, int] = {}
        
        # State
        self._active = False
        self._song_count = 0
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and build indices from cache."""
        if self._initialized:
            return
        
        await self.cache.initialize()
        
        subscribe_result = self.event_bus.subscribe(
            EventType.SONG_ANALYZED, self._on_song_analyzed
        )
        if inspect.isawaitable(subscribe_result):
            await subscribe_result
        
        # Build initial index from cache
        await self._build_index()
        
        self._initialized = True
        logger.info(
            f"Vector searcher initialized: {self._song_count} songs, "
            f"active={self._active}"
        )
    
    async def shutdown(self) -> None:
        """Clean up."""
        unsubscribe_result = self.event_bus.unsubscribe(
            EventType.SONG_ANALYZED, self._on_song_analyzed
        )
        if inspect.isawaitable(unsubscribe_result):
            await unsubscribe_result
        self._initialized = False
    
    async def _build_index(self) -> None:
        """Build vector index from cached songs."""
        count = await self.cache.get_analyzed_song_count()
        
        if count < self.MIN_SONGS_FOR_ACTIVATION:
            logger.info(
                f"Only {count} songs analyzed, need {self.MIN_SONGS_FOR_ACTIVATION} "
                f"for vector search"
            )
            self._active = False
            return
        
        # Get all metadata from cache
        # This is expensive, so we do it in batches
        # For now, we'll build incrementally as songs are analyzed
        self._song_count = count
        self._active = True
    
    async def _on_song_analyzed(self, payload: EventPayload) -> None:
        """Handle song analysis completion."""
        song_id = payload.data.get("song_id")
        if not song_id:
            return
        
        # Get metadata
        metadata = await self.cache.get_metadata(song_id)
        if not metadata:
            return
        
        # Add to indices
        self._index_song(metadata)
        
        # Check if we should activate
        self._song_count += 1
        if not self._active and self._song_count >= self.MIN_SONGS_FOR_ACTIVATION:
            self._active = True
            logger.info(f"Vector search now active with {self._song_count} songs")
    
    def _index_song(self, metadata: SongMetadata) -> None:
        """Add a song to the vector indices."""
        song_id = metadata.song_id
        
        # Index embedding if available
        if (
            metadata.semantic_features
            and metadata.semantic_features.embedding_vector
        ):
            self._embedding_index.add(
                song_id,
                metadata.semantic_features.embedding_vector
            )
        
        # Build and index feature vector
        feature_vec = self._build_feature_vector(metadata)
        if feature_vec:
            self._feature_index.add(song_id, feature_vec)
    
    def _build_feature_vector(
        self,
        metadata: SongMetadata
    ) -> Optional[list[float]]:
        """
        Build a feature vector from song metadata.
        
        Vector structure:
        - [0-9]: Audio features (normalized) - Physics Layer
        - [10-29]: Genre bag-of-words (top 20 genres) - Librarian Layer
        - [30-49]: Mood bag-of-words (top 20 moods) - Librarian Layer
        - [50-63]: Reserved/padding
        """
        features = [0.0] * self.FEATURE_DIM
        
        # Audio features (Physics Layer)
        if metadata.audio_features:
            audio = metadata.audio_features
            features[0] = audio.bpm / 200.0 if audio.bpm else 0.5
            features[1] = audio.energy if audio.energy else 0.5
            features[2] = (audio.loudness_db + 60) / 60 if audio.loudness_db else 0.5
            features[3] = audio.spectral_centroid / 10000 if audio.spectral_centroid else 0.5
            
            # Key encoding (chromatic circle)
            if audio.key:
                key_idx = self._key_to_index(audio.key)
                features[4] = math.cos(2 * math.pi * key_idx / 12)
                features[5] = math.sin(2 * math.pi * key_idx / 12)
            
            # Mode (major/minor)
            features[6] = 1.0 if audio.mode == "major" else 0.0
        
        # Librarian features
        if metadata.librarian_info:
            lib = metadata.librarian_info
            
            features[7] = lib.energy_level if lib.energy_level else 0.5
            features[8] = lib.danceability if lib.danceability else 0.5
            features[9] = 1.0 if lib.explicit_content else 0.0
            
            # Genre bag-of-words
            for genre in (lib.genres or [])[:5]:
                idx = self._get_genre_index(genre)
                if idx is not None and 10 + idx < 30:
                    features[10 + idx] = 1.0
            
            # Mood bag-of-words
            for mood in (lib.moods or [])[:5]:
                idx = self._get_mood_index(mood)
                if idx is not None and 30 + idx < 50:
                    features[30 + idx] = 1.0
        
        return features
    
    def _key_to_index(self, key: str) -> int:
        """Convert musical key to chromatic index."""
        keys = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
        key_upper = key.upper().replace('♯', '#').replace('♭', 'b')
        
        # Handle flats
        if 'b' in key_upper:
            flat_to_sharp = {
                'Db': 'C#', 'Eb': 'D#', 'Fb': 'E', 'Gb': 'F#',
                'Ab': 'G#', 'Bb': 'A#', 'Cb': 'B'
            }
            key_upper = flat_to_sharp.get(key_upper[:2], key_upper)
        
        try:
            return keys.index(key_upper[:2] if '#' in key_upper else key_upper[0])
        except (ValueError, IndexError):
            return 0
    
    def _get_genre_index(self, genre: str) -> Optional[int]:
        """Get or create index for genre in vocabulary."""
        genre_lower = genre.lower()
        
        if genre_lower not in self._genre_vocab:
            if len(self._genre_vocab) >= 20:
                return None  # Vocabulary full
            self._genre_vocab[genre_lower] = len(self._genre_vocab)
        
        return self._genre_vocab[genre_lower]
    
    def _get_mood_index(self, mood: str) -> Optional[int]:
        """Get or create index for mood in vocabulary."""
        mood_lower = mood.lower()
        
        if mood_lower not in self._mood_vocab:
            if len(self._mood_vocab) >= 20:
                return None
            self._mood_vocab[mood_lower] = len(self._mood_vocab)
        
        return self._mood_vocab[mood_lower]
    
    @property
    def is_active(self) -> bool:
        """Check if vector search is active."""
        return self._active and self._song_count >= self.MIN_SONGS_FOR_ACTIVATION
    
    async def find_similar(
        self,
        song_id: str,
        k: int = 20,
        exclude: Optional[set[str]] = None
    ) -> list[SimilarityResult]:
        """
        Find songs similar to a given song by audio content.
        
        Args:
            song_id: Reference song ID
            k: Number of results
            exclude: Song IDs to exclude
            
        Returns:
            List of similar songs with scores
        """
        if not self.is_active:
            return []
        
        exclude = exclude or set()
        exclude.add(song_id)  # Don't return the query song
        
        # Get query song
        metadata = await self.cache.get_metadata(song_id)
        if not metadata:
            return []
        
        results = await self.find_similar_to_features(
            metadata=metadata,
            k=k,
            exclude=exclude
        )
        
        return results
    
    async def find_similar_to_features(
        self,
        metadata: Optional[SongMetadata] = None,
        embedding: Optional[list[float]] = None,
        features: Optional[list[float]] = None,
        k: int = 20,
        exclude: Optional[set[str]] = None
    ) -> list[SimilarityResult]:
        """
        Find songs similar to given audio features.
        
        Can search by:
        - Full metadata (uses all available features)
        - Embedding only (Semantics layer)
        - Feature vector only (Physics + Librarian layers)
        
        Args:
            metadata: Full song metadata
            embedding: Semantic embedding vector
            features: Feature vector
            k: Number of results
            exclude: Song IDs to exclude
            
        Returns:
            List of similar songs with scores
        """
        if not self.is_active:
            return []
        
        exclude = exclude or set()
        results: dict[str, float] = {}
        
        # Get embedding from metadata if not provided
        if embedding is None and metadata and metadata.semantic_features:
            embedding = metadata.semantic_features.embedding_vector
        
        # Get features from metadata if not provided
        if features is None and metadata:
            features = self._build_feature_vector(metadata)
        
        # Search embedding index (Semantics layer)
        if embedding and len(self._embedding_index) > 0:
            embedding_results = self._embedding_index.search(
                embedding, k=k * 2, exclude=exclude
            )
            
            for song_id, score in embedding_results:
                results[song_id] = results.get(song_id, 0) + \
                    score * self.EMBEDDING_WEIGHT
        
        # Search feature index (Physics + Librarian layers)
        if features and len(self._feature_index) > 0:
            feature_results = self._feature_index.search(
                features, k=k * 2, exclude=exclude
            )
            
            for song_id, score in feature_results:
                results[song_id] = results.get(song_id, 0) + \
                    score * (self.AUDIO_WEIGHT + self.LIBRARIAN_WEIGHT)
        
        # Sort and return top k
        sorted_results = sorted(
            results.items(),
            key=lambda x: x[1],
            reverse=True
        )[:k]
        
        # Convert to SimilarityResult with metadata
        output = []
        for song_id, score in sorted_results:
            song_metadata = await self.cache.get_metadata(song_id)
            output.append(SimilarityResult(
                song_id=song_id,
                score=score,
                metadata=song_metadata
            ))
        
        return output
    
    async def find_similar_batch(
        self,
        song_ids: list[str],
        k: int = 10,
        exclude: Optional[set[str]] = None
    ) -> list[SimilarityResult]:
        """
        Find songs similar to a batch of songs.
        
        Averages the feature vectors of input songs to find
        songs similar to the overall "vibe" (audio characteristics).
        
        Args:
            song_ids: List of reference song IDs
            k: Number of results
            exclude: Song IDs to exclude
            
        Returns:
            List of similar songs
        """
        if not self.is_active or not song_ids:
            return []
        
        exclude = exclude or set()
        exclude.update(song_ids)  # Don't return query songs
        
        # Collect embeddings and features
        embeddings = []
        features = []
        
        for song_id in song_ids:
            metadata = await self.cache.get_metadata(song_id)
            if not metadata:
                continue
            
            if (
                metadata.semantic_features
                and metadata.semantic_features.embedding_vector
            ):
                embeddings.append(metadata.semantic_features.embedding_vector)
            
            feature_vec = self._build_feature_vector(metadata)
            if feature_vec:
                features.append(feature_vec)
        
        # Average vectors
        avg_embedding = None
        if embeddings:
            avg_embedding = np.mean(embeddings, axis=0).tolist()
        
        avg_features = None
        if features:
            avg_features = np.mean(features, axis=0).tolist()
        
        return await self.find_similar_to_features(
            embedding=avg_embedding,
            features=avg_features,
            k=k,
            exclude=exclude
        )
    
    def get_stats(self) -> dict[str, Any]:
        """Get index statistics."""
        return {
            "active": self._active,
            "song_count": self._song_count,
            "embedding_index_size": len(self._embedding_index),
            "feature_index_size": len(self._feature_index),
            "genre_vocabulary_size": len(self._genre_vocab),
            "mood_vocabulary_size": len(self._mood_vocab),
            "min_for_activation": self.MIN_SONGS_FOR_ACTIVATION
        }


# Singleton instance
_vector_searcher: Optional[VectorSearcher] = None


def get_vector_searcher() -> VectorSearcher:
    """Get global vector searcher instance."""
    global _vector_searcher
    if _vector_searcher is None:
        _vector_searcher = VectorSearcher()
    return _vector_searcher


# Backwards compatibility aliases (deprecated)
CollaborativeFilterer = VectorSearcher
get_collaborative_filterer = get_vector_searcher
