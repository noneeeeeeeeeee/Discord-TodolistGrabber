"""
Tests for V3 Autoplay Engine Recommender Module

Tests for the head chef orchestrator with Cold/Warm/Hot state machine.
"""

import pytest
import asyncio
import sys
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.recommender import Recommender, get_recommender, Recommendation, RecommendationCandidate
from modules.music.Autoplay_Engine.v3.constants import (
    SessionState,
    RecommendationSource,
    V3Config
)
from modules.music.Autoplay_Engine.v3.mappings import SongIdentifier


def create_mock_cache():
    """Create a mock cache manager."""
    cache = MagicMock()
    cache.initialize = AsyncMock()
    cache.shutdown = AsyncMock()
    cache.get_metadata = AsyncMock(return_value=None)
    cache.set_metadata = AsyncMock()
    return cache


def create_mock_mappings():
    """Create a mock mappings manager."""
    mappings = MagicMock()
    mappings.initialize = AsyncMock()
    mappings.shutdown = AsyncMock()
    mappings.resolve_song = AsyncMock(return_value=None)
    return mappings


def create_mock_analyzer():
    """Create a mock song analyzer."""
    analyzer = MagicMock()
    analyzer.initialize = AsyncMock()
    analyzer.shutdown = AsyncMock()
    analyzer.analyze = AsyncMock(return_value=None)
    return analyzer


def create_mock_context():
    """Create a mock context analyzer."""
    context = MagicMock()
    context.initialize = AsyncMock()
    context.shutdown = AsyncMock()
    context.get_session_context = MagicMock(return_value={
        "session_id": "test",
        "state": "cold",
        "play_count": 0,
        "skip_rate": 0,
        "avg_energy": 0.5,
        "avg_bpm": 120
    })
    context.get_preference_vector = MagicMock(return_value={})
    return context


def create_mock_novelty():
    """Create a mock novelty controller."""
    novelty = MagicMock()
    novelty.initialize = AsyncMock()
    novelty.shutdown = AsyncMock()
    novelty.apply_novelty = AsyncMock(side_effect=lambda recs: recs)
    return novelty


def create_mock_vector_search():
    """Create a mock vector searcher."""
    vs = MagicMock()
    vs.initialize = AsyncMock()
    vs.shutdown = AsyncMock()
    vs.search_similar = AsyncMock(return_value=[])
    return vs


def create_mock_collaborative():
    """Create a mock collaborative recommender."""
    collab = MagicMock()
    collab.initialize = AsyncMock()
    collab.shutdown = AsyncMock()
    collab.get_recommendations = AsyncMock(return_value=[])
    return collab


def create_mock_gemini():
    """Create a mock Gemini manager."""
    gemini = MagicMock()
    gemini.initialize = AsyncMock()
    gemini.shutdown = AsyncMock()
    gemini.get_recommendations_prompt = AsyncMock(return_value=MagicMock(success=False))
    return gemini


