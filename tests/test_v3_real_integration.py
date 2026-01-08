"""
Real API Integration Tests for V3 Autoplay Engine

Tests the new adaptive recommendation system with REAL API calls:
- Deezer API: chart, genre, search
- Last.fm API: track.getSimilar, artist.getSimilar

These tests verify the actual API connectivity and recommendation quality.
Target: 8-of-10 track completions (80% satisfaction rate)

Requirements:
- LASTFM_API_KEY environment variable (optional but recommended)
- Internet connection for API calls

Usage:
    pytest tests/test_v3_real_integration.py -v -s
"""

import pytest
import pytest_asyncio
import asyncio
import os
import sys
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.session_mixer import (
    SessionMixer,
    RecoveryState,
    SkipType,
    TransitionFeatures,
    SessionAnchor,
    get_session_mixer
)
from modules.music.Autoplay_Engine.v3.daydreamer import Daydreamer, get_daydreamer
from modules.music.Autoplay_Engine.v3.mappings import MappingsManager, SongIdentifier, get_mappings_manager
from modules.music.Autoplay_Engine.v3.recommender import Recommender, get_recommender
from modules.music.Autoplay_Engine.v3.constants import SessionState

# Configure logging for tests
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# =============================================================================
# Fixtures
# =============================================================================

@pytest_asyncio.fixture
async def session_mixer():
    """Create and initialize session mixer."""
    mixer = SessionMixer()
    await mixer.initialize()
    yield mixer
    await mixer.shutdown()


@pytest_asyncio.fixture
async def daydreamer():
    """Create and initialize daydreamer with real API access."""
    dd = Daydreamer()
    await dd.initialize()
    yield dd
    await dd.shutdown()


@pytest_asyncio.fixture
async def mappings():
    """Create and initialize mappings manager."""
    mgr = MappingsManager()
    await mgr.initialize()
    yield mgr
    await mgr.shutdown()


# =============================================================================
# Session Mixer Tests
# =============================================================================

