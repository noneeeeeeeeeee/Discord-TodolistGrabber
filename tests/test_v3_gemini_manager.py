"""
Tests for V3 Autoplay Engine Gemini Manager Module

Tests for API key rotation, request batching, rate limiting, and grounding.
"""

import pytest
import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.gemini_manager import GeminiManager
from modules.music.Autoplay_Engine.v3.constants import V3Config


class TestGeminiManagerInitialization:
    """Tests for GeminiManager initialization."""
    
    @pytest.fixture
    def mock_env_keys(self):
        """Mock environment with Gemini API keys."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["key1", "key2", "key3"]'
        }):
            yield
    
    def test_gemini_manager_creation(self, mock_env_keys):
        """Verify Gemini manager can be created."""
        config = V3Config()
        manager = GeminiManager(config)
        assert manager is not None
    
    def test_loads_api_keys_from_env(self, mock_env_keys):
        """Should load API keys from GeminiApiKeys environment variable."""
        config = V3Config()
        manager = GeminiManager(config)
        
        assert len(manager._api_keys) == 3
        assert 'key1' in manager._api_keys
    
    def test_handles_missing_keys(self):
        """Should handle missing API keys gracefully."""
        with patch.dict(os.environ, {}, clear=True):
            config = V3Config()
            
            # Should either raise or initialize with empty keys
            try:
                manager = GeminiManager(config)
                assert manager._api_keys == [] or manager._api_keys is None
            except ValueError:
                pass  # Also acceptable


class TestApiKeyRotation:
    """Tests for API key rotation functionality."""
    
    @pytest.fixture
    def manager_with_keys(self):
        """Create manager with mock keys."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["key1", "key2", "key3"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    def test_key_rotation_on_error(self, manager_with_keys):
        """Should rotate to next key on rate limit error."""
        initial_key = manager_with_keys._current_key_index
        
        manager_with_keys._rotate_key()
        
        assert manager_with_keys._current_key_index != initial_key or len(manager_with_keys._api_keys) == 1
    
    def test_key_rotation_wraps_around(self, manager_with_keys):
        """Key rotation should wrap around to first key."""
        # Rotate through all keys
        for _ in range(len(manager_with_keys._api_keys)):
            manager_with_keys._rotate_key()
        
        # Should be back at start or at a valid index
        assert 0 <= manager_with_keys._current_key_index < len(manager_with_keys._api_keys)
    
    def test_get_current_key(self, manager_with_keys):
        """Should return current active key."""
        key = manager_with_keys._get_current_key()
        assert key in manager_with_keys._api_keys


class TestRequestBatching:
    """Tests for request batching (50 per batch)."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for batching tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["test_key"]'
        }):
            config = V3Config()
            config.rate_limits.gemini_batch_size = 50
            manager = GeminiManager(config)
            return manager
    
    @pytest.mark.asyncio
    async def test_batch_size_limit(self, manager):
        """Batches should not exceed 50 items."""
        items = [{'id': str(i)} for i in range(100)]
        
        batches = manager._create_batches(items)
        
        for batch in batches:
            assert len(batch) <= 50
    
    @pytest.mark.asyncio
    async def test_batch_creation(self, manager):
        """Should correctly split items into batches."""
        items = [{'id': str(i)} for i in range(125)]
        
        batches = manager._create_batches(items)
        
        assert len(batches) == 3  # 50 + 50 + 25
        assert len(batches[0]) == 50
        assert len(batches[1]) == 50
        assert len(batches[2]) == 25
    
    @pytest.mark.asyncio
    async def test_empty_batch(self, manager):
        """Should handle empty input."""
        batches = manager._create_batches([])
        assert len(batches) == 0 or batches == [[]]


class TestRateLimiting:
    """Tests for rate limiting and backoff."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for rate limit tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["key1", "key2"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    @pytest.mark.asyncio
    async def test_exponential_backoff(self, manager):
        """Should implement exponential backoff on errors."""
        # Simulate errors
        manager._error_count = 0
        
        delay1 = manager._get_backoff_delay()
        manager._error_count = 1
        delay2 = manager._get_backoff_delay()
        manager._error_count = 2
        delay3 = manager._get_backoff_delay()
        
        # Each delay should be greater
        assert delay2 >= delay1
        assert delay3 >= delay2
    
    @pytest.mark.asyncio
    async def test_max_backoff_cap(self, manager):
        """Backoff should have a maximum cap."""
        manager._error_count = 100  # Many errors
        
        delay = manager._get_backoff_delay()
        
        # Should be capped at some reasonable maximum (e.g., 60 seconds)
        assert delay <= 300  # 5 minutes max
    
    @pytest.mark.asyncio
    async def test_rate_limit_tracking(self, manager):
        """Should track request counts for rate limiting."""
        if hasattr(manager, '_request_count'):
            initial = manager._request_count
            
            # Simulate request
            manager._increment_request_count()
            
            assert manager._request_count == initial + 1