def create_mock_event_bus():
    """Create a mock event bus."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


def create_test_recommender():
    """Create a recommender with all mocked dependencies."""
    config = V3Config()
    return Recommender(
        config=config,
        cache=create_mock_cache(),
        mappings=create_mock_mappings(),
        analyzer=create_mock_analyzer(),
        context=create_mock_context(),
        novelty=create_mock_novelty(),
        vector_search=create_mock_vector_search(),
        collaborative=create_mock_collaborative(),
        gemini=create_mock_gemini(),
        event_bus=create_mock_event_bus()
    )


class TestRecommenderInitialization:
    """Tests for Recommender initialization."""
    
    def test_recommender_creation(self):
        """Verify recommender can be created with mocked deps."""
        recommender = create_test_recommender()
        assert recommender is not None
        assert recommender._initialized is False
    
    @pytest.mark.asyncio
    async def test_initialize_success(self):
        """Recommender should initialize all dependencies."""
        recommender = create_test_recommender()
        
        await recommender.initialize()
        
        assert recommender._initialized is True
        recommender.cache.initialize.assert_awaited_once()
        recommender.mappings.initialize.assert_awaited_once()
        recommender.analyzer.initialize.assert_awaited_once()
        recommender.context.initialize.assert_awaited_once()
        recommender.novelty.initialize.assert_awaited_once()
        recommender.vector_search.initialize.assert_awaited_once()
        recommender.collaborative.initialize.assert_awaited_once()
        recommender.gemini.initialize.assert_awaited_once()
    
    @pytest.mark.asyncio
    async def test_initialize_idempotent(self):
        """Initialize should be idempotent."""
        recommender = create_test_recommender()
        
        await recommender.initialize()
        await recommender.initialize()
        
        # Each dependency should only be initialized once
        recommender.cache.initialize.assert_awaited_once()
    
    @pytest.mark.asyncio
    async def test_shutdown(self):
        """Shutdown should shutdown all dependencies."""
        recommender = create_test_recommender()
        
        await recommender.initialize()
        await recommender.shutdown()
        
        assert recommender._initialized is False
        recommender.cache.shutdown.assert_awaited()


class TestStrategyWeights:
    """Tests for strategy weight configuration."""
    
    def test_cold_state_weights_exist(self):
        """COLD state should have strategy weights defined."""
        assert SessionState.COLD in Recommender.STRATEGY_WEIGHTS
        weights = Recommender.STRATEGY_WEIGHTS[SessionState.COLD]
        assert "lastfm" in weights
    
    def test_warm_state_weights_exist(self):
        """WARM state should have strategy weights defined."""
        assert SessionState.WARM in Recommender.STRATEGY_WEIGHTS
        weights = Recommender.STRATEGY_WEIGHTS[SessionState.WARM]
        assert "cf" in weights  # cf = collaborative filtering
    
    def test_hot_state_weights_exist(self):
        """HOT state should have strategy weights defined."""
        assert SessionState.HOT in Recommender.STRATEGY_WEIGHTS
        weights = Recommender.STRATEGY_WEIGHTS[SessionState.HOT]
        assert "vector_search" in weights
    
    def test_extended_state_weights_exist(self):
        """EXTENDED state should have strategy weights defined."""
        assert SessionState.EXTENDED in Recommender.STRATEGY_WEIGHTS
        weights = Recommender.STRATEGY_WEIGHTS[SessionState.EXTENDED]
        assert "daydreamer" in weights
    
    def test_cold_relies_mostly_on_lastfm(self):
        """COLD state should weight Last.fm heavily."""
        weights = Recommender.STRATEGY_WEIGHTS[SessionState.COLD]
        assert weights["lastfm"] >= 0.4
    
    def test_hot_uses_vector_search(self):
        """HOT state should weight vector search."""
        weights = Recommender.STRATEGY_WEIGHTS[SessionState.HOT]
        assert weights["vector_search"] >= 0.2


class TestDaydreamerIntegration:
    """Tests for Daydreamer integration."""
    
    def test_set_daydreamer(self):
        """set_daydreamer should store reference."""
        recommender = create_test_recommender()
        
        mock_daydreamer = MagicMock()
        recommender.set_daydreamer(mock_daydreamer)
        
        assert recommender._daydreamer is mock_daydreamer
    
    def test_daydreamer_starts_none(self):
        """Daydreamer should start as None."""
        recommender = create_test_recommender()
        assert recommender._daydreamer is None


class TestStatistics:
    """Tests for recommendation statistics."""
    
    def test_stats_initialized(self):
        """Recommender should initialize stats dict."""
        recommender = create_test_recommender()
        
        assert hasattr(recommender, '_stats')
        assert "recommendations" in recommender._stats
        assert "by_source" in recommender._stats
        assert "by_state" in recommender._stats


class TestGetRecommendationMethod:
    """Tests for get_recommendation method existence."""
    
    def test_get_recommendation_exists(self):
        """get_recommendation method should exist."""
        recommender = create_test_recommender()
        assert hasattr(recommender, 'get_recommendation')
        assert asyncio.iscoroutinefunction(recommender.get_recommendation)
    
    def test_get_recommendations_exists(self):
        """get_recommendations (plural) method should exist."""
        recommender = create_test_recommender()
        if hasattr(recommender, 'get_recommendations'):
            assert asyncio.iscoroutinefunction(recommender.get_recommendations)


class TestConstants:
    """Tests for recommender constants."""
    
    def test_strategy_weights_type(self):
        """STRATEGY_WEIGHTS should be a dict."""
        assert isinstance(Recommender.STRATEGY_WEIGHTS, dict)
    
    def test_all_session_states_covered(self):
        """All session states should have weights."""
        for state in [SessionState.COLD, SessionState.WARM, SessionState.HOT, SessionState.EXTENDED]:
            assert state in Recommender.STRATEGY_WEIGHTS


class TestDependencyStorage:
    """Tests for dependency storage."""
    
    def test_stores_config(self):
        """Should store config."""
        recommender = create_test_recommender()
        assert recommender.config is not None
    
    def test_stores_cache(self):
        """Should store cache reference."""
        recommender = create_test_recommender()
        assert recommender.cache is not None
    
    def test_stores_mappings(self):
        """Should store mappings reference."""
        recommender = create_test_recommender()
        assert recommender.mappings is not None
    
    def test_stores_analyzer(self):
        """Should store analyzer reference."""
        recommender = create_test_recommender()
        assert recommender.analyzer is not None
    
    def test_stores_context(self):
        """Should store context reference."""
        recommender = create_test_recommender()
        assert recommender.context is not None
    
    def test_stores_novelty(self):
        """Should store novelty reference."""
        recommender = create_test_recommender()
        assert recommender.novelty is not None
    
    def test_stores_vector_search(self):
        """Should store vector_search reference."""
        recommender = create_test_recommender()
        assert recommender.vector_search is not None
    
    def test_stores_collaborative(self):
        """Should store collaborative reference."""
        recommender = create_test_recommender()
        assert recommender.collaborative is not None
    
    def test_stores_gemini(self):
        """Should store gemini reference."""
        recommender = create_test_recommender()
        assert recommender.gemini is not None
    
    def test_stores_event_bus(self):
        """Should store event_bus reference."""
        recommender = create_test_recommender()
        assert recommender.event_bus is not None


class TestGlobalSingleton:
    """Tests for singleton getter."""
    
    def test_get_recommender_returns_instance(self):
        """get_recommender should return instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.recommender as rec_mod
        rec_mod._recommender = None
        
        recommender = get_recommender()
        
        assert recommender is not None
        assert isinstance(recommender, Recommender)
    
    def test_get_recommender_returns_same_instance(self):
        """get_recommender should return same instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.recommender as rec_mod
        rec_mod._recommender = None
        
        recommender1 = get_recommender()
        recommender2 = get_recommender()
        
        assert recommender1 is recommender2
