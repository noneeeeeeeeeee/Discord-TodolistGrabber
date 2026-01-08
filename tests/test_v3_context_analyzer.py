"""
Tests for V3 Autoplay Engine Context Analyzer Module

Tests for skip detection, time-weighted preferences, and session profile building.
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

from modules.music.Autoplay_Engine.v3.context_analyzer import (
    ContextAnalyzer,
    PlaybackEvent,
    PreferenceScore,
    SessionProfile,
    get_context_analyzer
)
from modules.music.Autoplay_Engine.v3.constants import SessionState, V3Config


def create_mock_cache():
    """Create a mock cache manager."""
    cache = MagicMock()
    cache.initialize = AsyncMock()
    cache.get_metadata = AsyncMock(return_value=None)
    return cache


def create_mock_event_bus():
    """Create a mock event bus."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


class TestContextAnalyzerInitialization:
    """Tests for ContextAnalyzer initialization."""
    
    def test_context_analyzer_creation(self):
        """Verify context analyzer can be created."""
        config = V3Config()
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        analyzer = ContextAnalyzer(config, cache, bus)
        assert analyzer is not None
        assert analyzer._initialized is False
    
    @pytest.mark.asyncio
    async def test_initialize(self):
        """Context analyzer should initialize successfully."""
        config = V3Config()
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        analyzer = ContextAnalyzer(config, cache, bus)
        
        await analyzer.initialize()
        
        assert analyzer._initialized is True
        cache.initialize.assert_awaited_once()
    
    @pytest.mark.asyncio
    async def test_initialize_idempotent(self):
        """Initialize should be idempotent."""
        config = V3Config()
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        analyzer = ContextAnalyzer(config, cache, bus)
        
        await analyzer.initialize()
        await analyzer.initialize()
        
        # Should only initialize cache once
        cache.initialize.assert_awaited_once()
    
    @pytest.mark.asyncio
    async def test_shutdown(self):
        """Shutdown should unsubscribe from events."""
        config = V3Config()
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        analyzer = ContextAnalyzer(config, cache, bus)
        
        await analyzer.initialize()
        await analyzer.shutdown()
        
        assert analyzer._initialized is False
        assert bus.unsubscribe.call_count == 2


class TestSessionManagement:
    """Tests for session creation and management."""
    
    def test_get_or_create_session_creates_new(self):
        """Should create new session if not exists."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        
        session = analyzer.get_or_create_session("session_1", "guild_123")
        
        assert session is not None
        assert session.session_id == "session_1"
        assert session.guild_id == "guild_123"
        assert session.state == SessionState.COLD
    
    def test_get_or_create_session_returns_existing(self):
        """Should return existing session."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        
        session1 = analyzer.get_or_create_session("session_1", "guild_123")
        session2 = analyzer.get_or_create_session("session_1", "guild_123")
        
        assert session1 is session2
    
    def test_end_session_removes(self):
        """End session should remove and return profile."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        
        session = analyzer.get_or_create_session("session_1", "guild_123")
        ended = analyzer.end_session("session_1")
        
        assert ended is session
        assert analyzer._sessions.get("session_1") is None
    
    def test_end_session_nonexistent(self):
        """End session should return None for nonexistent."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        
        result = analyzer.end_session("nonexistent")
        
        assert result is None


class TestAppleMusicStyleSkipDetection:
    """Tests for Apple Music-style skip classification."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for skip tests."""
        return ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
    
    def test_ended_normally_not_skip(self, analyzer):
        """Song that ended normally is not a skip."""
        result = analyzer._detect_skip(
            duration_played_ms=5000,
            total_duration_ms=180000,
            ended_normally=True
        )
        assert result is False
    
    def test_short_song_skip_threshold(self, analyzer):
        """Short song (<30s): skip if <50% played."""
        # 20s song, played 8s = 40% - should be skip
        result = analyzer._detect_skip(
            duration_played_ms=8000,
            total_duration_ms=20000,
            ended_normally=False
        )
        assert result is True
    
    def test_short_song_not_skip(self, analyzer):
        """Short song (<30s): not skip if >=50% played."""
        # 20s song, played 12s = 60% - should NOT be skip
        result = analyzer._detect_skip(
            duration_played_ms=12000,
            total_duration_ms=20000,
            ended_normally=False
        )
        assert result is False
    
    def test_long_song_skip_threshold(self, analyzer):
        """Long song (>=30s): skip if <30s played."""
        # 3min song, played 20s - should be skip
        result = analyzer._detect_skip(
            duration_played_ms=20000,
            total_duration_ms=180000,
            ended_normally=False
        )
        assert result is True
    
    def test_long_song_not_skip(self, analyzer):
        """Long song (>=30s): not skip if >=30s played."""
        # 3min song, played 35s - should NOT be skip
        result = analyzer._detect_skip(
            duration_played_ms=35000,
            total_duration_ms=180000,
            ended_normally=False
        )
        assert result is False
    
    def test_immediate_skip_is_skip(self, analyzer):
        """Playing only 3 seconds should be a skip."""
        result = analyzer._detect_skip(
            duration_played_ms=3000,
            total_duration_ms=180000,
            ended_normally=False
        )
        assert result is True