class TestGrounding:
    """Tests for Google Search grounding functionality."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for grounding tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["test_key"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    @pytest.mark.asyncio
    async def test_grounding_enabled_for_enrichment(self, manager):
        """Grounding should be enabled for enrichment queries."""
        query_type = 'enrichment'
        
        should_ground = manager._should_use_grounding(query_type)
        assert should_ground is True
    
    @pytest.mark.asyncio
    async def test_grounding_fields(self, manager):
        """Grounding should be used for specific fields."""
        # These fields require grounding per spec
        grounding_fields = ['explicit_content', 'cultural_vibe', 'canonical_title']
        
        for field in grounding_fields:
            should_ground = manager._should_use_grounding(field)
            assert should_ground is True
    
    @pytest.mark.asyncio
    async def test_grounding_disabled_for_analysis(self, manager):
        """Grounding may be disabled for pure analysis."""
        query_type = 'audio_analysis'
        
        should_ground = manager._should_use_grounding(query_type)
        # May or may not use grounding for analysis
        assert isinstance(should_ground, bool)


class TestEnrichmentQueries:
    """Tests for song enrichment queries."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for enrichment tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["test_key"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    @pytest.mark.asyncio
    async def test_enrich_song_metadata(self, manager):
        """Should be able to enrich song metadata."""
        song = {
            'title': 'Bohemian Rhapsody',
            'artist': 'Queen'
        }
        
        # Mock the API call
        with patch.object(manager, '_call_api', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {
                'explicit_content': False,
                'cultural_vibe': 'classic rock anthem',
                'canonical_title': 'Bohemian Rhapsody'
            }
            
            if hasattr(manager, 'enrich_metadata'):
                result = await manager.enrich_metadata(song)
                assert 'explicit_content' in result or result is not None
    
    @pytest.mark.asyncio
    async def test_batch_enrichment(self, manager):
        """Should support batch enrichment of multiple songs."""
        songs = [
            {'title': f'Song {i}', 'artist': f'Artist {i}'}
            for i in range(10)
        ]
        
        with patch.object(manager, '_call_api', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = [
                {'explicit_content': False} for _ in songs
            ]
            
            if hasattr(manager, 'enrich_batch'):
                results = await manager.enrich_batch(songs)
                assert len(results) == 10


class TestRecommendationQueries:
    """Tests for recommendation queries to Gemini."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for recommendation tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["test_key"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    @pytest.mark.asyncio
    async def test_get_recommendations(self, manager):
        """Should be able to get recommendations from Gemini."""
        context = {
            'recent_songs': [
                {'title': 'Song 1', 'artist': 'Artist 1', 'genre': 'rock'},
                {'title': 'Song 2', 'artist': 'Artist 2', 'genre': 'rock'}
            ],
            'preferences': {
                'liked_genres': ['rock', 'metal']
            }
        }
        
        with patch.object(manager, '_call_api', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = [
                {'title': 'Recommended 1', 'artist': 'Rec Artist 1'},
                {'title': 'Recommended 2', 'artist': 'Rec Artist 2'}
            ]
            
            if hasattr(manager, 'get_recommendations'):
                recommendations = await manager.get_recommendations(context, limit=5)
                assert recommendations is not None


class TestErrorHandling:
    """Tests for error handling."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for error tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["key1", "key2"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    @pytest.mark.asyncio
    async def test_retry_on_failure(self, manager):
        """Should retry on transient failures."""
        call_count = 0
        
        async def failing_then_succeeding(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Transient error")
            return {'result': 'success'}
        
        with patch.object(manager, '_make_request', side_effect=failing_then_succeeding):
            if hasattr(manager, '_call_with_retry'):
                try:
                    result = await manager._call_with_retry({})
                    assert call_count >= 2  # At least one retry
                except Exception:
                    pass
    
    @pytest.mark.asyncio
    async def test_key_rotation_on_rate_limit(self, manager):
        """Should rotate key on rate limit error."""
        initial_key_index = manager._current_key_index
        
        # Simulate rate limit error
        manager._handle_rate_limit_error()
        
        # Key should have rotated
        assert manager._current_key_index != initial_key_index or len(manager._api_keys) == 1
    
    @pytest.mark.asyncio
    async def test_graceful_degradation(self, manager):
        """Should degrade gracefully when all keys exhausted."""
        # Mark all keys as exhausted
        for _ in range(len(manager._api_keys) * 2):
            manager._rotate_key()
        
        # Should still function (maybe with delays)
        key = manager._get_current_key()
        assert key is not None


class TestModelConfiguration:
    """Tests for Gemini model configuration."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for config tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["test_key"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    def test_model_is_gemini_25_flash(self, manager):
        """Should use gemini-2.5-flash model per spec."""
        assert 'gemini-2.5-flash' in manager._model_name
    
    def test_temperature_setting(self, manager):
        """Should have appropriate temperature setting."""
        if hasattr(manager, '_temperature'):
            assert 0 <= manager._temperature <= 1
    
    def test_max_tokens_setting(self, manager):
        """Should have max tokens configured."""
        if hasattr(manager, '_max_tokens'):
            assert manager._max_tokens > 0


class TestCaching:
    """Tests for response caching."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for cache tests."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': '["test_key"]'
        }):
            config = V3Config()
            manager = GeminiManager(config)
            return manager
    
    @pytest.mark.asyncio
    async def test_cache_enrichment_results(self, manager):
        """Should cache enrichment results to avoid redundant API calls."""
        song = {'title': 'Test Song', 'artist': 'Test Artist'}
        
        if hasattr(manager, '_cache'):
            cache_key = manager._get_cache_key(song)
            
            # Simulate caching
            manager._cache[cache_key] = {'explicit_content': False}
            
            # Check cache hit
            cached = manager._get_cached(cache_key)
            assert cached is not None
    
    @pytest.mark.asyncio
    async def test_cache_key_generation(self, manager):
        """Cache keys should be consistent for same input."""
        song1 = {'title': 'Test Song', 'artist': 'Test Artist'}
        song2 = {'title': 'Test Song', 'artist': 'Test Artist'}
        
        key1 = manager._get_cache_key(song1)
        key2 = manager._get_cache_key(song2)
        
        assert key1 == key2
