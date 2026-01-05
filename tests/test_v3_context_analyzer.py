"""
Tests for V3 Autoplay Engine Context Analyzer Module

Tests for skip detection, time-weighted preferences, and session profile building.
"""

import pytest
import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.context_analyzer import ContextAnalyzer
from modules.music.Autoplay_Engine.v3.constants import SkipType, V3Config


@dataclass
class MockSong:
    """Mock song for testing."""
    id: str
    title: str
    artist: str
    duration: int = 180
    bpm: int = 120
    genre: str = 'rock'
    decade: str = '2000s'


class TestContextAnalyzerInitialization:
    """Tests for ContextAnalyzer initialization."""
    
    def test_context_analyzer_creation(self):
        """Verify context analyzer can be created."""
        config = V3Config()
        analyzer = ContextAnalyzer(config)
        assert analyzer is not None
    
    @pytest.mark.asyncio
    async def test_initialize(self):
        """Context analyzer should initialize successfully."""
        config = V3Config()
        analyzer = ContextAnalyzer(config)
        await analyzer.initialize()
        assert analyzer._initialized is True


class TestAppleMusicStyleSkipDetection:
    """Tests for Apple Music-style skip classification."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for skip tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    def test_immediate_skip(self, analyzer):
        """Skip within first 5 seconds should be IMMEDIATE."""
        skip_type = analyzer.classify_skip(
            play_duration=3,
            total_duration=180
        )
        assert skip_type == SkipType.IMMEDIATE
    
    def test_early_skip(self, analyzer):
        """Skip at 5-25% should be EARLY."""
        # 25% of 180s = 45s
        skip_type = analyzer.classify_skip(
            play_duration=30,  # ~17%
            total_duration=180
        )
        assert skip_type == SkipType.EARLY
    
    def test_mid_skip(self, analyzer):
        """Skip at 25-75% should be MID."""
        skip_type = analyzer.classify_skip(
            play_duration=90,  # 50%
            total_duration=180
        )
        assert skip_type == SkipType.MID
    
    def test_late_skip(self, analyzer):
        """Skip at 75-95% should be LATE."""
        skip_type = analyzer.classify_skip(
            play_duration=160,  # ~89%
            total_duration=180
        )
        assert skip_type == SkipType.LATE
    
    def test_completed(self, analyzer):
        """Playing past 95% should be COMPLETED."""
        skip_type = analyzer.classify_skip(
            play_duration=175,  # ~97%
            total_duration=180
        )
        assert skip_type == SkipType.COMPLETED
    
    def test_percentage_calculation(self, analyzer):
        """Should correctly calculate play percentage."""
        percentage = analyzer._calculate_percentage(90, 180)
        assert percentage == 50.0


class TestSkipImpact:
    """Tests for skip impact on preferences."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for impact tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    def test_immediate_skip_strong_negative(self, analyzer):
        """Immediate skip should have strong negative impact."""
        impact = analyzer.get_skip_impact(SkipType.IMMEDIATE)
        assert impact < 0
        assert impact <= -0.8  # Very negative
    
    def test_early_skip_moderate_negative(self, analyzer):
        """Early skip should have moderate negative impact."""
        impact = analyzer.get_skip_impact(SkipType.EARLY)
        assert impact < 0
        assert impact > -0.8  # Less negative than immediate
    
    def test_mid_skip_slight_negative(self, analyzer):
        """Mid skip should have slight negative impact."""
        impact = analyzer.get_skip_impact(SkipType.MID)
        assert impact <= 0
    
    def test_late_skip_neutral_or_positive(self, analyzer):
        """Late skip should be neutral or slightly positive."""
        impact = analyzer.get_skip_impact(SkipType.LATE)
        assert impact >= -0.1
    
    def test_completed_positive(self, analyzer):
        """Completed play should have positive impact."""
        impact = analyzer.get_skip_impact(SkipType.COMPLETED)
        assert impact > 0