class TestPlaybackRecording:
    """Tests for recording playback events."""
    
    @pytest.fixture
    def analyzer(self):
        """Create initialized analyzer."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        analyzer = ContextAnalyzer(cache=cache, event_bus=bus)
        # Create a session
        analyzer.get_or_create_session("session_1", "guild_123")
        return analyzer
    
    @pytest.mark.asyncio
    async def test_record_playback_adds_to_history(self, analyzer):
        """Recording playback should add to history."""
        await analyzer.record_playback(
            session_id="session_1",
            song_id="song_123",
            duration_played_ms=180000,
            total_duration_ms=180000,
            ended_normally=True
        )
        
        session = analyzer._sessions["session_1"]
        assert len(session.playback_history) == 1
        assert session.playback_history[0].song_id == "song_123"
    
    @pytest.mark.asyncio
    async def test_record_playback_updates_play_count(self, analyzer):
        """Recording playback should update play count."""
        for i in range(3):
            await analyzer.record_playback(
                session_id="session_1",
                song_id=f"song_{i}",
                duration_played_ms=180000,
                total_duration_ms=180000,
                ended_normally=True
            )
        
        session = analyzer._sessions["session_1"]
        assert session.total_plays == 3
        assert session.skip_count == 0
    
    @pytest.mark.asyncio
    async def test_record_playback_detects_skip(self, analyzer):
        """Recording should detect skips."""
        await analyzer.record_playback(
            session_id="session_1",
            song_id="song_123",
            duration_played_ms=5000,  # Only 5 seconds
            total_duration_ms=180000,
            ended_normally=False  # User stopped it
        )
        
        session = analyzer._sessions["session_1"]
        assert session.skip_count == 1
        assert session.playback_history[0].was_skipped is True
    
    @pytest.mark.asyncio
    async def test_record_playback_consecutive_skips(self, analyzer):
        """Consecutive skips should be tracked."""
        for i in range(3):
            await analyzer.record_playback(
                session_id="session_1",
                song_id=f"song_{i}",
                duration_played_ms=5000,
                total_duration_ms=180000,
                ended_normally=False
            )
        
        session = analyzer._sessions["session_1"]
        assert session.consecutive_skips == 3
    
    @pytest.mark.asyncio
    async def test_record_playback_resets_consecutive_on_completion(self, analyzer):
        """Completing a song should reset consecutive skips."""
        # Skip 2 songs
        for i in range(2):
            await analyzer.record_playback(
                session_id="session_1",
                song_id=f"song_{i}",
                duration_played_ms=5000,
                total_duration_ms=180000,
                ended_normally=False
            )
        
        # Complete one
        await analyzer.record_playback(
            session_id="session_1",
            song_id="song_complete",
            duration_played_ms=180000,
            total_duration_ms=180000,
            ended_normally=True
        )
        
        session = analyzer._sessions["session_1"]
        assert session.consecutive_skips == 0
    
    @pytest.mark.asyncio
    async def test_record_playback_ignores_unknown_session(self, analyzer):
        """Recording for unknown session should be ignored."""
        await analyzer.record_playback(
            session_id="nonexistent",
            song_id="song_123",
            duration_played_ms=180000,
            total_duration_ms=180000,
            ended_normally=True
        )
        # Should not raise


class TestSessionStateTransitions:
    """Tests for session state updates based on play count."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer with session."""
        cache = create_mock_cache()
        bus = create_mock_event_bus()
        analyzer = ContextAnalyzer(cache=cache, event_bus=bus)
        analyzer.get_or_create_session("session_1", "guild_123")
        return analyzer
    
    @pytest.mark.asyncio
    async def test_starts_cold(self, analyzer):
        """Session should start in COLD state."""
        session = analyzer._sessions["session_1"]
        assert session.state == SessionState.COLD
    
    @pytest.mark.asyncio
    async def test_cold_to_warm_transition(self, analyzer):
        """Should transition to WARM after cold_max plays."""
        # Play enough songs to exit COLD (default cold_max=10)
        for i in range(11):
            await analyzer.record_playback(
                session_id="session_1",
                song_id=f"song_{i}",
                duration_played_ms=180000,
                total_duration_ms=180000,
                ended_normally=True
            )
        
        session = analyzer._sessions["session_1"]
        assert session.state == SessionState.WARM
    
    @pytest.mark.asyncio
    async def test_warm_to_hot_transition(self, analyzer):
        """Should transition to HOT after warm_max plays."""
        # Play enough songs to get to HOT (default warm_max=25)
        for i in range(26):
            await analyzer.record_playback(
                session_id="session_1",
                song_id=f"song_{i}",
                duration_played_ms=180000,
                total_duration_ms=180000,
                ended_normally=True
            )
        
        session = analyzer._sessions["session_1"]
        assert session.state == SessionState.HOT


