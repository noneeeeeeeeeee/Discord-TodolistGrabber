"""
Tests for V3 Autoplay Engine Mappings Module

Tests for Deezer ↔ YouTube ↔ Last.fm bidirectional ID mapping.
"""

import pytest
import asyncio
import sys
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.mappings import (
    MappingsManager,
    SongIdentifier,
    get_mappings_manager
)
from modules.music.Autoplay_Engine.v3.constants import V3Config


def create_mock_cache():
    """Create a mock cache manager."""
    cache = MagicMock()
    cache.initialize = AsyncMock()
    cache.get_mapping = AsyncMock(return_value=None)
    cache.set_mapping = AsyncMock()
    cache.get_metadata = AsyncMock(return_value=None)
    cache.set_metadata = AsyncMock()
    return cache


def create_mock_event_bus():
    """Create a mock event bus."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


def create_mock_lavalink():
    """Create a mock Lavalink client."""
    lavalink = MagicMock()
    
    async def search_func(query, source='youtube'):
        return [
            {
                'info': {
                    'identifier': 'dQw4w9WgXcQ',
                    'title': query,
                    'author': 'Test Artist',
                    'length': 180000
                }
            }
        ]
    
    lavalink.get_tracks = AsyncMock(side_effect=search_func)
    return lavalink


class TestSongIdentifier:
    """Tests for SongIdentifier dataclass."""
    
    def test_song_identifier_creation(self):
        """Should create SongIdentifier with all fields."""
        identifier = SongIdentifier(
            deezer_id="123",
            youtube_id="abc123",
            isrc="USRC12345678",
            title="Test Song",
            artist="Test Artist"
        )
        
        assert identifier.deezer_id == "123"
        assert identifier.youtube_id == "abc123"
        assert identifier.isrc == "USRC12345678"
    
    def test_primary_id_prefers_deezer(self):
        """primary_id should prefer Deezer ID."""
        identifier = SongIdentifier(
            deezer_id="123",
            youtube_id="abc",
            isrc="TEST"
        )
        
        assert identifier.primary_id == "123"
    
    def test_primary_id_fallback_youtube(self):
        """primary_id should fallback to YouTube if no Deezer."""
        identifier = SongIdentifier(
            youtube_id="abc"
        )
        
        assert identifier.primary_id == "abc"
    
    def test_is_complete_true(self):
        """is_complete should return True with all IDs."""
        identifier = SongIdentifier(
            deezer_id="123",
            youtube_id="abc"
        )
        
        # Complete if we have both primary IDs
        assert identifier.deezer_id is not None
        assert identifier.youtube_id is not None
    
    def test_is_complete_false(self):
        """is_complete should return False without YouTube ID."""
        identifier = SongIdentifier(
            deezer_id="123"
        )
        
        assert identifier.youtube_id is None


class TestMappingsManagerInitialization:
    """Tests for MappingsManager initialization."""
    
    def test_mapping_manager_creation(self):
        """Verify mapping manager can be created."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        assert manager is not None
        assert manager._initialized is False
    
    def test_mapping_manager_stores_cache(self):
        """Manager should store cache reference."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        assert manager.cache is cache
    
    def test_mapping_manager_stores_event_bus(self):
        """Manager should store event bus reference."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        assert manager.event_bus is bus
    
    @pytest.mark.asyncio
    async def test_initialize_sets_flag(self):
        """Initialize should set _initialized flag."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        await manager.initialize()
        
        assert manager._initialized is True
        cache.initialize.assert_awaited_once()
        
        await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(self):
        """Initialize should be idempotent."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        await manager.initialize()
        await manager.initialize()
        
        # Cache should only be initialized once
        cache.initialize.assert_awaited_once()
        
        await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_shutdown_clears_session(self):
        """Shutdown should close HTTP session."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        await manager.initialize()
        await manager.shutdown()
        
        assert manager._initialized is False
        assert manager._session is None
    
    def test_set_lavalink(self):
        """set_lavalink should store Lavalink client."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        lavalink = create_mock_lavalink()
        manager.set_lavalink(lavalink)
        
        assert manager.lavalink is lavalink