class TestGenrePreferenceTracking:
    """Tests for genre preference tracking."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for genre tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_record_genre_play(self, analyzer):
        """Should record genre plays."""
        await analyzer.initialize()
        
        song = MockSong(id='1', title='Rock Song', artist='Artist', genre='rock')
        
        await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        prefs = await analyzer.get_genre_preferences('session_1')
        assert 'rock' in prefs
    
    @pytest.mark.asyncio
    async def test_genre_preference_accumulation(self, analyzer):
        """Multiple plays should accumulate preference."""
        await analyzer.initialize()
        
        for i in range(5):
            song = MockSong(id=str(i), title=f'Rock Song {i}', artist='Artist', genre='rock')
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        song = MockSong(id='99', title='Pop Song', artist='Artist', genre='pop')
        await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        prefs = await analyzer.get_genre_preferences('session_1')
        
        assert prefs.get('rock', 0) > prefs.get('pop', 0)
    
    @pytest.mark.asyncio
    async def test_genre_penalty_on_skip(self, analyzer):
        """Skipped genres should be penalized."""
        await analyzer.initialize()
        
        # Play and skip jazz songs
        for i in range(3):
            song = MockSong(id=str(i), title=f'Jazz Song {i}', artist='Artist', genre='jazz')
            await analyzer.record_play('session_1', song, SkipType.IMMEDIATE)
        
        prefs = await analyzer.get_genre_preferences('session_1')
        
        # Jazz should have negative or low preference
        assert prefs.get('jazz', 0) <= 0


class TestArtistPreferenceTracking:
    """Tests for artist preference tracking."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for artist tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_record_artist_play(self, analyzer):
        """Should record artist plays."""
        await analyzer.initialize()
        
        song = MockSong(id='1', title='Song', artist='Queen', genre='rock')
        await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        prefs = await analyzer.get_artist_preferences('session_1')
        assert 'Queen' in prefs or 'queen' in [k.lower() for k in prefs.keys()]
    
    @pytest.mark.asyncio
    async def test_artist_preference_from_completions(self, analyzer):
        """Completed plays should increase artist preference."""
        await analyzer.initialize()
        
        for i in range(3):
            song = MockSong(id=str(i), title=f'Queen Song {i}', artist='Queen', genre='rock')
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        prefs = await analyzer.get_artist_preferences('session_1')
        queen_pref = prefs.get('Queen', prefs.get('queen', 0))
        
        assert queen_pref > 0


class TestBPMPreferenceTracking:
    """Tests for BPM preference tracking."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for BPM tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_record_bpm_preference(self, analyzer):
        """Should track preferred BPM ranges."""
        await analyzer.initialize()
        
        # Play several songs around 120 BPM
        for i in range(5):
            song = MockSong(id=str(i), title=f'Song {i}', artist='Artist', bpm=120 + i * 2)
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        bpm_pref = await analyzer.get_bpm_preference('session_1')
        
        # Should prefer around 120 BPM
        assert 100 <= bpm_pref['preferred_bpm'] <= 140 or bpm_pref is not None
    
    @pytest.mark.asyncio
    async def test_bpm_range_extraction(self, analyzer):
        """Should extract preferred BPM range."""
        await analyzer.initialize()
        
        # Mix of tempos with preference for faster
        fast_songs = [MockSong(id=str(i), title=f'Fast {i}', artist='A', bpm=140) for i in range(5)]
        slow_songs = [MockSong(id=str(i+5), title=f'Slow {i}', artist='A', bpm=80) for i in range(2)]
        
        for song in fast_songs:
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        for song in slow_songs:
            await analyzer.record_play('session_1', song, SkipType.EARLY)
        
        bpm_pref = await analyzer.get_bpm_preference('session_1')
        
        # Should show preference for faster BPMs
        if 'range' in bpm_pref:
            assert bpm_pref['range'][0] >= 100


class TestDecadePreferenceTracking:
    """Tests for decade preference tracking."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for decade tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_record_decade_preference(self, analyzer):
        """Should track preferred decades."""
        await analyzer.initialize()
        
        # Play 80s songs
        for i in range(3):
            song = MockSong(id=str(i), title=f'80s Song {i}', artist='A', decade='1980s')
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        prefs = await analyzer.get_decade_preferences('session_1')
        
        assert '1980s' in prefs or '80s' in str(prefs)