class TestPreferenceTracking:
    """Tests for preference score tracking."""
    
    def test_preference_score_net_score(self):
        """Net score should account for plays and skips."""
        pref = PreferenceScore(
            value="rock",
            score=5.0,
            play_count=5,
            skip_count=2,
            last_played=time.time()
        )
        
        # Net = score * recency - skips * 0.5
        assert pref.net_score > 0  # Should be positive with recent plays
    
    def test_preference_score_recency_decay(self):
        """Older preferences should have lower recency weight."""
        # Recent preference
        recent = PreferenceScore(
            value="rock",
            score=5.0,
            play_count=5,
            skip_count=0,
            last_played=time.time()
        )
        
        # Old preference (48 hours ago)
        old = PreferenceScore(
            value="jazz",
            score=5.0,
            play_count=5,
            skip_count=0,
            last_played=time.time() - 48 * 3600
        )
        
        assert recent._recency_weight() > old._recency_weight()


class TestPlaybackEventProperties:
    """Tests for PlaybackEvent dataclass properties."""
    
    def test_play_percentage_calculation(self):
        """Should correctly calculate play percentage."""
        event = PlaybackEvent(
            song_id="test",
            started_at=time.time() - 90,
            duration_played_ms=90000,
            total_duration_ms=180000
        )
        
        assert event.play_percentage == 0.5
    
    def test_play_percentage_zero_duration(self):
        """Should handle zero total duration."""
        event = PlaybackEvent(
            song_id="test",
            started_at=time.time(),
            duration_played_ms=0,
            total_duration_ms=0
        )
        
        assert event.play_percentage == 0.0
    
    def test_play_percentage_capped_at_one(self):
        """Percentage should not exceed 1.0."""
        event = PlaybackEvent(
            song_id="test",
            started_at=time.time() - 200,
            duration_played_ms=200000,
            total_duration_ms=180000  # Played more than total
        )
        
        assert event.play_percentage == 1.0


class TestSessionProfileProperties:
    """Tests for SessionProfile dataclass properties."""
    
    def test_skip_rate_calculation(self):
        """Should correctly calculate skip rate."""
        profile = SessionProfile(
            session_id="test",
            guild_id="guild",
            started_at=time.time(),
            total_plays=10,
            skip_count=3
        )
        
        assert profile.skip_rate == 0.3
    
    def test_skip_rate_no_plays(self):
        """Skip rate should be 0 with no plays."""
        profile = SessionProfile(
            session_id="test",
            guild_id="guild",
            started_at=time.time()
        )
        
        assert profile.skip_rate == 0.0
    
    def test_avg_energy_calculation(self):
        """Should calculate average energy from preferences."""
        profile = SessionProfile(
            session_id="test",
            guild_id="guild",
            started_at=time.time(),
            energy_preferences=[0.5, 0.6, 0.7, 0.8, 0.9]
        )
        
        assert profile.avg_energy == 0.7
    
    def test_avg_energy_default(self):
        """Default energy should be 0.5."""
        profile = SessionProfile(
            session_id="test",
            guild_id="guild",
            started_at=time.time()
        )
        
        assert profile.avg_energy == 0.5
    
    def test_avg_bpm_calculation(self):
        """Should calculate average BPM from preferences."""
        profile = SessionProfile(
            session_id="test",
            guild_id="guild",
            started_at=time.time(),
            bpm_preferences=[100.0, 120.0, 140.0]
        )
        
        assert profile.avg_bpm == 120.0
    
    def test_avg_bpm_default(self):
        """Default BPM should be 120."""
        profile = SessionProfile(
            session_id="test",
            guild_id="guild",
            started_at=time.time()
        )
        
        assert profile.avg_bpm == 120.0


class TestGetTopPreferences:
    """Tests for get_top_preferences method."""
    
    def test_get_top_genres(self):
        """Should return top genre preferences."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        
        # Add some genre preferences
        session.genre_preferences["rock"] = PreferenceScore(
            value="rock", score=5.0, play_count=5, skip_count=0, last_played=time.time()
        )
        session.genre_preferences["pop"] = PreferenceScore(
            value="pop", score=2.0, play_count=2, skip_count=0, last_played=time.time()
        )
        
        top = analyzer.get_top_preferences("session_1", "genres", limit=2)
        
        assert len(top) == 2
        assert top[0][0] == "rock"  # Rock should be first
    
    def test_get_top_preferences_empty_session(self):
        """Should return empty list for unknown session."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        
        result = analyzer.get_top_preferences("nonexistent", "genres")
        
        assert result == []


