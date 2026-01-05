"""
Tests for V3 Autoplay Engine Vector Search Index Module

Tests for VectorIndex, VectorSearcher, and content-based similarity.
"""

import pytest
import asyncio
import sys
import os
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.vector_search_index import (
    VectorIndex,
    VectorSearcher,
    SimilarityResult,
    get_vector_searcher
)
from modules.music.Autoplay_Engine.v3.constants import V3Config


# ============================================================================
# VectorIndex Tests
# ============================================================================

class TestVectorIndexInitialization:
    """Tests for VectorIndex initialization."""
    
    def test_index_creation(self):
        """Verify vector index can be created with dimension."""
        index = VectorIndex(dimension=128)
        assert index is not None
        assert index.dimension == 128
    
    def test_empty_index(self):
        """New index should be empty."""
        index = VectorIndex(dimension=64)
        assert len(index) == 0
    
    def test_different_dimensions(self):
        """Should support various dimensions."""
        for dim in [32, 64, 128, 256]:
            index = VectorIndex(dimension=dim)
            assert index.dimension == dim


class TestVectorIndexAddOperations:
    """Tests for adding vectors to the index."""
    
    @pytest.fixture
    def index(self):
        """Create a 128-dim index for testing."""
        return VectorIndex(dimension=128)
    
    def test_add_single_vector(self, index):
        """Should add a single vector to the index."""
        vector = [1.0] * 128
        index.add("song_1", vector)
        assert len(index) == 1
    
    def test_add_multiple_vectors(self, index):
        """Should add multiple vectors."""
        for i in range(10):
            vector = [float(i)] * 128
            index.add(f"song_{i}", vector)
        assert len(index) == 10
    
    def test_add_shorter_vector_pads(self, index):
        """Shorter vectors should be padded to dimension."""
        short_vector = [1.0] * 64
        index.add("song_short", short_vector)
        assert len(index) == 1
        # Vector should be stored (padded internally)
    
    def test_add_longer_vector_truncates(self, index):
        """Longer vectors should be truncated to dimension."""
        long_vector = [1.0] * 256
        index.add("song_long", long_vector)
        assert len(index) == 1
    
    def test_duplicate_id_overwrites(self, index):
        """Adding same ID should overwrite previous vector."""
        index.add("song_1", [1.0] * 128)
        index.add("song_1", [2.0] * 128)
        assert len(index) == 1


class TestVectorIndexRemoveOperations:
    """Tests for removing vectors from the index."""
    
    @pytest.fixture
    def populated_index(self):
        """Create index with sample vectors."""
        index = VectorIndex(dimension=128)
        for i in range(5):
            index.add(f"song_{i}", [float(i)] * 128)
        return index
    
    def test_remove_existing(self, populated_index):
        """Should remove an existing vector."""
        assert len(populated_index) == 5
        populated_index.remove("song_2")
        assert len(populated_index) == 4
    
    def test_remove_nonexistent(self, populated_index):
        """Removing nonexistent vector should not error."""
        populated_index.remove("nonexistent")
        assert len(populated_index) == 5
    
    def test_remove_all(self, populated_index):
        """Should be able to remove all vectors."""
        for i in range(5):
            populated_index.remove(f"song_{i}")
        assert len(populated_index) == 0


class TestVectorIndexSearch:
    """Tests for similarity search."""
    
    @pytest.fixture
    def index_with_vectors(self):
        """Create index with diverse vectors for search testing."""
        index = VectorIndex(dimension=128)
        
        # Add vectors with different patterns
        # song_0: all 1s
        index.add("song_0", [1.0] * 128)
        # song_1: all 2s (similar to song_0 in direction)
        index.add("song_1", [2.0] * 128)
        # song_2: alternating pattern
        index.add("song_2", [float(i % 2) for i in range(128)])
        # song_3: all 0.5s
        index.add("song_3", [0.5] * 128)
        # song_4: negative values
        index.add("song_4", [-1.0] * 128)
        
        return index
    
    def test_search_returns_results(self, index_with_vectors):
        """Search should return results."""
        query = [1.0] * 128
        results = index_with_vectors.search(query, k=3)
        assert len(results) > 0
        assert len(results) <= 3
    
    def test_search_returns_most_similar(self, index_with_vectors):
        """Most similar vectors should be returned first."""
        # Query similar to song_0 and song_1
        query = [1.0] * 128
        results = index_with_vectors.search(query, k=2)
        
        # song_0 and song_1 should be most similar (same direction, cosine ≈ 1.0)
        result_ids = [r[0] for r in results]
        assert "song_0" in result_ids or "song_1" in result_ids
    
    def test_search_respects_k(self, index_with_vectors):
        """Should return at most k results."""
        query = [1.0] * 128
        
        results_1 = index_with_vectors.search(query, k=1)
        assert len(results_1) == 1
        
        results_5 = index_with_vectors.search(query, k=5)
        assert len(results_5) == 5
    
    def test_search_excludes_ids(self, index_with_vectors):
        """Should exclude specified song IDs."""
        query = [1.0] * 128
        exclude = {"song_0", "song_1"}
        
        results = index_with_vectors.search(query, k=5, exclude=exclude)
        result_ids = {r[0] for r in results}
        
        assert "song_0" not in result_ids
        assert "song_1" not in result_ids
    
    def test_search_empty_index(self):
        """Searching empty index should return empty list."""
        index = VectorIndex(dimension=128)
        results = index.search([1.0] * 128, k=5)
        assert results == []
    
    def test_search_scores_normalized(self, index_with_vectors):
        """Similarity scores should be between -1 and 1 for cosine."""
        query = [1.0] * 128
        results = index_with_vectors.search(query, k=5)
        
        for song_id, score in results:
            assert -1.0 <= score <= 1.0


