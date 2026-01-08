"""
Unit Tests for V3 Session Mixer

Tests for the new adaptive recommendation weight system:
- Session confidence tracking
- Skip classification
- Recovery state machine
- Anchor management
- Adaptive weights
"""

import pytest
import pytest_asyncio
import time
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.session_mixer import (
    SessionMixer,
    RecoveryState,
    SkipType,
    TransitionFeatures,
    SessionAnchor,
    SessionStats,
    get_session_mixer
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mixer():
    """Create a session mixer instance."""
    return SessionMixer()


@pytest.fixture
def initialized_session(mixer):
    """Create a mixer with an initialized session."""
    session_id = "test_session"
    mixer.create_session(session_id)
    return mixer, session_id


# =============================================================================
# Session Creation Tests
# =============================================================================

class TestSessionCreation:
    """Tests for session creation and management."""
    
    def test_create_session(self, mixer):
        """Test creating a new session."""
        session_id = "new_session"
        stats = mixer.create_session(session_id)
        
        assert stats is not None
        assert isinstance(stats, SessionStats)
        assert stats.total_played == 0
        assert stats.skip_streak == 0
    
    def test_get_session(self, initialized_session):
        """Test getting an existing session."""
        mixer, session_id = initialized_session
        stats = mixer.get_session(session_id)
        
        assert stats is not None
    
    def test_get_nonexistent_session(self, mixer):
        """Test getting a session that doesn't exist."""
        stats = mixer.get_session("nonexistent")
        assert stats is None
    
    def test_end_session(self, initialized_session):
        """Test ending a session."""
        mixer, session_id = initialized_session
        mixer.end_session(session_id)
        
        assert mixer.get_session(session_id) is None
        assert mixer.get_confidence(session_id) == 0.0


# =============================================================================
# Anchor Management Tests
# =============================================================================

class TestAnchorManagement:
    """Tests for anchor (queued track) management."""
    
    def test_add_anchor(self, initialized_session):
        """Test adding an anchor."""
        mixer, session_id = initialized_session
        
        anchor = mixer.add_anchor(
            session_id=session_id,
            song_id="song_1",
            title="Test Song",
            artist="Test Artist"
        )
        
        assert anchor is not None
        assert anchor.song_id == "song_1"
        assert anchor.title == "Test Song"
        assert anchor.weight == 1.0
    
    def test_add_anchor_boost(self, initialized_session):
        """Test that adding the same anchor boosts its weight."""
        mixer, session_id = initialized_session
        
        # Add first time
        mixer.add_anchor(session_id, "song_1", "Test", "Artist")
        
        # Add again
        anchor = mixer.add_anchor(session_id, "song_1", "Test", "Artist")
        
        assert anchor.weight > 1.0
    
    def test_get_anchors(self, initialized_session):
        """Test getting all anchors."""
        mixer, session_id = initialized_session
        
        mixer.add_anchor(session_id, "song_1", "Song 1", "Artist")
        mixer.add_anchor(session_id, "song_2", "Song 2", "Artist")
        mixer.add_anchor(session_id, "song_3", "Song 3", "Artist")
        
        anchors = mixer.get_anchors(session_id)
        
        assert len(anchors) == 3
    
    def test_get_taste_vector(self, initialized_session):
        """Test building taste vector from anchors."""
        mixer, session_id = initialized_session
        
        mixer.add_anchor(session_id, "1", "Song 1", "Artist A")
        mixer.add_anchor(session_id, "2", "Song 2", "Artist B")
        
        taste = mixer.get_taste_vector(session_id)
        
        assert "artists" in taste
        assert "avg_bpm" in taste
        assert "avg_energy" in taste
        assert len(taste["artists"]) == 2
    
    def test_anchor_recency_weight(self):
        """Test anchor recency weight decay."""
        anchor = SessionAnchor(
            song_id="1",
            title="Test",
            artist="Artist",
            queued_at=time.time() - 3600  # 1 hour ago
        )
        
        # After 1 hour, recency weight should be less than 1.0
        assert anchor.recency_weight < 1.0


# =============================================================================
# Skip Classification Tests
# =============================================================================

class TestSkipClassification:
    """Tests for skip type classification."""
    
    def test_early_skip_by_time(self, initialized_session):
        """Test early skip detection by absolute time."""
        mixer, session_id = initialized_session
        
        skip_type = mixer.classify_skip(
            session_id=session_id,
            duration_played_ms=10000,  # 10s < 15s threshold
            total_duration_ms=180000
        )
        
        assert skip_type == SkipType.EARLY
    
    def test_early_skip_by_percentage(self, initialized_session):
        """Test early skip detection by percentage."""
        mixer, session_id = initialized_session
        
        skip_type = mixer.classify_skip(
            session_id=session_id,
            duration_played_ms=15000,  # 5% of a 5-minute song
            total_duration_ms=300000
        )
        
        assert skip_type == SkipType.EARLY
    
    def test_late_skip(self, initialized_session):
        """Test late skip detection (boredom indicator)."""
        mixer, session_id = initialized_session
        
        skip_type = mixer.classify_skip(
            session_id=session_id,
            duration_played_ms=144000,  # 80% of 3 minutes
            total_duration_ms=180000
        )
        
        assert skip_type == SkipType.LATE
    
    def test_normal_skip(self, initialized_session):
        """Test normal skip (between 10-70%)."""
        mixer, session_id = initialized_session
        
        skip_type = mixer.classify_skip(
            session_id=session_id,
            duration_played_ms=90000,  # 50% of 3 minutes
            total_duration_ms=180000
        )
        
        assert skip_type == SkipType.NORMAL
    
    def test_transition_skip(self, initialized_session):
        """Test transition failure detection."""
        mixer, session_id = initialized_session
        
        # Set last track features
        stats = mixer.get_session(session_id)
        stats.last_track_features = TransitionFeatures(
            bpm=80,
            energy=0.3,
            key="C"
        )
        
        # Early skip with big feature jump
        skip_type = mixer.classify_skip(
            session_id=session_id,
            duration_played_ms=5000,  # Early
            total_duration_ms=180000,
            current_features=TransitionFeatures(
                bpm=150,  # +70 BPM
                energy=0.9,  # +0.6 energy
                key="F#"
            )
        )
        
        assert skip_type == SkipType.TRANSITION


# =============================================================================
# Playback Recording Tests
# =============================================================================

class TestPlaybackRecording:
    """Tests for recording playback events."""
    
    def test_record_completion(self, initialized_session):
        """Test recording a completed song."""
        mixer, session_id = initialized_session
        
        skip_type = mixer.record_playback(
            session_id=session_id,
            song_id="song_1",
            duration_played_ms=180000,
            total_duration_ms=180000,
            was_skipped=False
        )
        
        assert skip_type == SkipType.NONE
        
        stats = mixer.get_session(session_id)
        assert stats.total_played == 1
        assert stats.skip_streak == 0
    
    def test_record_skip_updates_streak(self, initialized_session):
        """Test that skips update the streak counter."""
        mixer, session_id = initialized_session
        
        mixer.record_playback(session_id, "song_1", 10000, 180000, True)
        mixer.record_playback(session_id, "song_2", 10000, 180000, True)
        
        stats = mixer.get_session(session_id)
        assert stats.skip_streak == 2
    
    def test_completion_resets_streak(self, initialized_session):
        """Test that completing a song resets skip streak."""
        mixer, session_id = initialized_session
        
        mixer.record_playback(session_id, "song_1", 10000, 180000, True)
        mixer.record_playback(session_id, "song_2", 10000, 180000, True)
        mixer.record_playback(session_id, "song_3", 180000, 180000, False)  # Complete
        
        stats = mixer.get_session(session_id)
        assert stats.skip_streak == 0
    
    def test_last_good_anchor_updated(self, initialized_session):
        """Test that last good anchor is updated on completion."""
        mixer, session_id = initialized_session
        
        # Add anchor and complete it
        mixer.add_anchor(session_id, "song_1", "Test", "Artist")
        mixer.record_playback(session_id, "song_1", 180000, 180000, False)
        
        stats = mixer.get_session(session_id)
        assert stats.last_good_anchor is not None
        assert stats.last_good_anchor.song_id == "song_1"


# =============================================================================
# Recovery State Machine Tests
# =============================================================================

class TestRecoveryStateMachine:
    """Tests for the recovery state machine."""
    
    def test_initial_state_normal(self, initialized_session):
        """Test that initial state is NORMAL."""
        mixer, session_id = initialized_session
        assert mixer.get_recovery_state(session_id) == RecoveryState.NORMAL
    
    def test_transition_to_caution(self, initialized_session):
        """Test transition to CAUTION state (2 skips)."""
        mixer, session_id = initialized_session
        
        mixer.record_playback(session_id, "s1", 60000, 180000, True)
        mixer.record_playback(session_id, "s2", 60000, 180000, True)
        
        assert mixer.get_recovery_state(session_id) == RecoveryState.CAUTION
    
    def test_transition_to_recovery(self, initialized_session):
        """Test transition to RECOVERY state (3 skips)."""
        mixer, session_id = initialized_session
        
        for i in range(3):
            mixer.record_playback(session_id, f"s{i}", 60000, 180000, True)
        
        assert mixer.get_recovery_state(session_id) == RecoveryState.RECOVERY
    
    def test_transition_to_panic(self, initialized_session):
        """Test transition to PANIC state (5 skips)."""
        mixer, session_id = initialized_session
        
        for i in range(5):
            mixer.record_playback(session_id, f"s{i}", 60000, 180000, True)
        
        assert mixer.get_recovery_state(session_id) == RecoveryState.PANIC
    
    def test_early_skip_panic(self, initialized_session):
        """Test panic from early skips (4 early skips)."""
        mixer, session_id = initialized_session
        
        for i in range(4):
            mixer.record_playback(session_id, f"s{i}", 5000, 180000, True)  # All early
        
        assert mixer.get_recovery_state(session_id) == RecoveryState.PANIC
    
    def test_completion_resets_to_normal(self, initialized_session):
        """Test that completion resets state to NORMAL."""
        mixer, session_id = initialized_session
        
        # Get to RECOVERY
        for i in range(3):
            mixer.record_playback(session_id, f"s{i}", 60000, 180000, True)
        
        assert mixer.get_recovery_state(session_id) == RecoveryState.RECOVERY
        
        # Complete a song
        mixer.record_playback(session_id, "good", 180000, 180000, False)
        
        assert mixer.get_recovery_state(session_id) == RecoveryState.NORMAL


# =============================================================================
# Confidence Management Tests
# =============================================================================

class TestConfidenceManagement:
    """Tests for session confidence tracking."""
    
    def test_initial_confidence_zero(self, initialized_session):
        """Test that initial confidence is 0."""
        mixer, session_id = initialized_session
        assert mixer.get_confidence(session_id) == 0.0
    
    def test_queued_tracks_boost_confidence(self, initialized_session):
        """Test that queued tracks increase confidence."""
        mixer, session_id = initialized_session
        
        mixer.add_anchor(session_id, "s1", "Song", "Artist")
        
        assert mixer.get_confidence(session_id) > 0.0
    
    def test_completions_boost_confidence(self, initialized_session):
        """Test that completions increase confidence."""
        mixer, session_id = initialized_session
        
        initial = mixer.get_confidence(session_id)
        mixer.record_playback(session_id, "s1", 180000, 180000, False)
        
        assert mixer.get_confidence(session_id) > initial
    
    def test_skips_penalize_confidence(self, initialized_session):
        """Test that skips decrease confidence."""
        mixer, session_id = initialized_session
        
        # Start with some confidence
        mixer.add_anchor(session_id, "s1", "Song", "Artist")
        initial = mixer.get_confidence(session_id)
        
        mixer.record_playback(session_id, "s2", 60000, 180000, True)
        
        assert mixer.get_confidence(session_id) < initial
    
    def test_confidence_clamped(self, initialized_session):
        """Test that confidence is clamped to [MIN, MAX]."""
        mixer, session_id = initialized_session
        
        # Many completions
        for i in range(20):
            mixer.record_playback(session_id, f"s{i}", 180000, 180000, False)
        
        assert mixer.get_confidence(session_id) <= mixer.MAX_CONFIDENCE
        
        # Many skips
        for i in range(20, 50):
            mixer.record_playback(session_id, f"s{i}", 60000, 180000, True)
        
        assert mixer.get_confidence(session_id) >= mixer.MIN_CONFIDENCE


# =============================================================================
# Adaptive Weights Tests
# =============================================================================

class TestAdaptiveWeights:
    """Tests for adaptive source weight calculation."""
    
    def test_weights_sum_to_one(self, initialized_session):
        """Test that weights sum to approximately 1.0."""
        mixer, session_id = initialized_session
        
        weights = mixer.get_source_weights(session_id)
        total = sum(weights.values())
        
        assert 0.99 <= total <= 1.01
    
    def test_high_confidence_favors_hot(self, initialized_session):
        """Test that high confidence increases hot pool weight."""
        mixer, session_id = initialized_session
        
        # Build high confidence
        for i in range(10):
            mixer.add_anchor(session_id, f"s{i}", f"Song {i}", "Artist")
            mixer.record_playback(session_id, f"s{i}", 180000, 180000, False)
        
        weights = mixer.get_source_weights(session_id)
        
        # Hot should be significant
        assert weights["hot"] >= 0.3
    
    def test_panic_mode_minimizes_hot(self, initialized_session):
        """Test that panic mode minimizes hot pool."""
        mixer, session_id = initialized_session
        
        # Trigger panic
        for i in range(5):
            mixer.record_playback(session_id, f"s{i}", 10000, 180000, True)
        
        weights = mixer.get_source_weights(session_id)
        
        assert weights["hot"] < weights["warm"]
    
    def test_early_session_conservative(self, initialized_session):
        """Test that early session (< 3 songs) is conservative."""
        mixer, session_id = initialized_session
        
        # Only 1 song played
        mixer.record_playback(session_id, "s1", 180000, 180000, False)
        
        weights = mixer.get_source_weights(session_id)
        
        # Warm should dominate in early phase
        assert weights["warm"] > weights["hot"]
    
    def test_transition_weight_increases(self, initialized_session):
        """Test that transition weight increases in recovery states."""
        mixer, session_id = initialized_session
        
        normal_weight = mixer.get_transition_weight(session_id)
        
        # Trigger recovery
        for i in range(3):
            mixer.record_playback(session_id, f"s{i}", 60000, 180000, True)
        
        recovery_weight = mixer.get_transition_weight(session_id)
        
        assert recovery_weight > normal_weight


# =============================================================================
# Fallback Strategy Tests
# =============================================================================

class TestFallbackStrategies:
    """Tests for panic mode fallback strategies."""
    
    def test_fallback_with_anchor(self, initialized_session):
        """Test fallback strategy when anchors exist."""
        mixer, session_id = initialized_session
        
        mixer.add_anchor(session_id, "s1", "Song", "Artist")
        
        strategy = mixer.get_safe_fallback_strategy(session_id)
        assert strategy == "anchor_popular"
    
    def test_fallback_with_last_good(self, initialized_session):
        """Test fallback strategy when last good anchor exists."""
        mixer, session_id = initialized_session
        
        # Complete a song to set last_good_anchor
        mixer.add_anchor(session_id, "s1", "Song", "Artist")
        mixer.record_playback(session_id, "s1", 180000, 180000, False)
        
        strategy = mixer.get_safe_fallback_strategy(session_id)
        assert strategy == "best_session"
    
    def test_fallback_no_data(self, initialized_session):
        """Test fallback strategy with no data."""
        mixer, session_id = initialized_session
        
        strategy = mixer.get_safe_fallback_strategy(session_id)
        assert strategy == "hard_pivot"


# =============================================================================
# Session Stats Tests
# =============================================================================

class TestSessionStats:
    """Tests for session statistics."""
    
    def test_completion_rate_calculation(self, initialized_session):
        """Test completion rate calculation."""
        mixer, session_id = initialized_session
        
        # 8 completions, 2 skips = 80%
        for i in range(8):
            mixer.record_playback(session_id, f"c{i}", 180000, 180000, False)
        for i in range(2):
            mixer.record_playback(session_id, f"s{i}", 60000, 180000, True)
        
        stats = mixer.get_session(session_id)
        assert abs(stats.completion_rate - 0.8) < 0.01
    
    def test_early_skip_rate(self, initialized_session):
        """Test early skip rate calculation."""
        mixer, session_id = initialized_session
        
        # 2 early skips out of 5 songs
        mixer.record_playback(session_id, "e1", 5000, 180000, True)   # Early
        mixer.record_playback(session_id, "e2", 5000, 180000, True)   # Early
        mixer.record_playback(session_id, "n1", 60000, 180000, True)  # Normal
        mixer.record_playback(session_id, "c1", 180000, 180000, False)
        mixer.record_playback(session_id, "c2", 180000, 180000, False)
        
        stats = mixer.get_session(session_id)
        assert abs(stats.early_skip_rate - 0.4) < 0.01  # 2/5
    
    def test_get_session_stats(self, initialized_session):
        """Test getting full session statistics."""
        mixer, session_id = initialized_session
        
        mixer.add_anchor(session_id, "a1", "Song", "Artist")
        mixer.record_playback(session_id, "s1", 180000, 180000, False)
        
        stats = mixer.get_session_stats(session_id)
        
        assert "total_played" in stats
        assert "completion_rate" in stats
        assert "recovery_state" in stats
        assert "confidence" in stats
        assert stats["anchor_count"] == 1


# =============================================================================
# Transition Features Tests
# =============================================================================

class TestTransitionFeatures:
    """Tests for transition feature calculations."""
    
    def test_feature_distance_same(self):
        """Test distance between identical features."""
        f1 = TransitionFeatures(bpm=120, energy=0.5, key="C")
        f2 = TransitionFeatures(bpm=120, energy=0.5, key="C")
        
        assert f1.distance_to(f2) == 0.0
    
    def test_feature_distance_bpm(self):
        """Test distance with BPM difference."""
        f1 = TransitionFeatures(bpm=120, energy=0.5, key="C")
        f2 = TransitionFeatures(bpm=180, energy=0.5, key="C")  # +60 BPM
        
        distance = f1.distance_to(f2)
        assert distance > 0.0
    
    def test_feature_distance_key_change(self):
        """Test distance with key change."""
        f1 = TransitionFeatures(bpm=120, energy=0.5, key="C")
        f2 = TransitionFeatures(bpm=120, energy=0.5, key="F#")
        
        distance = f1.distance_to(f2)
        assert distance > 0.0


# =============================================================================
# Should Use Extended Tests
# =============================================================================

class TestShouldUseExtended:
    """Tests for extended pool usage."""
    
    def test_no_extended_in_normal(self, initialized_session):
        """Test that extended is not used in normal state."""
        mixer, session_id = initialized_session
        
        assert not mixer.should_use_extended(session_id)
    
    def test_extended_on_high_late_skip(self, initialized_session):
        """Test extended used when late skip rate is high."""
        mixer, session_id = initialized_session
        
        # Many late skips = boredom
        for i in range(5):
            mixer.record_playback(session_id, f"s{i}", 150000, 180000, True)  # ~83%
        
        # High late skip rate should trigger extended
        stats = mixer.get_session(session_id)
        assert stats.late_skip_rate > 0.4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