class TestGetAvoidedValues:
    """Tests for get_avoided_values method."""
    
    def test_avoided_genres_high_skip_rate(self):
        """Should identify genres with high skip rate."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        
        # Genre with 80% skip rate
        session.genre_preferences["metal"] = PreferenceScore(
            value="metal", score=-3.0, play_count=1, skip_count=4, last_played=time.time()
        )
        
        avoided = analyzer.get_avoided_values("session_1", "genres")
        
        assert "metal" in avoided
    
    def test_avoided_needs_enough_samples(self):
        """Should not avoid with too few samples."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        
        # Only 2 total plays - not enough samples
        session.genre_preferences["metal"] = PreferenceScore(
            value="metal", score=-1.0, play_count=0, skip_count=2, last_played=time.time()
        )
        
        avoided = analyzer.get_avoided_values("session_1", "genres")
        
        assert "metal" not in avoided  # Not enough samples


class TestReanalysisTriggers:
    """Tests for should_trigger_reanalysis method."""
    
    def test_trigger_on_consecutive_skips(self):
        """Should trigger reanalysis after 3 consecutive skips."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        session.consecutive_skips = 3
        
        assert analyzer.should_trigger_reanalysis("session_1") is True
    
    def test_no_trigger_with_few_skips(self):
        """Should not trigger with 2 or fewer consecutive skips."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        session.consecutive_skips = 2
        
        assert analyzer.should_trigger_reanalysis("session_1") is False
    
    def test_trigger_on_high_recent_skip_rate(self):
        """Should trigger when recent skip rate > 50%."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        
        # 6 skips out of 10 recent plays
        for i in range(4):
            session.playback_history.append(PlaybackEvent(
                song_id=f"s{i}", started_at=time.time(), was_skipped=False
            ))
        for i in range(6):
            session.playback_history.append(PlaybackEvent(
                song_id=f"skip{i}", started_at=time.time(), was_skipped=True
            ))
        
        assert analyzer.should_trigger_reanalysis("session_1") is True


class TestGetSessionContext:
    """Tests for get_session_context method."""
    
    def test_context_includes_all_fields(self):
        """Context should include all required fields."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        
        context = analyzer.get_session_context("session_1")
        
        assert context is not None
        assert context["session_id"] == "session_1"
        assert context["guild_id"] == "guild_123"
        assert "state" in context
        assert "play_count" in context
        assert "skip_rate" in context
        assert "avg_energy" in context
        assert "avg_bpm" in context
    
    def test_context_unknown_session(self):
        """Should return None for unknown session."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        
        context = analyzer.get_session_context("nonexistent")
        
        assert context is None


class TestGetPreferenceVector:
    """Tests for get_preference_vector method."""
    
    def test_vector_includes_normalized_scores(self):
        """Vector should have normalized genre/mood scores."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        session = analyzer.get_or_create_session("session_1", "guild_123")
        
        session.genre_preferences["rock"] = PreferenceScore(
            value="rock", score=5.0, play_count=5, skip_count=0, last_played=time.time()
        )
        session.energy_preferences.append(0.8)
        session.bpm_preferences.append(140.0)
        
        vector = analyzer.get_preference_vector("session_1")
        
        assert vector is not None
        assert "genre:rock" in vector
        assert "energy" in vector
        assert "bpm_normalized" in vector
    
    def test_vector_unknown_session(self):
        """Should return None for unknown session."""
        analyzer = ContextAnalyzer(cache=create_mock_cache(), event_bus=create_mock_event_bus())
        
        vector = analyzer.get_preference_vector("nonexistent")
        
        assert vector is None


class TestGlobalSingleton:
    """Tests for singleton getter."""
    
    def test_get_context_analyzer_returns_instance(self):
        """get_context_analyzer should return instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.context_analyzer as ctx_mod
        ctx_mod._context_analyzer = None
        
        analyzer = get_context_analyzer()
        
        assert analyzer is not None
        assert isinstance(analyzer, ContextAnalyzer)
    
    def test_get_context_analyzer_returns_same_instance(self):
        """get_context_analyzer should return same instance."""
        # Reset singleton for test
        import modules.music.Autoplay_Engine.v3.context_analyzer as ctx_mod
        ctx_mod._context_analyzer = None
        
        analyzer1 = get_context_analyzer()
        analyzer2 = get_context_analyzer()
        
        assert analyzer1 is analyzer2
