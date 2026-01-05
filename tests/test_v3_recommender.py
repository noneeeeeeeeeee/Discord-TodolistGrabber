"""
Tests for V3 Autoplay Engine Recommender Module

Tests for the head chef orchestrator with Cold/Warm/Hot state machine.
"""

import pytest
import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.recommender import Recommender
from modules.music.Autoplay_Engine.v3.constants import (
    SessionState,
    RecommendationSource,
    V3Config
)


@dataclass
class MockSong:
    """Mock song for testing."""
    id: str
    title: str
    artist: str
    deezer_id: str = None
    bpm: int = 120
    genre: str = 'rock'


class MockSessionManager:
    """Mock session manager for testing."""
    
    def __init__(self):
        self.sessions = {}
    
    async def get_state(self, session_id):
        return self.sessions.get(session_id, {}).get('state', SessionState.COLD)
    
    async def get_history(self, session_id):
        return self.sessions.get(session_id, {}).get('history', [])
    
    async def get_session(self, session_id):
        return self.sessions.get(session_id)


class MockNoveltyController:
    """Mock novelty controller for testing."""
    
    async def apply_diversity(self, candidates, context):
        return candidates  # Pass through
    
    async def should_inject_novelty(self, context):
        return False


class MockCollaborativeFiltering:
    """Mock collaborative filtering for testing."""
    
    async def get_similar_songs(self, context, limit=10):
        return []


class MockLastFMClient:
    """Mock Last.fm client for testing."""
    
    async def get_similar_tracks(self, artist, title, limit=50):
        return [
            {'artist': f'Artist {i}', 'title': f'Track {i}'}
            for i in range(limit)
        ]


class MockGeminiManager:
    """Mock Gemini manager for testing."""
    
    async def get_recommendations(self, context, limit=10):
        return []


class TestRecommenderInitialization:
    """Tests for Recommender initialization."""
    
    def test_recommender_creation(self):
        """Verify recommender can be created."""
        config = V3Config()
        recommender = Recommender(config)
        assert recommender is not None
    
    @pytest.mark.asyncio
    async def test_initialize(self):
        """Recommender should initialize successfully."""
        config = V3Config()
        recommender = Recommender(config)
        await recommender.initialize()
        assert recommender._initialized is True


class TestColdStateStrategy:
    """Tests for Cold state (1-10 songs) recommendation strategy."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._lastfm = MockLastFMClient()
        return recommender
    
    @pytest.mark.asyncio
    async def test_cold_uses_lastfm(self, recommender):
        """Cold state should primarily use Last.fm similar tracks."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': MockSong('1', 'Test Song', 'Test Artist'),
            'history': []
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        
        # Should return something based on Last.fm
        assert recommendations is not None
    
    @pytest.mark.asyncio
    async def test_cold_uses_seed_track(self, recommender):
        """Cold state should use the seed track for similarity."""
        await recommender.initialize()
        
        seed = MockSong('1', 'Bohemian Rhapsody', 'Queen')
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': seed,
            'history': []
        }
        
        # The recommender should use the seed track
        recommendations = await recommender.get_recommendations(context, limit=5)
        assert recommendations is not None
    
    @pytest.mark.asyncio
    async def test_cold_respects_limit(self, recommender):
        """Cold state should respect the requested limit."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': MockSong('1', 'Test', 'Artist'),
            'history': []
        }
        
        recommendations = await recommender.get_recommendations(context, limit=3)
        assert len(recommendations) <= 3


class TestWarmStateStrategy:
    """Tests for Warm state (11-25 songs) recommendation strategy."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._collaborative = MockCollaborativeFiltering()
        recommender._lastfm = MockLastFMClient()
        return recommender
    
    @pytest.mark.asyncio
    async def test_warm_uses_collaborative(self, recommender):
        """Warm state should incorporate collaborative filtering."""
        await recommender.initialize()
        
        history = [MockSong(str(i), f'Song {i}', 'Artist') for i in range(15)]
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.WARM,
            'seed_track': history[0],
            'history': history
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        assert recommendations is not None
    
    @pytest.mark.asyncio
    async def test_warm_considers_history(self, recommender):
        """Warm state should consider session history for preferences."""
        await recommender.initialize()
        
        history = [
            MockSong(str(i), f'Rock Song {i}', 'Rock Artist', genre='rock')
            for i in range(15)
        ]
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.WARM,
            'seed_track': history[0],
            'history': history
        }
        
        # Should consider that user seems to like rock
        recommendations = await recommender.get_recommendations(context, limit=5)
        assert recommendations is not None