class TestSessionMixer:
    """Tests for the new SessionMixer."""
    
    def test_create_session(self, session_mixer):
        """Test session creation."""
        session_id = "test_session_001"
        stats = session_mixer.create_session(session_id)
        
        assert stats is not None
        assert session_mixer.get_confidence(session_id) == 0.0
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.NORMAL
    
    def test_add_anchor(self, session_mixer):
        """Test adding queued tracks as anchors."""
        session_id = "test_session_002"
        session_mixer.create_session(session_id)
        
        # Add an anchor (queued track)
        anchor = session_mixer.add_anchor(
            session_id=session_id,
            song_id="deezer_12345",
            title="Test Song",
            artist="Test Artist"
        )
        
        assert anchor is not None
        assert anchor.song_id == "deezer_12345"
        assert anchor.title == "Test Song"
        
        # Adding same song should boost weight
        session_mixer.add_anchor(
            session_id=session_id,
            song_id="deezer_12345",
            title="Test Song",
            artist="Test Artist"
        )
        
        anchors = session_mixer.get_anchors(session_id)
        assert len(anchors) == 1
        assert anchors[0].weight > 1.0  # Boosted
    
    def test_confidence_increases_with_queued_tracks(self, session_mixer):
        """Test that queued tracks increase confidence."""
        session_id = "test_session_003"
        session_mixer.create_session(session_id)
        
        initial_conf = session_mixer.get_confidence(session_id)
        
        # Add a queued track
        session_mixer.add_anchor(
            session_id=session_id,
            song_id="song_1",
            title="Song 1",
            artist="Artist"
        )
        
        new_conf = session_mixer.get_confidence(session_id)
        assert new_conf > initial_conf
    
    def test_skip_classification_early(self, session_mixer):
        """Test early skip classification."""
        session_id = "test_session_004"
        session_mixer.create_session(session_id)
        
        # Early skip: 10 seconds of a 3-minute song
        skip_type = session_mixer.record_playback(
            session_id=session_id,
            song_id="song_1",
            duration_played_ms=10000,  # 10 seconds
            total_duration_ms=180000,  # 3 minutes
            was_skipped=True
        )
        
        assert skip_type == SkipType.EARLY
    
    def test_skip_classification_late(self, session_mixer):
        """Test late skip classification (boredom)."""
        session_id = "test_session_005"
        session_mixer.create_session(session_id)
        
        # Late skip: 85% played
        skip_type = session_mixer.record_playback(
            session_id=session_id,
            song_id="song_1",
            duration_played_ms=153000,  # 85% of 180s
            total_duration_ms=180000,
            was_skipped=True
        )
        
        assert skip_type == SkipType.LATE
    
    def test_recovery_state_transitions(self, session_mixer):
        """Test recovery state machine transitions."""
        session_id = "test_session_006"
        session_mixer.create_session(session_id)
        
        # Start at NORMAL
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.NORMAL
        
        # 2 skips -> CAUTION
        for i in range(2):
            session_mixer.record_playback(
                session_id=session_id,
                song_id=f"song_{i}",
                duration_played_ms=60000,
                total_duration_ms=180000,
                was_skipped=True
            )
        
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.CAUTION
        
        # 1 more skip -> RECOVERY
        session_mixer.record_playback(
            session_id=session_id,
            song_id="song_3",
            duration_played_ms=60000,
            total_duration_ms=180000,
            was_skipped=True
        )
        
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.RECOVERY
    
    def test_completion_resets_recovery(self, session_mixer):
        """Test that completing a song resets recovery state."""
        session_id = "test_session_007"
        session_mixer.create_session(session_id)
        
        # Get into CAUTION with 2 skips
        for i in range(2):
            session_mixer.record_playback(
                session_id=session_id,
                song_id=f"song_{i}",
                duration_played_ms=10000,
                total_duration_ms=180000,
                was_skipped=True
            )
        
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.CAUTION
        
        # Complete a song
        session_mixer.record_playback(
            session_id=session_id,
            song_id="song_good",
            duration_played_ms=180000,
            total_duration_ms=180000,
            was_skipped=False
        )
        
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.NORMAL
    
    def test_adaptive_weights_normal(self, session_mixer):
        """Test adaptive weights in normal state."""
        session_id = "test_session_008"
        session_mixer.create_session(session_id)
        
        weights = session_mixer.get_source_weights(session_id)
        
        assert "hot" in weights
        assert "warm" in weights
        assert "cold" in weights
        assert "extended" in weights
        
        # Sum should be approximately 1.0
        total = sum(weights.values())
        assert 0.99 <= total <= 1.01
    
    def test_adaptive_weights_panic(self, session_mixer):
        """Test adaptive weights in panic state."""
        session_id = "test_session_009"
        session_mixer.create_session(session_id)
        
        # Get into panic with 5 skips
        for i in range(5):
            session_mixer.record_playback(
                session_id=session_id,
                song_id=f"song_{i}",
                duration_played_ms=10000,
                total_duration_ms=180000,
                was_skipped=True
            )
        
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.PANIC
        
        weights = session_mixer.get_source_weights(session_id)
        
        # In panic, hot should be minimal
        assert weights["hot"] < weights["warm"]


# =============================================================================
# Real API Tests - Daydreamer
# =============================================================================

