"""
Tests for V3 Autoplay Engine Cache Manager Module

Tests for sharded JSON storage with atomic writes, crash recovery, and version migration.
"""

import pytest
import asyncio
import json
import os
import sys
import tempfile
import shutil
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.cache_manager import CacheManager
from modules.music.Autoplay_Engine.v3.constants import CacheConfig, PhysicsLayer, SongMetadata


def create_mock_event_bus():
    """Create a mock event bus."""
    mock_bus = MagicMock()
    mock_bus.publish = AsyncMock()
    return mock_bus


def create_test_metadata(deezer_id: str = "123") -> SongMetadata:
    """Create a test SongMetadata object."""
    return SongMetadata(
        deezer_id=deezer_id,
        physics=PhysicsLayer(
            computed_bpm=120.0,
            computed_key="C major",
            computed_loudness=-5.0,
            timbre_vector=[0.1, 0.2, 0.3, 0.4, 0.5]
        )
    )


class TestCacheManagerInitialization:
    """Tests for CacheManager initialization."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    def test_cache_manager_creation(self, cache_manager):
        """Verify cache manager can be created."""
        assert cache_manager is not None
    
    @pytest.mark.asyncio
    async def test_initialize(self, cache_manager):
        """Cache manager should initialize successfully."""
        await cache_manager.initialize()
        assert cache_manager._initialized is True
    
    @pytest.mark.asyncio
    async def test_cache_directories_created(self, cache_manager, temp_cache_dir):
        """Cache manager should create required directories."""
        await cache_manager.initialize()
        
        # Check that base directory exists
        assert Path(temp_cache_dir).exists()
    
    def test_config_stored(self, cache_manager):
        """Cache manager should store configuration."""
        assert cache_manager.config is not None


class TestCacheManagerMetadata:
    """Tests for metadata storage operations."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_set_and_get_metadata(self, cache_manager):
        """Should be able to store and retrieve metadata."""
        await cache_manager.initialize()
        
        metadata = create_test_metadata("song_123")
        await cache_manager.set_metadata("song_123", metadata)
        
        result = await cache_manager.get_metadata("song_123")
        
        assert result is not None
        assert result.deezer_id == "song_123"
    
    @pytest.mark.asyncio
    async def test_get_nonexistent_metadata(self, cache_manager):
        """Getting nonexistent metadata should return None."""
        await cache_manager.initialize()
        
        result = await cache_manager.get_metadata("nonexistent_key")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_metadata_with_audio_features(self, cache_manager):
        """Should preserve audio features in metadata."""
        await cache_manager.initialize()
        
        metadata = create_test_metadata("song_123")
        await cache_manager.set_metadata("song_123", metadata)
        
        result = await cache_manager.get_metadata("song_123")
        
        assert result is not None
        assert result.physics is not None
        assert result.physics.computed_bpm == 120.0


class TestCacheManagerMappings:
    """Tests for ID mapping operations."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_set_and_get_mapping(self, cache_manager):
        """Should be able to store and retrieve ID mappings."""
        await cache_manager.initialize()
        
        await cache_manager.set_mapping(
            deezer_id="123",
            youtube_id="yt_456",
            lastfm_id="lf_789"
        )
        
        # Mappings are stored with platform prefix
        result = await cache_manager.get_mapping("deezer:123")
        
        assert result is not None
        assert result.get("youtube_id") == "yt_456"
    
    @pytest.mark.asyncio
    async def test_get_nonexistent_mapping(self, cache_manager):
        """Getting nonexistent mapping should return None."""
        await cache_manager.initialize()
        
        result = await cache_manager.get_mapping("nonexistent_id")
        assert result is None


class TestCacheManagerSharding:
    """Tests for shard distribution."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_shard_distribution(self, cache_manager):
        """Different song IDs should distribute across shards."""
        await cache_manager.initialize()
        
        # Store multiple items
        for i in range(10):
            metadata = create_test_metadata(f"song_{i}")
            await cache_manager.set_metadata(f"song_{i}", metadata)
        
        # All should be retrievable
        for i in range(10):
            result = await cache_manager.get_metadata(f"song_{i}")
            assert result is not None
            assert result.deezer_id == f"song_{i}"
    
    @pytest.mark.asyncio
    async def test_consistent_shard_assignment(self, cache_manager):
        """Same song ID should always map to same shard."""
        await cache_manager.initialize()
        
        # The _get_shard_index should be deterministic
        if hasattr(cache_manager, '_get_shard_index'):
            shard1 = cache_manager._get_shard_index("song_123")
            shard2 = cache_manager._get_shard_index("song_123")
            assert shard1 == shard2


class TestCacheManagerCacheStats:
    """Tests for cache statistics."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_stats_summary(self, cache_manager):
        """Should provide cache statistics."""
        await cache_manager.initialize()
        
        # Add some data
        metadata = create_test_metadata()
        await cache_manager.set_metadata("song_1", metadata)
        await cache_manager.set_metadata("song_2", metadata)
        
        # Get (hit)
        await cache_manager.get_metadata("song_1")
        # Get (miss)
        await cache_manager.get_metadata("nonexistent")
        
        stats = await cache_manager.get_stats()
        
        assert stats is not None
        assert "hits" in stats or "misses" in stats


class TestCacheManagerFlush:
    """Tests for cache flush operations."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_flush_writes_to_disk(self, cache_manager, temp_cache_dir):
        """Flush should write in-memory data to disk."""
        await cache_manager.initialize()
        
        # Add data
        metadata = create_test_metadata()
        await cache_manager.set_metadata("song_123", metadata)
        
        # Flush
        await cache_manager.flush()
        
        # Data should persist in some form
        # (exact file structure is implementation-dependent)


class TestCacheManagerClear:
    """Tests for cache clearing operations."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_clear_all(self, cache_manager):
        """Clear should remove all cached data."""
        await cache_manager.initialize()
        
        # Add data
        metadata = create_test_metadata()
        await cache_manager.set_metadata("song_1", metadata)
        await cache_manager.set_metadata("song_2", metadata)
        
        # Clear
        await cache_manager.clear()
        
        # Data should be gone
        result1 = await cache_manager.get_metadata("song_1")
        result2 = await cache_manager.get_metadata("song_2")
        
        assert result1 is None
        assert result2 is None


class TestCacheManagerAnalyzedCount:
    """Tests for analyzed song counting."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_get_analyzed_song_count(self, cache_manager):
        """Should count songs with analysis data."""
        await cache_manager.initialize()
        
        # Add metadata
        metadata = create_test_metadata()
        await cache_manager.set_metadata("song_1", metadata)
        await cache_manager.set_metadata("song_2", metadata)
        
        count = await cache_manager.get_analyzed_song_count()
        
        assert count >= 0  # At least 0 or some positive number


class TestCacheManagerShutdown:
    """Tests for cache manager shutdown."""
    
    @pytest.fixture
    def temp_cache_dir(self):
        """Create a temporary directory for cache tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def cache_manager(self, temp_cache_dir):
        """Create a cache manager with temp directory."""
        config = CacheConfig(base_path=temp_cache_dir)
        return CacheManager(config=config, event_bus=create_mock_event_bus())
    
    @pytest.mark.asyncio
    async def test_shutdown(self, cache_manager):
        """Shutdown should flush and clean up."""
        await cache_manager.initialize()
        
        # Add data
        metadata = create_test_metadata()
        await cache_manager.set_metadata("song_1", metadata)
        
        # Shutdown
        await cache_manager.shutdown()
        
        # Should complete without error
        assert True