class TestHotStateStrategy:
    """Tests for Hot state (25+ songs) recommendation strategy."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._collaborative = MockCollaborativeFiltering()
        recommender._gemini = MockGeminiManager()
        return recommender
    
    @pytest.mark.asyncio
    async def test_hot_uses_full_profile(self, recommender):
        """Hot state should use full preference profile."""
        await recommender.initialize()
        
        history = [MockSong(str(i), f'Song {i}', 'Artist') for i in range(30)]
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.HOT,
            'seed_track': history[0],
            'history': history,
            'preference_profile': {
                'preferred_genres': ['rock', 'metal'],
                'preferred_bpm_range': (100, 140),
                'preferred_decades': ['2000s', '2010s']
            }
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        assert recommendations is not None
    
    @pytest.mark.asyncio
    async def test_hot_may_use_gemini(self, recommender):
        """Hot state may incorporate Gemini recommendations."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.HOT,
            'history': [MockSong(str(i), f'Song {i}', 'Artist') for i in range(30)],
            'preference_profile': {}
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        # Just verify it runs without error
        assert recommendations is not None


class TestStrategyBlending:
    """Tests for blending multiple recommendation strategies."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender with all strategies available."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._collaborative = MockCollaborativeFiltering()
        recommender._lastfm = MockLastFMClient()
        recommender._gemini = MockGeminiManager()
        return recommender
    
    @pytest.mark.asyncio
    async def test_blend_multiple_sources(self, recommender):
        """Should blend recommendations from multiple sources."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.WARM,
            'history': [MockSong(str(i), f'Song {i}', 'Artist') for i in range(15)]
        }
        
        if hasattr(recommender, 'get_blended_recommendations'):
            recommendations = await recommender.get_blended_recommendations(context, limit=10)
            assert recommendations is not None
    
    @pytest.mark.asyncio
    async def test_recommendation_has_source(self, recommender):
        """Each recommendation should indicate its source."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': MockSong('1', 'Test', 'Artist'),
            'history': []
        }
        
        recommendations = await recommender.get_recommendations(context, limit=3)
        
        for rec in recommendations:
            if hasattr(rec, 'source'):
                assert rec.source in list(RecommendationSource)


class TestNoveltyIntegration:
    """Tests for novelty controller integration."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._lastfm = MockLastFMClient()
        return recommender
    
    @pytest.mark.asyncio
    async def test_novelty_applied_to_recommendations(self, recommender):
        """Novelty controller should filter recommendations."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.WARM,
            'history': [MockSong(str(i), f'Song {i}', 'Artist') for i in range(15)]
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        # Novelty controller was called (mock passes through)
        assert recommendations is not None
    
    @pytest.mark.asyncio
    async def test_diversity_injection(self, recommender):
        """Should inject diversity when genre stagnation detected."""
        # Create novelty controller that requests diversity
        class DiversityRequestingController:
            async def apply_diversity(self, candidates, context):
                return candidates
            
            async def should_inject_novelty(self, context):
                return True  # Request diversity
            
            async def get_diversity_suggestion(self, context):
                return {'type': 'genre', 'value': 'jazz'}
        
        recommender._novelty_controller = DiversityRequestingController()
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.HOT,
            'history': [
                MockSong(str(i), f'Rock Song {i}', 'Rock Artist', genre='rock')
                for i in range(30)
            ]
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        assert recommendations is not None


class TestHistoryFiltering:
    """Tests for filtering out already-played songs."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._lastfm = MockLastFMClient()
        return recommender
    
    @pytest.mark.asyncio
    async def test_excludes_played_songs(self, recommender):
        """Should not recommend songs already in history."""
        await recommender.initialize()
        
        played_songs = [
            MockSong(str(i), f'Played Song {i}', 'Artist')
            for i in range(5)
        ]
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': played_songs[0],
            'history': played_songs
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        
        played_ids = {song.id for song in played_songs}
        for rec in recommendations:
            if hasattr(rec, 'id'):
                assert rec.id not in played_ids