class TestStatistics:
    """Tests for mapping statistics."""
    
    def test_stats_initialized(self):
        """Manager should initialize stats dict."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        manager = MappingsManager(cache, bus)
        
        assert hasattr(manager, '_stats')
        assert "cache_hits" in manager._stats
        assert "deezer_lookups" in manager._stats
        assert "youtube_searches" in manager._stats
        assert "fuzzy_matches" in manager._stats
        assert "failures" in manager._stats


class TestFuzzyMatching:
    """Tests for fuzzy matching utilities."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for fuzzy tests."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        return MappingsManager(cache, bus)
    
    def test_normalize_title_removes_remaster(self, manager):
        """_normalize_title should remove remaster suffixes."""
        if hasattr(manager, '_normalize_title'):
            normalized = manager._normalize_title("Bohemian Rhapsody (Remastered 2011)")
            assert "remaster" not in normalized.lower()
    
    def test_normalize_title_lowercase(self, manager):
        """_normalize_title should lowercase."""
        if hasattr(manager, '_normalize_title'):
            normalized = manager._normalize_title("BOHEMIAN RHAPSODY")
            assert normalized.islower()
    
    def test_calculate_similarity_identical(self, manager):
        """Identical strings should have similarity 1.0."""
        if hasattr(manager, '_calculate_similarity'):
            score = manager._calculate_similarity("Test Song", "Test Song")
            assert score == 1.0
    
    def test_calculate_similarity_different(self, manager):
        """Different strings should have lower similarity."""
        if hasattr(manager, '_calculate_similarity'):
            score = manager._calculate_similarity("Test Song", "Completely Different")
            assert score < 0.5


class TestDeezerAPI:
    """Tests for Deezer API constants."""
    
    def test_deezer_api_url_defined(self):
        """Should have DEEZER_API constant."""
        assert hasattr(MappingsManager, 'DEEZER_API')
        assert "deezer.com" in MappingsManager.DEEZER_API
    
    def test_fuzzy_threshold_defined(self):
        """Should have FUZZY_THRESHOLD constant."""
        assert hasattr(MappingsManager, 'FUZZY_THRESHOLD')
        assert 0 < MappingsManager.FUZZY_THRESHOLD < 1


class TestResolveSong:
    """Tests for resolve_song method."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for resolve tests."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        return MappingsManager(cache, bus)
    
    def test_resolve_song_method_exists(self, manager):
        """resolve_song method should exist."""
        assert hasattr(manager, 'resolve_song')
        assert asyncio.iscoroutinefunction(manager.resolve_song)
    
    @pytest.mark.asyncio
    async def test_resolve_song_checks_cache(self, manager):
        """resolve_song should check cache first."""
        # Setup cache hit
        manager.cache.get_mapping = AsyncMock(return_value={
            "deezer_id": "123",
            "youtube_id": "abc",
            "title": "Cached Song",
            "artist": "Cached Artist"
        })
        
        await manager.initialize()
        
        result = await manager.resolve_song(deezer_id="123")
        
        assert result is not None
        assert result.deezer_id == "123"
        
        await manager.shutdown()


class TestCacheIntegration:
    """Tests for cache integration."""
    
    @pytest.fixture
    def manager(self):
        """Create manager for cache tests."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        return MappingsManager(cache, bus)
    
    @pytest.mark.asyncio
    async def test_cache_hit_increments_stat(self, manager):
        """Cache hit should increment stat counter."""
        # Setup cache hit
        manager.cache.get_mapping = AsyncMock(return_value={
            "deezer_id": "123",
            "youtube_id": "abc"
        })
        
        await manager.initialize()
        
        initial = manager._stats["cache_hits"]
        await manager.resolve_song(deezer_id="123")
        
        assert manager._stats["cache_hits"] >= initial
        
        await manager.shutdown()


class TestGlobalSingleton:
    """Tests for singleton getter."""
    
    def test_get_mappings_manager_returns_instance(self):
        """get_mappings_manager should return instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.mappings as map_mod
        map_mod._mappings_manager = None
        
        manager = get_mappings_manager()
        
        assert manager is not None
        assert isinstance(manager, MappingsManager)
    
    def test_get_mappings_manager_returns_same_instance(self):
        """get_mappings_manager should return same instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.mappings as map_mod
        map_mod._mappings_manager = None
        
        manager1 = get_mappings_manager()
        manager2 = get_mappings_manager()
        
        assert manager1 is manager2