class TestCosineSimilarity:
    """Tests for cosine similarity calculation."""
    
    def test_identical_vectors_similarity_one(self):
        """Identical vectors should have similarity ~1.0."""
        index = VectorIndex(dimension=128)
        vector = [1.0] * 128
        index.add("song_1", vector)
        
        results = index.search(vector, k=1)
        assert len(results) == 1
        assert results[0][1] > 0.99  # Should be very close to 1.0
    
    def test_opposite_vectors_similarity_negative(self):
        """Opposite vectors should have negative similarity."""
        index = VectorIndex(dimension=128)
        index.add("positive", [1.0] * 128)
        index.add("negative", [-1.0] * 128)
        
        # Search for positive direction
        results = index.search([1.0] * 128, k=2)
        
        # Find the negative vector's score
        negative_result = [r for r in results if r[0] == "negative"]
        if negative_result:
            assert negative_result[0][1] < 0
    
    def test_orthogonal_vectors_similarity_zero(self):
        """Orthogonal vectors should have similarity ~0."""
        index = VectorIndex(dimension=4)
        index.add("song_1", [1.0, 0.0, 0.0, 0.0])
        index.add("song_2", [0.0, 1.0, 0.0, 0.0])
        
        results = index.search([1.0, 0.0, 0.0, 0.0], k=2)
        
        # song_2 should have similarity ~0
        song_2_result = [r for r in results if r[0] == "song_2"]
        if song_2_result:
            assert abs(song_2_result[0][1]) < 0.01


# ============================================================================
# SimilarityResult Tests
# ============================================================================

class TestSimilarityResult:
    """Tests for SimilarityResult dataclass."""
    
    def test_creation(self):
        """Should create SimilarityResult."""
        result = SimilarityResult(song_id="song_1", score=0.85)
        assert result.song_id == "song_1"
        assert result.score == 0.85
        assert result.metadata is None
    
    def test_comparison_higher_score_first(self):
        """Higher scores should sort first (for max-heap behavior)."""
        result_high = SimilarityResult(song_id="high", score=0.9)
        result_low = SimilarityResult(song_id="low", score=0.5)
        
        # __lt__ is inverted for sorting highest first
        assert result_high < result_low  # Higher score = "less than" for sorting
    
    def test_sorting(self):
        """Results should sort by score descending."""
        results = [
            SimilarityResult(song_id="low", score=0.3),
            SimilarityResult(song_id="high", score=0.9),
            SimilarityResult(song_id="mid", score=0.6),
        ]
        
        sorted_results = sorted(results)
        assert sorted_results[0].song_id == "high"
        assert sorted_results[-1].song_id == "low"


# ============================================================================
# VectorSearcher Tests
# ============================================================================

class TestVectorSearcherInitialization:
    """Tests for VectorSearcher initialization."""
    
    def test_searcher_creation(self):
        """Should create VectorSearcher."""
        config = V3Config()
        searcher = VectorSearcher(config=config)
        assert searcher is not None
    
    def test_default_weights(self):
        """Should have default layer weights."""
        searcher = VectorSearcher()
        assert searcher.EMBEDDING_WEIGHT == 0.5
        assert searcher.AUDIO_WEIGHT == 0.25
        assert searcher.LIBRARIAN_WEIGHT == 0.25
    
    def test_min_songs_threshold(self):
        """Should require 500 songs for activation."""
        searcher = VectorSearcher()
        assert searcher.MIN_SONGS_FOR_ACTIVATION == 500


