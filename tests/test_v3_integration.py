"""
Integration Tests for V3 Autoplay Engine

Tests for the complete autoplay system working together.
These tests verify the public API of V3Engine without requiring
external services like Gemini.
"""

import pytest
import pytest_asyncio
import asyncio
import sys
import os
import tempfile
import shutil
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3 import V3Engine
from modules.music.Autoplay_Engine.v3.constants import (
    SessionState,
    V3Config,
    CacheConfig
)


@dataclass
class MockTrack:
    """Mock track for integration testing."""
    id: str
    title: str
    artist: str
    duration: int = 180
    preview_url: str = 'https://example.com/preview.mp3'
    deezer_id: int = None
    
    def __post_init__(self):
        if self.deezer_id is None:
            self.deezer_id = int(self.id) if self.id.isdigit() else abs(hash(self.id)) % 1000000


def create_mock_event_bus():
    """Create a mock event bus."""
    bus = MagicMock()
    bus.start = AsyncMock()
    bus.stop = AsyncMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


def create_mock_cache_manager():
    """Create a mock cache manager."""
    cache = MagicMock()
    cache.initialize = AsyncMock()
    cache.shutdown = AsyncMock()
    cache.get_metadata = AsyncMock(return_value=None)
    cache.set_metadata = AsyncMock()
    return cache


def create_mock_session_manager():
    """Create a mock session manager."""
    mgr = MagicMock()
    mgr.initialize = AsyncMock()
    mgr.shutdown = AsyncMock()
    mgr._sessions = {}
    
    async def create_session(guild_id, voice_channel_id):
        session = MagicMock()
        session.session_id = f"session_{guild_id}_{voice_channel_id}"
        session.guild_id = guild_id
        session.voice_channel_id = voice_channel_id
        session.state = SessionState.COLD
        session.song_count = 0
        session.skip_count = 0
        mgr._sessions[session.session_id] = session
        return session
    
    mgr.create_session = AsyncMock(side_effect=create_session)
    
    async def get_session(session_id):
        return mgr._sessions.get(session_id)
    
    mgr.get_session = AsyncMock(side_effect=get_session)
    
    async def end_session(session_id=None, guild_id=None):
        if session_id and session_id in mgr._sessions:
            del mgr._sessions[session_id]
        if guild_id:
            # Remove by guild_id
            to_remove = [sid for sid, s in mgr._sessions.items() if s.guild_id == guild_id]
            for sid in to_remove:
                del mgr._sessions[sid]
    
    mgr.end_session = AsyncMock(side_effect=end_session)
    mgr.close_session = AsyncMock(side_effect=end_session)
    mgr.get_active_sessions = MagicMock(return_value=list(mgr._sessions.values()))
    
    return mgr


def create_mock_buffer_manager():
    """Create a mock buffer manager."""
    mgr = MagicMock()
    mgr.initialize = AsyncMock()
    mgr.shutdown = AsyncMock()
    mgr.fill_initial_buffer = AsyncMock()
    mgr._buffers = {}
    
    def get_next(session_id):
        buf = mgr._buffers.get(session_id, [])
        if buf:
            return buf.pop(0)
        return None
    
    mgr.get_next = MagicMock(side_effect=get_next)
    
    def add_songs(session_id, songs):
        if session_id not in mgr._buffers:
            mgr._buffers[session_id] = []
        mgr._buffers[session_id].extend(songs)
    
    mgr.add_songs = AsyncMock(side_effect=add_songs)
    mgr.size = MagicMock(side_effect=lambda sid: len(mgr._buffers.get(sid, [])))
    
    return mgr


def create_mock_recommender():
    """Create a mock recommender."""
    rec = MagicMock()
    rec.initialize = AsyncMock()
    rec.shutdown = AsyncMock()
    rec.set_daydreamer = MagicMock()
    rec.get_recommendation = AsyncMock(return_value=None)
    return rec


def create_mock_analyzer():
    """Create a mock song analyzer."""
    analyzer = MagicMock()
    analyzer.initialize = AsyncMock()
    analyzer.shutdown = AsyncMock()
    analyzer.analyze = AsyncMock(return_value=None)
    return analyzer


def create_mock_mappings():
    """Create a mock mappings manager."""
    mappings = MagicMock()
    mappings.initialize = AsyncMock()
    mappings.shutdown = AsyncMock()
    mappings.resolve_song = AsyncMock(return_value=None)
    return mappings


def create_mock_daydreamer():
    """Create a mock daydreamer."""
    dd = MagicMock()
    dd.initialize = AsyncMock()
    dd.shutdown = AsyncMock()
    return dd


class TestV3EngineCreation:
    """Tests for V3Engine instantiation."""
    
    def test_create_engine_with_default_config(self):
        """Should create V3Engine with default config."""
        # We need to patch all the singleton getters
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert engine is not None
            assert engine._initialized is False
    
    def test_create_engine_with_custom_config(self):
        """Should create V3Engine with custom config."""
        config = V3Config()
        
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine(config)
            assert engine.config is config