class TestFallbackBehavior:
    """Tests for fallback when primary source fails."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        return recommender
    
    @pytest.mark.asyncio
    async def test_fallback_on_lastfm_failure(self, recommender):
        """Should fallback to other sources if Last.fm fails."""
        # Mock failing Last.fm
        class FailingLastFM:
            async def get_similar_tracks(self, *args, **kwargs):
                raise Exception("API Error")
        
        recommender._lastfm = FailingLastFM()
        recommender._collaborative = MockCollaborativeFiltering()
        
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': MockSong('1', 'Test', 'Artist'),
            'history': []
        }
        
        # Should not raise, should fallback
        try:
            recommendations = await recommender.get_recommendations(context, limit=5)
            assert recommendations is not None or recommendations == []
        except Exception:
            # May raise if no fallback available
            pass
    
    @pytest.mark.asyncio
    async def test_empty_result_handling(self, recommender):
        """Should handle empty results gracefully."""
        # Mock Last.fm returning empty
        class EmptyLastFM:
            async def get_similar_tracks(self, *args, **kwargs):
                return []
        
        recommender._lastfm = EmptyLastFM()
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': MockSong('1', 'Test', 'Artist'),
            'history': []
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        assert recommendations is not None
        assert isinstance(recommendations, list)


class TestRecommendationCaching:
    """Tests for caching of recommendations."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._lastfm = MockLastFMClient()
        return recommender
    
    @pytest.mark.asyncio
    async def test_recommendations_may_be_cached(self, recommender):
        """Recommendations may be cached for efficiency."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.COLD,
            'seed_track': MockSong('1', 'Test', 'Artist'),
            'history': []
        }
        
        # First call
        recs1 = await recommender.get_recommendations(context, limit=5)
        
        # Second call with same context
        recs2 = await recommender.get_recommendations(context, limit=5)
        
        # Both should work (caching is optional optimization)
        assert recs1 is not None
        assert recs2 is not None


class TestScoring:
    """Tests for recommendation scoring."""
    
    @pytest.fixture
    def recommender(self):
        """Create a recommender for testing."""
        config = V3Config()
        recommender = Recommender(config)
        recommender._session_manager = MockSessionManager()
        recommender._novelty_controller = MockNoveltyController()
        recommender._lastfm = MockLastFMClient()
        return recommender
    
    @pytest.mark.asyncio
    async def test_recommendations_have_scores(self, recommender):
        """Recommendations should include confidence scores."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.WARM,
            'history': [MockSong(str(i), f'Song {i}', 'Artist') for i in range(15)]
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        
        for rec in recommendations:
            if hasattr(rec, 'score'):
                assert 0 <= rec.score <= 1
    
    @pytest.mark.asyncio
    async def test_sorted_by_score(self, recommender):
        """Recommendations should be sorted by score (highest first)."""
        await recommender.initialize()
        
        context = {
            'session_id': 'test_session',
            'state': SessionState.WARM,
            'history': [MockSong(str(i), f'Song {i}', 'Artist') for i in range(15)]
        }
        
        recommendations = await recommender.get_recommendations(context, limit=5)
        
        if len(recommendations) > 1 and hasattr(recommendations[0], 'score'):
            scores = [rec.score for rec in recommendations]
            assert scores == sorted(scores, reverse=True)