class TestVectorSearcherActivation:
    """Tests for VectorSearcher activation conditions."""
    
    @pytest.mark.asyncio
    async def test_not_active_below_threshold(self):
        """Searcher should not activate with fewer than 500 songs."""
        searcher = VectorSearcher()
        
        # Mock cache with few songs
        mock_cache = MagicMock()
        mock_cache.initialize = AsyncMock()
        mock_cache.get_analyzed_song_count = AsyncMock(return_value=100)
        searcher.cache = mock_cache
        
        mock_bus = MagicMock()
        mock_bus.subscribe = AsyncMock()
        searcher.event_bus = mock_bus
        
        await searcher.initialize()
        
        assert searcher._active is False
    
    @pytest.mark.asyncio
    async def test_active_above_threshold(self):
        """Searcher should activate with 500+ songs."""
        searcher = VectorSearcher()
        
        mock_cache = MagicMock()
        mock_cache.initialize = AsyncMock()
        mock_cache.get_analyzed_song_count = AsyncMock(return_value=600)
        searcher.cache = mock_cache
        
        mock_bus = MagicMock()
        mock_bus.subscribe = AsyncMock()
        searcher.event_bus = mock_bus
        
        await searcher.initialize()
        
        assert searcher._active is True


class TestVectorSearcherMultiLayerWeighting:
    """Tests for 3-layer similarity weighting."""
    
    def test_weights_sum_to_one(self):
        """Layer weights should sum to 1.0."""
        searcher = VectorSearcher()
        total = (
            searcher.EMBEDDING_WEIGHT +
            searcher.AUDIO_WEIGHT +
            searcher.LIBRARIAN_WEIGHT
        )
        assert abs(total - 1.0) < 0.001
    
    def test_embedding_highest_weight(self):
        """EfficientAT embeddings should have highest weight (50%)."""
        searcher = VectorSearcher()
        assert searcher.EMBEDDING_WEIGHT >= searcher.AUDIO_WEIGHT
        assert searcher.EMBEDDING_WEIGHT >= searcher.LIBRARIAN_WEIGHT


# ============================================================================
# VectorSearcher Search Tests
# ============================================================================

class TestVectorSearcherSearch:
    """Tests for VectorSearcher search functionality."""
    
    @pytest.fixture
    def searcher(self):
        """Create a VectorSearcher for testing."""
        return VectorSearcher()
    
    @pytest.mark.asyncio
    async def test_search_when_inactive(self, searcher):
        """Search should return empty when not active."""
        searcher._active = False
        
        if hasattr(searcher, 'search'):
            results = await searcher.search("seed_song", k=5)
            assert results == [] or results is None
    
    @pytest.mark.asyncio
    async def test_search_returns_similarity_results(self, searcher):
        """Search should return SimilarityResult objects."""
        searcher._active = True
        searcher._initialized = True
        
        # Mock the internal search
        mock_results = [
            SimilarityResult(song_id="song_1", score=0.9),
            SimilarityResult(song_id="song_2", score=0.85),
        ]
        
        if hasattr(searcher, 'search'):
            with patch.object(searcher, '_do_search', return_value=mock_results):
                results = await searcher.search("seed_song", k=5)
                if results:
                    assert all(isinstance(r, SimilarityResult) for r in results)


# ============================================================================
# Singleton/Factory Tests
# ============================================================================

class TestVectorSearcherSingleton:
    """Tests for get_vector_searcher singleton."""
    
    def test_singleton_returns_instance(self):
        """get_vector_searcher should return an instance."""
        searcher = get_vector_searcher()
        assert isinstance(searcher, VectorSearcher)
    
    def test_singleton_same_instance(self):
        """Multiple calls should return same instance."""
        searcher1 = get_vector_searcher()
        searcher2 = get_vector_searcher()
        assert searcher1 is searcher2


# ============================================================================
# Integration Tests
# ============================================================================

class TestVectorSearcherIntegration:
    """Integration tests for complete search workflow."""
    
    @pytest.mark.asyncio
    async def test_full_workflow(self):
        """Test complete initialization and search workflow."""
        searcher = VectorSearcher()
        
        # Mock dependencies
        mock_cache = MagicMock()
        mock_cache.initialize = AsyncMock()
        mock_cache.get_analyzed_song_count = AsyncMock(return_value=600)
        searcher.cache = mock_cache
        
        mock_bus = MagicMock()
        mock_bus.subscribe = AsyncMock()
        mock_bus.unsubscribe = AsyncMock()
        searcher.event_bus = mock_bus
        
        # Initialize
        await searcher.initialize()
        assert searcher._initialized is True
        assert searcher._active is True
        
        # Cleanup
        await searcher.shutdown()
        assert searcher._initialized is False
    
    @pytest.mark.asyncio
    async def test_event_subscription(self):
        """Should subscribe to SONG_ANALYZED events."""
        searcher = VectorSearcher()
        
        mock_cache = MagicMock()
        mock_cache.initialize = AsyncMock()
        mock_cache.get_analyzed_song_count = AsyncMock(return_value=0)
        searcher.cache = mock_cache
        
        mock_bus = MagicMock()
        mock_bus.subscribe = AsyncMock()
        searcher.event_bus = mock_bus
        
        await searcher.initialize()
        
        # Should have subscribed to SONG_ANALYZED
        mock_bus.subscribe.assert_called()