class TestV3EngineInitialization:
    """Tests for V3Engine initialization."""
    
    @pytest.mark.asyncio
    async def test_initialize_success(self):
        """Should initialize all components."""
        mock_event_bus = create_mock_event_bus()
        mock_cache = create_mock_cache_manager()
        mock_session = create_mock_session_manager()
        mock_buffer = create_mock_buffer_manager()
        mock_recommender = create_mock_recommender()
        mock_analyzer = create_mock_analyzer()
        mock_mappings = create_mock_mappings()
        mock_daydreamer = create_mock_daydreamer()
        
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=mock_event_bus), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=mock_cache), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=mock_session), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=mock_buffer), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=mock_recommender), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=mock_analyzer), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=mock_mappings), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=mock_daydreamer):
            
            engine = V3Engine()
            await engine.initialize()
            
            assert engine._initialized is True
            mock_event_bus.start.assert_awaited_once()
            mock_cache.initialize.assert_awaited_once()
            mock_mappings.initialize.assert_awaited_once()
            mock_analyzer.initialize.assert_awaited_once()
            mock_session.initialize.assert_awaited_once()
            mock_buffer.initialize.assert_awaited_once()
            mock_recommender.initialize.assert_awaited_once()
            mock_daydreamer.initialize.assert_awaited_once()
    
    @pytest.mark.asyncio
    async def test_initialize_idempotent(self):
        """Initialize should be idempotent."""
        mock_event_bus = create_mock_event_bus()
        
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=mock_event_bus), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            await engine.initialize()
            await engine.initialize()  # Second call
            
            # Should only be called once
            assert mock_event_bus.start.await_count == 1
    
    @pytest.mark.asyncio
    async def test_shutdown(self):
        """Should shutdown all components."""
        mock_event_bus = create_mock_event_bus()
        mock_cache = create_mock_cache_manager()
        mock_daydreamer = create_mock_daydreamer()
        
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=mock_event_bus), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=mock_cache), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=mock_daydreamer):
            
            engine = V3Engine()
            await engine.initialize()
            await engine.shutdown()
            
            mock_daydreamer.shutdown.assert_awaited()
            mock_cache.shutdown.assert_awaited()
            mock_event_bus.stop.assert_awaited()


class TestV3EngineSessionManagement:
    """Tests for session management through V3Engine."""
    
    @pytest_asyncio.fixture
    async def engine(self):
        """Create an initialized V3Engine with mocked components."""
        mock_event_bus = create_mock_event_bus()
        mock_cache = create_mock_cache_manager()
        mock_session = create_mock_session_manager()
        mock_buffer = create_mock_buffer_manager()
        mock_recommender = create_mock_recommender()
        mock_analyzer = create_mock_analyzer()
        mock_mappings = create_mock_mappings()
        mock_daydreamer = create_mock_daydreamer()
        
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=mock_event_bus), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=mock_cache), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=mock_session), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=mock_buffer), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=mock_recommender), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=mock_analyzer), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=mock_mappings), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=mock_daydreamer):
            
            engine = V3Engine()
            await engine.initialize()
            yield engine
            await engine.shutdown()
    
    @pytest.mark.asyncio
    async def test_start_session(self, engine):
        """Should start a new autoplay session."""
        seed_track = MockTrack('1', 'Test Song', 'Test Artist')
        
        session = await engine.start_session(
            guild_id='123456',
            voice_channel_id='789012',
            seed_songs=[seed_track]
        )
        
        assert session is not None
        assert session.guild_id == '123456'
        assert session.state == SessionState.COLD
    
    @pytest.mark.asyncio
    async def test_end_session(self, engine):
        """Should end a session."""
        seed_track = MockTrack('1', 'Test Song', 'Test Artist')
        
        session = await engine.start_session(
            guild_id='123456',
            voice_channel_id='789012',
            seed_songs=[seed_track]
        )
        
        await engine.end_session(session.session_id)
        
        # close_session should have been called (the actual method name)
        engine.session_mgr.close_session.assert_awaited()


class TestV3EnginePublicAPI:
    """Tests for V3Engine public API methods."""
    
    def test_has_start_session(self):
        """Should have start_session method."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'start_session')
            assert asyncio.iscoroutinefunction(engine.start_session)
    
    def test_has_end_session(self):
        """Should have end_session method."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'end_session')
            assert asyncio.iscoroutinefunction(engine.end_session)
    
    def test_has_get_next_song(self):
        """Should have get_next_song method."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'get_next_song')
            assert asyncio.iscoroutinefunction(engine.get_next_song)
    
    def test_has_record_playback(self):
        """Should have record_playback method."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'record_playback')
            assert asyncio.iscoroutinefunction(engine.record_playback)
    
    def test_has_skip_current(self):
        """Should have skip_current method."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'skip_current')
            assert asyncio.iscoroutinefunction(engine.skip_current)
    
    def test_has_get_stats(self):
        """Should have get_stats method."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'get_stats')
            assert asyncio.iscoroutinefunction(engine.get_stats)
    
    def test_has_health_check(self):
        """Should have health_check method."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'health_check')
            assert asyncio.iscoroutinefunction(engine.health_check)


class TestV3EngineComponentAccess:
    """Tests for component access through V3Engine."""
    
    def test_has_event_bus(self):
        """Should expose event_bus."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'event_bus')
    
    def test_has_cache(self):
        """Should expose cache."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'cache')
    
    def test_has_session_mgr(self):
        """Should expose session_mgr."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'session_mgr')
    
    def test_has_recommender(self):
        """Should expose recommender."""
        with patch('modules.music.Autoplay_Engine.v3.get_event_bus', return_value=create_mock_event_bus()), \
             patch('modules.music.Autoplay_Engine.v3.get_cache_manager', return_value=create_mock_cache_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_session_manager', return_value=create_mock_session_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_buffer_manager', return_value=create_mock_buffer_manager()), \
             patch('modules.music.Autoplay_Engine.v3.get_recommender', return_value=create_mock_recommender()), \
             patch('modules.music.Autoplay_Engine.v3.get_song_analyzer', return_value=create_mock_analyzer()), \
             patch('modules.music.Autoplay_Engine.v3.get_mappings_manager', return_value=create_mock_mappings()), \
             patch('modules.music.Autoplay_Engine.v3.get_daydreamer', return_value=create_mock_daydreamer()):
            
            engine = V3Engine()
            assert hasattr(engine, 'recommender')