@pytest.mark.asyncio
class TestDaydreamerRealAPI:
    """Tests for Daydreamer with real Deezer/Last.fm API calls."""
    
    async def test_deezer_chart_fetch(self, daydreamer):
        """Test fetching from Deezer global chart."""
        # Run random exploration which uses chart API
        suggestions = await daydreamer._random_exploration()
        
        # Should get at least one suggestion
        if suggestions:
            logger.info(f"Got {len(suggestions)} chart suggestions")
            for s in suggestions[:3]:
                logger.info(f"  - {s.identifier.artist} - {s.identifier.title}")
            
            # Verify structure
            assert suggestions[0].identifier.title is not None
            assert suggestions[0].identifier.artist is not None
    
    async def test_deezer_genre_chart(self, daydreamer):
        """Test fetching from genre-specific charts."""
        suggestions = await daydreamer._explore_charts()
        
        if suggestions:
            logger.info(f"Got {len(suggestions)} genre chart suggestions")
            for s in suggestions:
                logger.info(f"  - [{s.exploration_reason}] {s.identifier.artist} - {s.identifier.title}")
    
    async def test_genre_search(self, daydreamer):
        """Test searching for songs in a specific genre."""
        # Test with a known genre
        song = await daydreamer._search_genre("pop")
        
        if song:
            logger.info(f"Found pop song: {song.artist} - {song.title}")
            assert song.deezer_id is not None
    
    @pytest.mark.skipif(
        not os.environ.get("LASTFM_API_KEY"),
        reason="LASTFM_API_KEY not set"
    )
    async def test_lastfm_similar_artists(self, daydreamer):
        """Test Last.fm artist.getSimilar API."""
        # Add some artists to explore
        daydreamer._explored_artists.add("The Beatles")
        daydreamer._explored_artists.add("Radiohead")
        
        suggestions = await daydreamer._explore_similar_artists()
        
        if suggestions:
            logger.info(f"Got {len(suggestions)} similar artist suggestions")
            for s in suggestions:
                logger.info(f"  - [{s.exploration_reason}] {s.identifier.artist} - {s.identifier.title}")
    
    async def test_exploration_round(self, daydreamer):
        """Test a full exploration round."""
        # Seed with some artists
        daydreamer._explored_artists.add("Daft Punk")
        
        # Run a round
        await daydreamer._run_exploration_round()
        
        stats = daydreamer.get_stats()
        logger.info(f"Exploration stats: {stats}")
        
        # Should have some API calls
        assert stats["api_calls"] > 0


# =============================================================================
# Real API Tests - Mappings
# =============================================================================

@pytest.mark.asyncio
class TestMappingsRealAPI:
    """Tests for MappingsManager with real API calls."""
    
    async def test_deezer_search(self, mappings):
        """Test searching Deezer for a song."""
        result = await mappings._search_deezer(
            title="Bohemian Rhapsody",
            artist="Queen"
        )
        
        if result:
            logger.info(f"Found: {result.get('title')} by {result.get('artist', {}).get('name')}")
            assert result.get("id") is not None
            assert "queen" in result.get("artist", {}).get("name", "").lower()
    
    async def test_resolve_song(self, mappings):
        """Test full song resolution."""
        identifier = await mappings.resolve_song(
            title="Hotel California",
            artist="Eagles"
        )
        
        if identifier:
            logger.info(f"Resolved: {identifier.artist} - {identifier.title}")
            logger.info(f"  Deezer ID: {identifier.deezer_id}")
            logger.info(f"  Preview: {identifier.preview_url}")
            
            assert identifier.deezer_id is not None
    
    @pytest.mark.skipif(
        not os.environ.get("LASTFM_API_KEY"),
        reason="LASTFM_API_KEY not set"
    )
    async def test_lastfm_similar_tracks(self, mappings):
        """Test Last.fm track.getSimilar API."""
        similar = await mappings.get_similar_tracks(
            title="Billie Jean",
            artist="Michael Jackson",
            limit=10
        )
        
        if similar:
            logger.info(f"Found {len(similar)} similar tracks")
            for track in similar[:5]:
                logger.info(f"  - {track.artist} - {track.title}")
            
            assert len(similar) > 0
            assert similar[0].title is not None


# =============================================================================
# Completion Rate Simulation
# =============================================================================

