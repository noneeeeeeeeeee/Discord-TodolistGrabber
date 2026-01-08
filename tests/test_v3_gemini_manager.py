"""
Tests for V3 Autoplay Engine Gemini Manager Module

Tests for API key rotation, request batching, rate limiting, and grounding.
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

from modules.music.Autoplay_Engine.v3.gemini_manager import (
    GeminiManager,
    GeminiResponse,
    GroundingPriority,
    get_gemini_manager
)
from modules.music.Autoplay_Engine.v3.constants import V3Config


def create_mock_event_bus():
    """Create a mock event bus."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


class TestGeminiManagerInitialization:
    """Tests for GeminiManager initialization."""
    
    def test_gemini_manager_creation_without_keys(self):
        """Verify Gemini manager can be created even without API keys."""
        with patch.dict(os.environ, {}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            assert manager is not None
            assert manager._initialized is False
    
    def test_loads_api_keys_from_comma_separated_env(self):
        """Should load API keys from comma-separated GeminiApiKeys."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': 'key1, key2, key3'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert len(manager._api_keys) == 3
            assert 'key1' in manager._api_keys
            assert 'key2' in manager._api_keys
            assert 'key3' in manager._api_keys
    
    def test_loads_single_key_from_fallback_env(self):
        """Should load single key from GEMINI_API_KEY fallback."""
        with patch.dict(os.environ, {
            'GEMINI_API_KEY': 'single_key'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert len(manager._api_keys) == 1
            assert 'single_key' in manager._api_keys
    
    def test_handles_missing_keys(self):
        """Should handle missing API keys gracefully at creation."""
        with patch.dict(os.environ, {}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert manager._api_keys == []
    
    @pytest.mark.asyncio
    async def test_initialize_fails_without_keys(self):
        """Initialize should fail if no API keys available."""
        with patch.dict(os.environ, {}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            with pytest.raises(RuntimeError, match="No Gemini API keys available"):
                await manager.initialize()
    
    @pytest.mark.asyncio
    async def test_initialize_success_with_keys(self):
        """Initialize should succeed with API keys."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            await manager.initialize()
            
            assert manager._initialized is True
            assert manager._session is not None
            
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_shutdown_closes_session(self):
        """Shutdown should close HTTP session."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            await manager.initialize()
            await manager.shutdown()
            
            assert manager._initialized is False
            assert manager._session is None


class TestApiKeyRotation:
    """Tests for API key rotation functionality."""
    
    def test_get_next_key_returns_valid_key(self):
        """_get_next_key should return a valid key and index."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': 'key1, key2, key3'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            key, idx = manager._get_next_key()
            
            assert key in manager._api_keys
            assert 0 <= idx < len(manager._api_keys)
    
    def test_get_next_key_rotates(self):
        """_get_next_key should rotate through keys."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': 'key1, key2, key3'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            keys_seen = set()
            for _ in range(6):  # Call twice per key
                key, _ = manager._get_next_key()
                keys_seen.add(key)
            
            # Should have seen all 3 keys
            assert len(keys_seen) == 3
    
    def test_mark_key_failure_increases_count(self):
        """_mark_key_failure should increase failure count."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': 'key1, key2, key3'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            manager._mark_key_failure(0, retry_after=60)
            
            assert manager._key_failures.get(0, 0) == 1
    
    def test_mark_key_failure_sets_cooldown(self):
        """_mark_key_failure should set cooldown time."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': 'key1, key2'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            manager._mark_key_failure(0, retry_after=60)
            
            assert 0 in manager._key_cooldowns
            assert manager._key_cooldowns[0] > time.time()
    
    def test_mark_key_success_resets_failure_count(self):
        """_mark_key_success should reset failure count."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': 'key1'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            manager._key_failures[0] = 3
            manager._mark_key_success(0)
            
            assert manager._key_failures.get(0, 0) == 0
    
    def test_get_next_key_skips_cooled_down_keys(self):
        """_get_next_key should skip keys in cooldown."""
        with patch.dict(os.environ, {
            'GeminiApiKeys': 'key1, key2'
        }, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            # Put key at index 0 in cooldown
            manager._key_cooldowns[0] = time.time() + 60
            
            key, idx = manager._get_next_key()
            
            # Should skip to key2
            assert key == 'key2'
            assert idx == 1


class TestRateLimiting:
    """Tests for rate limiting."""
    
    def test_request_times_list_exists(self):
        """Manager should have request_times list for rate limiting."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert hasattr(manager, '_request_times')
            assert isinstance(manager._request_times, list)
    
    def test_max_requests_per_minute_set(self):
        """Should have MAX_REQUESTS_PER_MINUTE constant."""
        assert hasattr(GeminiManager, 'MAX_REQUESTS_PER_MINUTE')
        assert GeminiManager.MAX_REQUESTS_PER_MINUTE == 60
    
    def test_backoff_constants_defined(self):
        """Should have backoff constants defined."""
        assert hasattr(GeminiManager, 'BASE_RETRY_DELAY')
        assert hasattr(GeminiManager, 'MAX_RETRY_DELAY')
        assert hasattr(GeminiManager, 'MAX_RETRIES')
        
        assert GeminiManager.BASE_RETRY_DELAY == 1.0
        assert GeminiManager.MAX_RETRY_DELAY == 32.0
        assert GeminiManager.MAX_RETRIES == 3


class TestModelConfiguration:
    """Tests for Gemini model configuration."""
    
    def test_model_is_gemini_25_flash(self):
        """Should use gemini-2.5-flash model per spec."""
        assert GeminiManager.MODEL == "gemini-2.5-flash"
    
    def test_base_url_defined(self):
        """Should have BASE_URL for Gemini API."""
        assert hasattr(GeminiManager, 'BASE_URL')
        assert "generativelanguage.googleapis.com" in GeminiManager.BASE_URL
    
    def test_batch_size_is_50(self):
        """Should have BATCH_SIZE of 50 per spec."""
        assert GeminiManager.BATCH_SIZE == 50


class TestGroundingPriority:
    """Tests for grounding priority enum."""
    
    def test_grounding_priority_values(self):
        """GroundingPriority should have correct values."""
        assert GroundingPriority.EXPLICIT_CONTENT.value == "explicit_content"
        assert GroundingPriority.CULTURAL_VIBE.value == "cultural_vibe"
        assert GroundingPriority.CANONICAL_TITLE.value == "canonical_title"


class TestGeminiResponse:
    """Tests for GeminiResponse dataclass."""
    
    def test_success_response(self):
        """Should create successful response."""
        response = GeminiResponse(
            success=True,
            data={"genres": ["rock"]},
            grounded=True,
            usage={"totalTokenCount": 100}
        )
        
        assert response.success is True
        assert response.data == {"genres": ["rock"]}
        assert response.grounded is True
        assert response.usage["totalTokenCount"] == 100
    
    def test_failure_response(self):
        """Should create failure response."""
        response = GeminiResponse(
            success=False,
            error="Rate limited"
        )
        
        assert response.success is False
        assert response.error == "Rate limited"
        assert response.data is None


class TestStatistics:
    """Tests for API usage statistics."""
    
    def test_stats_initialized(self):
        """Manager should initialize stats dict."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert hasattr(manager, '_stats')
            assert "requests" in manager._stats
            assert "successes" in manager._stats
            assert "failures" in manager._stats
            assert "retries" in manager._stats
            assert "grounded_requests" in manager._stats
            assert "tokens_used" in manager._stats
    
    @pytest.mark.asyncio
    async def test_get_stats_method(self):
        """get_stats should return usage statistics."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            stats = await manager.get_stats()
            
            assert "available_keys" in stats
            assert "keys_in_cooldown" in stats
            assert stats["available_keys"] == 1


class TestAnalyzeSongMethod:
    """Tests for analyze_song method structure."""
    
    def test_analyze_song_method_exists(self):
        """analyze_song method should exist."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert hasattr(manager, 'analyze_song')
            assert asyncio.iscoroutinefunction(manager.analyze_song)


class TestBatchAnalyzeMethod:
    """Tests for batch_analyze_songs method structure."""
    
    def test_batch_analyze_method_exists(self):
        """batch_analyze_songs method should exist."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert hasattr(manager, 'batch_analyze_songs')
            assert asyncio.iscoroutinefunction(manager.batch_analyze_songs)


class TestRecommendationsMethod:
    """Tests for get_recommendations_prompt method structure."""
    
    def test_get_recommendations_method_exists(self):
        """get_recommendations_prompt method should exist."""
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            config = V3Config()
            bus = create_mock_event_bus()
            manager = GeminiManager(config, bus)
            
            assert hasattr(manager, 'get_recommendations_prompt')
            assert asyncio.iscoroutinefunction(manager.get_recommendations_prompt)


class TestGlobalSingleton:
    """Tests for singleton getter."""
    
    def test_get_gemini_manager_returns_instance(self):
        """get_gemini_manager should return instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.gemini_manager as gem_mod
        gem_mod._gemini_manager = None
        
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            manager = get_gemini_manager()
            
            assert manager is not None
            assert isinstance(manager, GeminiManager)
    
    def test_get_gemini_manager_returns_same_instance(self):
        """get_gemini_manager should return same instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.gemini_manager as gem_mod
        gem_mod._gemini_manager = None
        
        with patch.dict(os.environ, {'GeminiApiKeys': 'key1'}, clear=True):
            manager1 = get_gemini_manager()
            manager2 = get_gemini_manager()
            
            assert manager1 is manager2