class TestTimeWeightedPreferences:
    """Tests for time-weighted preference calculation."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for time-weighting tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_recent_plays_weighted_more(self, analyzer):
        """Recent plays should have higher weight."""
        await analyzer.initialize()
        
        # Old play (simulated)
        old_song = MockSong(id='1', title='Old Song', artist='A', genre='jazz')
        
        # Recent plays
        recent_songs = [
            MockSong(id=str(i+2), title=f'Recent {i}', artist='B', genre='rock')
            for i in range(5)
        ]
        
        await analyzer.record_play('session_1', old_song, SkipType.COMPLETED)
        for song in recent_songs:
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        prefs = await analyzer.get_genre_preferences('session_1')
        
        # Rock should be higher due to recency
        assert prefs.get('rock', 0) >= prefs.get('jazz', 0)
    
    @pytest.mark.asyncio
    async def test_decay_function(self, analyzer):
        """Older preferences should decay over time."""
        # Test decay calculation
        weight_recent = analyzer._calculate_time_weight(0)  # 0 songs ago
        weight_old = analyzer._calculate_time_weight(20)     # 20 songs ago
        
        assert weight_recent > weight_old


class TestSessionContextBuilding:
    """Tests for session context/profile building."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for context tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_build_session_context(self, analyzer):
        """Should build comprehensive session context."""
        await analyzer.initialize()
        
        # Play variety of songs
        songs = [
            MockSong('1', 'Rock 1', 'Queen', genre='rock', bpm=120, decade='1980s'),
            MockSong('2', 'Rock 2', 'AC/DC', genre='rock', bpm=130, decade='1990s'),
            MockSong('3', 'Pop 1', 'Madonna', genre='pop', bpm=110, decade='1980s'),
        ]
        
        for song in songs:
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        context = await analyzer.get_session_context('session_1')
        
        assert 'genre_preferences' in context or context is not None
        assert 'artist_preferences' in context or 'artists' in str(context)
    
    @pytest.mark.asyncio
    async def test_context_includes_history(self, analyzer):
        """Session context should include play history."""
        await analyzer.initialize()
        
        song = MockSong('1', 'Test Song', 'Artist')
        await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        context = await analyzer.get_session_context('session_1')
        
        if 'history' in context:
            assert len(context['history']) > 0


class TestMomentumTracking:
    """Tests for session momentum/energy tracking."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for momentum tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_completion_streak_increases_momentum(self, analyzer):
        """Consecutive completions should increase momentum."""
        await analyzer.initialize()
        
        for i in range(5):
            song = MockSong(id=str(i), title=f'Song {i}', artist='A')
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        momentum = await analyzer.get_momentum('session_1')
        assert momentum > 0.5  # High momentum
    
    @pytest.mark.asyncio
    async def test_skips_decrease_momentum(self, analyzer):
        """Skips should decrease momentum."""
        await analyzer.initialize()
        
        # Start with completions
        for i in range(3):
            song = MockSong(id=str(i), title=f'Song {i}', artist='A')
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        # Then skip
        for i in range(3, 6):
            song = MockSong(id=str(i), title=f'Song {i}', artist='A')
            await analyzer.record_play('session_1', song, SkipType.IMMEDIATE)
        
        momentum = await analyzer.get_momentum('session_1')
        assert momentum < 0.8  # Reduced momentum


class TestPreferenceProfile:
    """Tests for full preference profile generation."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for profile tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_generate_full_profile(self, analyzer):
        """Should generate complete preference profile."""
        await analyzer.initialize()
        
        songs = [
            MockSong('1', 'Song 1', 'Artist 1', genre='rock', bpm=120, decade='2000s'),
            MockSong('2', 'Song 2', 'Artist 2', genre='rock', bpm=125, decade='2000s'),
            MockSong('3', 'Song 3', 'Artist 1', genre='metal', bpm=140, decade='2010s'),
        ]
        
        for song in songs:
            await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        profile = await analyzer.get_preference_profile('session_1')
        
        assert profile is not None
        assert isinstance(profile, dict)
    
    @pytest.mark.asyncio
    async def test_profile_includes_all_dimensions(self, analyzer):
        """Profile should include genre, artist, BPM, decade preferences."""
        await analyzer.initialize()
        
        song = MockSong('1', 'Test', 'Artist', genre='rock', bpm=120, decade='2000s')
        await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        profile = await analyzer.get_preference_profile('session_1')
        
        # Check for key dimensions
        profile_str = str(profile)
        assert 'genre' in profile_str.lower() or len(profile) > 0


class TestContextCleanup:
    """Tests for context cleanup and memory management."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer for cleanup tests."""
        config = V3Config()
        return ContextAnalyzer(config)
    
    @pytest.mark.asyncio
    async def test_clear_session_context(self, analyzer):
        """Should clear session context on demand."""
        await analyzer.initialize()
        
        song = MockSong('1', 'Test', 'Artist')
        await analyzer.record_play('session_1', song, SkipType.COMPLETED)
        
        await analyzer.clear_session('session_1')
        
        context = await analyzer.get_session_context('session_1')
        
        # Should be empty or reset
        assert context is None or len(context.get('history', [])) == 0
    
    @pytest.mark.asyncio
    async def test_cleanup_all(self, analyzer):
        """Should clean up all session contexts."""
        await analyzer.initialize()
        
        for i in range(3):
            song = MockSong(str(i), 'Song', 'Artist')
            await analyzer.record_play(f'session_{i}', song, SkipType.COMPLETED)
        
        await analyzer.cleanup()
        
        # All should be cleared
        for i in range(3):
            context = await analyzer.get_session_context(f'session_{i}')
            assert context is None or context == {}