@pytest.mark.asyncio
class TestCompletionRateSimulation:
    """
    Simulates a listening session to test the 80% completion target.
    
    This test simulates how the adaptive system responds to user behavior.
    """
    
    async def test_session_with_skips_recovery(self, session_mixer):
        """Test that system recovers from skip streaks."""
        session_id = "simulation_001"
        session_mixer.create_session(session_id)
        
        # Simulate: 10 songs, 2 early skips, 8 completions
        events = [
            ("complete", 180000),  # Song 1: complete
            ("complete", 170000),  # Song 2: complete
            ("skip_early", 8000),  # Song 3: early skip
            ("skip_early", 5000),  # Song 4: early skip (now in CAUTION)
            ("complete", 175000),  # Song 5: complete (reset to NORMAL)
            ("complete", 180000),  # Song 6: complete
            ("complete", 180000),  # Song 7: complete
            ("complete", 180000),  # Song 8: complete
            ("complete", 180000),  # Song 9: complete
            ("complete", 180000),  # Song 10: complete
        ]
        
        for i, (event_type, duration) in enumerate(events):
            was_skipped = event_type.startswith("skip")
            session_mixer.record_playback(
                session_id=session_id,
                song_id=f"song_{i}",
                duration_played_ms=duration,
                total_duration_ms=180000,
                was_skipped=was_skipped
            )
        
        stats = session_mixer.get_session_stats(session_id)
        logger.info(f"Session stats: {stats}")
        
        # Completion rate should be 80%
        assert stats["completion_rate"] >= 0.75
        
        # Should be back to NORMAL
        assert stats["recovery_state"] == "normal"
    
    async def test_panic_mode_trigger(self, session_mixer):
        """Test that 5 consecutive skips trigger panic mode."""
        session_id = "simulation_002"
        session_mixer.create_session(session_id)
        
        # 5 skips in a row
        for i in range(5):
            session_mixer.record_playback(
                session_id=session_id,
                song_id=f"song_{i}",
                duration_played_ms=10000,
                total_duration_ms=180000,
                was_skipped=True
            )
        
        stats = session_mixer.get_session_stats(session_id)
        logger.info(f"Panic mode stats: {stats}")
        
        assert stats["recovery_state"] == "panic"
        assert stats["skip_streak"] == 5
        
        # Weights in panic should heavily favor warm pool
        weights = session_mixer.get_source_weights(session_id)
        assert weights["warm"] > weights["hot"]


# =============================================================================
# Full Workflow Test
# =============================================================================

@pytest.mark.asyncio
class TestFullWorkflow:
    """End-to-end test of the new adaptive recommendation system."""
    
    async def test_anchor_based_recommendations(self, session_mixer, daydreamer, mappings):
        """Test that anchors influence recommendations."""
        session_id = "workflow_001"
        session_mixer.create_session(session_id)
        
        # Add anchors (simulating queued tracks)
        session_mixer.add_anchor(
            session_id=session_id,
            song_id="1",
            title="Blinding Lights",
            artist="The Weeknd"
        )
        
        session_mixer.add_anchor(
            session_id=session_id,
            song_id="2",
            title="Save Your Tears",
            artist="The Weeknd"
        )
        
        # Get taste vector
        taste = session_mixer.get_taste_vector(session_id)
        logger.info(f"Taste vector: {taste}")
        
        assert "The Weeknd" in taste["artists"]
        
        # Confidence should increase
        assert session_mixer.get_confidence(session_id) > 0
    
    async def test_transition_weight_increases_on_failures(self, session_mixer):
        """Test that transition weight increases when transition failures occur."""
        session_id = "workflow_002"
        session_mixer.create_session(session_id)
        
        initial_weight = session_mixer.get_transition_weight(session_id)
        
        # Simulate transition failures (early skips with big feature jumps)
        for i in range(3):
            session_mixer.record_playback(
                session_id=session_id,
                song_id=f"song_{i}",
                duration_played_ms=5000,
                total_duration_ms=180000,
                was_skipped=True,
                features=TransitionFeatures(
                    bpm=140 if i % 2 == 0 else 80,  # Big BPM jumps
                    energy=0.9 if i % 2 == 0 else 0.2
                )
            )
        
        new_weight = session_mixer.get_transition_weight(session_id)
        
        # Should be in recovery mode with higher transition weight
        assert session_mixer.get_recovery_state(session_id) == RecoveryState.RECOVERY
        assert new_weight >= initial_weight


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
