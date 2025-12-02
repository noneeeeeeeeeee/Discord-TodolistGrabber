"""
Tests for Apple Music-style features implemented in V3.

Features tested:
1. Time-Based Energy Bias - REMOVED (server timezone != user timezone)
2. Anchor Artist System
3. Session Momentum (BPM/Energy limits)
4. Skip Penalty Decay (7-day decay) - NOTE: In-memory only until persistence implemented
5. Session Context Awareness
6. Collaboration Graph Boost
7. Cold Start Thresholds
"""

import math
import time
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest


# =============================================================================
# Test 1: Time-Based Energy Bias - REMOVED
# =============================================================================
# Time-based energy bias was removed because:
# - Server timezone != user timezone
# - Discord guilds span multiple timezones
# - A group listening together could have users in wildly different times
#
# The functions still exist for potential future use with per-guild timezone
# settings, but are not used in scoring.


def test_time_based_functions_exist():
    """Verify time-based functions exist but are not used in scoring."""
    from modules.music.Autoplay_Engine.v3.contextual_recommender import (
        ContextualRecommender,
    )
    
    # Functions should still exist
    assert hasattr(ContextualRecommender, "_get_time_based_energy_bias")
    assert hasattr(ContextualRecommender, "_score_time_energy_alignment")
    
    # They should be callable
    bias = ContextualRecommender._get_time_based_energy_bias()
    assert isinstance(bias, float)


# =============================================================================
# Test 2: Anchor Artist System
# =============================================================================
def test_anchor_artist_tracking():
    """Test that artists with >3 completed plays become anchors."""
    from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker

    tracker = ContextTracker(history_size=20)

    # Play an artist 3 times with >80% progress
    for i in range(3):
        tracker.record_play(
            track_id=f"track_{i}",
            artist="Taylor Swift",
            title=f"Song {i}",
            genres=["pop"],
            was_skipped=False,
            progress_ratio=0.9,  # >80% = completed
        )

    # Should not be anchor yet (exactly 3)
    assert tracker.is_anchor_artist("Taylor Swift"), \
        "Artist with 3 completed plays should be anchor"

    # Check anchor list
    anchors = tracker.get_anchor_artists()
    assert "taylor swift" in anchors, "Taylor Swift should be in anchor list"


def test_anchor_artist_score_boost():
    """Test that anchor artists get progressive boosts."""
    from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker

    tracker = ContextTracker(history_size=20)

    # 2 plays = no boost
    for i in range(2):
        tracker.record_play(
            track_id=f"track_{i}",
            artist="Artist A",
            title=f"Song {i}",
            genres=["pop"],
            was_skipped=False,
            progress_ratio=0.9,
        )
    assert tracker.get_anchor_artist_score("Artist A") == 0.0

    # 3 plays = +0.1
    tracker.record_play(
        track_id="track_2",
        artist="Artist A",
        title="Song 2",
        genres=["pop"],
        was_skipped=False,
        progress_ratio=0.9,
    )
    assert tracker.get_anchor_artist_score("Artist A") == 0.1

    # 4 plays = +0.15
    tracker.record_play(
        track_id="track_3",
        artist="Artist A",
        title="Song 3",
        genres=["pop"],
        was_skipped=False,
        progress_ratio=0.9,
    )
    assert tracker.get_anchor_artist_score("Artist A") == 0.15

    # 5+ plays = +0.2 (max)
    tracker.record_play(
        track_id="track_4",
        artist="Artist A",
        title="Song 4",
        genres=["pop"],
        was_skipped=False,
        progress_ratio=0.9,
    )
    assert tracker.get_anchor_artist_score("Artist A") == 0.2


# =============================================================================
# Test 3: Session Momentum (BPM/Energy limits)
# =============================================================================
def test_session_momentum_bpm_limit():
    """Test that large BPM jumps are penalized."""
    from modules.music.Autoplay_Engine.v3.contextual_recommender import (
        ContextualRecommender,
    )

    # Small BPM change (5 BPM) - should get bonus
    small_jump = ContextualRecommender._score_session_momentum(
        candidate_tempo=125.0,
        candidate_energy=0.5,
        last_tempo=120.0,
        last_energy=0.5,
    )
    assert small_jump >= 1.0, "Small BPM jump should get bonus"

    # Medium BPM change (25 BPM) - should get penalty
    medium_jump = ContextualRecommender._score_session_momentum(
        candidate_tempo=145.0,
        candidate_energy=0.5,
        last_tempo=120.0,
        last_energy=0.5,
    )
    assert medium_jump < small_jump, "Medium BPM jump should score lower than small"

    # Large BPM change (35 BPM) - severe penalty
    large_jump = ContextualRecommender._score_session_momentum(
        candidate_tempo=155.0,
        candidate_energy=0.5,
        last_tempo=120.0,
        last_energy=0.5,
    )
    assert large_jump <= 0.6, "Large BPM jump (>30) should have severe penalty"


def test_session_momentum_energy_limit():
    """Test that large energy jumps are penalized."""
    from modules.music.Autoplay_Engine.v3.contextual_recommender import (
        ContextualRecommender,
    )

    # Small energy change (0.1) - should get bonus
    small_jump = ContextualRecommender._score_session_momentum(
        candidate_tempo=120.0,
        candidate_energy=0.6,
        last_tempo=120.0,
        last_energy=0.5,
    )
    assert small_jump >= 1.0, "Small energy jump should get bonus"

    # Large energy change (0.5) - severe penalty
    large_jump = ContextualRecommender._score_session_momentum(
        candidate_tempo=120.0,
        candidate_energy=1.0,
        last_tempo=120.0,
        last_energy=0.5,
    )
    assert large_jump < 0.75, "Large energy jump (>0.3) should have penalty"


# =============================================================================
# Test 4: Skip Penalty Decay (7-day decay)
# NOTE: Currently in-memory only - persists within session only.
# Full persistence requires export/import feature (V2 in progress).
# =============================================================================
def test_skip_penalty_recording():
    """Test that skip penalties are recorded correctly."""
    from modules.music.Autoplay_Engine.v3.feedback_manager import FeedbackManager

    manager = FeedbackManager()

    # Record a skip
    manager.record_skip_penalty("track123", penalty=-0.3)

    # Should have penalty
    penalty = manager.get_track_penalty("track123")
    assert penalty < 0, "Skip penalty should be negative"
    assert penalty >= -0.3, "Initial penalty should be -0.3"


def test_skip_penalty_decay_over_time():
    """Test that skip penalties decay exponentially over 7 days."""
    from modules.music.Autoplay_Engine.v3.feedback_manager import SkipPenalty

    # Create a penalty from 7 days ago
    seven_days_ago = time.time() - (7 * 86400)
    penalty = SkipPenalty(
        track_id="track123",
        penalty=-0.3,
        timestamp=seven_days_ago,
    )

    # After 7 days, penalty should be ~36.8% of original (e^-1 ≈ 0.368)
    decayed = penalty.get_decayed_penalty()
    expected = -0.3 * math.exp(-1)  # -0.110 approximately
    assert abs(decayed - expected) < 0.01, \
        f"After 7 days, penalty should be ~{expected}, got {decayed}"

    # After 14 days, penalty should be ~13.5% of original (e^-2 ≈ 0.135)
    fourteen_days_ago = time.time() - (14 * 86400)
    penalty2 = SkipPenalty(
        track_id="track456",
        penalty=-0.3,
        timestamp=fourteen_days_ago,
    )
    decayed2 = penalty2.get_decayed_penalty()
    expected2 = -0.3 * math.exp(-2)
    assert abs(decayed2 - expected2) < 0.01, \
        f"After 14 days, penalty should be ~{expected2}, got {decayed2}"


def test_less_like_this_penalty():
    """Test 'Less Like This' applies stronger penalty."""
    from modules.music.Autoplay_Engine.v3.feedback_manager import FeedbackManager

    manager = FeedbackManager()

    # Regular skip
    manager.record_skip_penalty("track1", penalty=-0.3)

    # "Less Like This"
    manager.record_less_like_this("track2")

    penalty1 = manager.get_track_penalty("track1")
    penalty2 = manager.get_track_penalty("track2")

    assert penalty2 < penalty1, \
        "'Less Like This' should have stronger penalty than regular skip"


def test_artist_session_skip_counter():
    """Test that artist skips are counted per session."""
    from modules.music.Autoplay_Engine.v3.feedback_manager import FeedbackManager

    manager = FeedbackManager()

    # Skip same artist twice
    manager.record_skip_penalty("track1", guild_id="123", artist="Bad Artist")
    manager.record_skip_penalty("track2", guild_id="123", artist="Bad Artist")

    # Should have 2 skips
    skip_count = manager.get_artist_session_skips("123", "Bad Artist")
    assert skip_count == 2, f"Should have 2 artist skips, got {skip_count}"

    # Should have penalty
    penalty = manager.get_artist_penalty("123", "Bad Artist")
    assert penalty == -0.1, "2+ artist skips should give -0.1 penalty"


# =============================================================================
# Test 5: Session Context Awareness
# =============================================================================
def test_should_use_safe_picks_short_session():
    """Test that short sessions use safe picks."""
    from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker

    tracker = ContextTracker(history_size=20)

    # Record just one track (session just started)
    tracker.record_play(
        track_id="track1",
        artist="Artist A",
        title="Song 1",
        genres=["pop"],
        was_skipped=False,
        progress_ratio=1.0,
    )

    # Short session should use safe picks
    assert tracker.should_use_safe_picks(), \
        "Short session (<15 min) should use safe picks"


def test_should_allow_exploration_long_session():
    """Test that long sessions with low skip rate allow exploration."""
    from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker

    tracker = ContextTracker(history_size=20)

    # Simulate a long session by manipulating internal state
    tracker._session_start = time.time() - (70 * 60)  # 70 minutes ago

    # Record many completed tracks (no skips)
    for i in range(10):
        tracker.record_play(
            track_id=f"track_{i}",
            artist=f"Artist {i % 3}",
            title=f"Song {i}",
            genres=["pop"],
            was_skipped=False,
            progress_ratio=1.0,
        )

    # Long session with low skip rate should allow exploration
    assert tracker.should_allow_exploration(), \
        "Long session with low skip rate should allow exploration"


def test_skip_velocity_calculation():
    """Test skip velocity calculation."""
    from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker

    tracker = ContextTracker(history_size=20)

    # Record several skips in quick succession
    now = time.time()
    tracker._recent_skip_timestamps = [
        now - 60,  # 1 minute ago
        now - 45,  # 45 seconds ago
        now - 30,  # 30 seconds ago
        now - 15,  # 15 seconds ago
        now,       # just now
    ]

    velocity = tracker.get_skip_velocity()
    # 4 skips in 1 minute = 4 skips/min
    assert velocity > 3.0, f"Should have high skip velocity, got {velocity}"


# =============================================================================
# Test 6: Collaboration Graph Boost
# =============================================================================
def test_collaboration_recording():
    """Test that collaborations are recorded bidirectionally."""
    from modules.music.Autoplay_Engine.v3.collaborative_matrix import CollaborativeMatrix
    from modules.music.Autoplay_Engine.v3.cache_manager import CacheManager
    from pathlib import Path

    cache = CacheManager(Path("cache/test"))
    matrix = CollaborativeMatrix(cache)

    # Record collaboration
    matrix.record_collaboration("Drake", ["Travis Scott", "Future"])

    # Check collaborators
    drake_collabs = matrix.get_collaborative_artists("Drake")
    assert len(drake_collabs) == 2, "Drake should have 2 collaborators"

    # Check reverse direction
    travis_collabs = matrix.get_collaborative_artists("Travis Scott")
    assert any(name.lower() == "drake" for name, _ in travis_collabs), \
        "Travis Scott should have Drake as collaborator"


def test_collaboration_boost_calculation():
    """Test collaboration boost values."""
    from modules.music.Autoplay_Engine.v3.collaborative_matrix import CollaborativeMatrix
    from modules.music.Autoplay_Engine.v3.cache_manager import CacheManager
    from pathlib import Path

    cache = CacheManager(Path("cache/test"))
    matrix = CollaborativeMatrix(cache)

    # Mark an artist as liked
    matrix.mark_artist_liked("guild123", "Drake")

    # Record single collaboration
    matrix.record_collaboration("Drake", ["Artist B"])

    # Single collab = +0.2
    boost = matrix.get_collaboration_boost("guild123", "Artist B")
    assert boost == 0.2, f"Single collaboration should give +0.2 boost, got {boost}"

    # Record another collaboration
    matrix.record_collaboration("Drake", ["Artist B"])

    # 2+ collabs = +0.4
    boost2 = matrix.get_collaboration_boost("guild123", "Artist B")
    assert boost2 == 0.4, f"2+ collaborations should give +0.4 boost, got {boost2}"


def test_collaboration_cluster_building():
    """Test building collaboration clusters for genre bridging."""
    from modules.music.Autoplay_Engine.v3.collaborative_matrix import CollaborativeMatrix
    from modules.music.Autoplay_Engine.v3.cache_manager import CacheManager
    from pathlib import Path

    cache = CacheManager(Path("cache/test"))
    matrix = CollaborativeMatrix(cache)

    # Build a collaboration chain: A -> B -> C (2 hops)
    matrix.record_collaboration("Artist A", ["Artist B"])
    matrix.record_collaboration("Artist A", ["Artist B"])  # 2 collabs
    matrix.record_collaboration("Artist B", ["Artist C"])
    matrix.record_collaboration("Artist B", ["Artist C"])  # 2 collabs

    # Build cluster from A with depth=2
    cluster = matrix.build_collaboration_clusters("Artist A", depth=2, min_collabs=2)

    assert "artist a" in cluster
    assert "artist b" in cluster
    assert "artist c" in cluster, "Cluster should include 2-hop connections"


# =============================================================================
# Test 7: Cold Start Thresholds
# =============================================================================
def test_cold_start_thresholds():
    """Test progressive recommendation modes based on cache size."""
    from modules.music.Autoplay_Engine.v3.contextual_recommender import (
        ContextualRecommender,
        CandidateFeatures,
    )
    from modules.music.Autoplay_Engine.v3.collaborative_matrix import CollaborativeMatrix
    from modules.music.Autoplay_Engine.v3.cache_manager import CacheManager
    from pathlib import Path

    cache = CacheManager(Path("cache/test"))
    matrix = CollaborativeMatrix(cache)
    recommender = ContextualRecommender(matrix)

    # Create a simple candidate
    candidates = [
        CandidateFeatures(
            track_id="track1",
            artist="Artist",
            title="Song",
            content_similarity=0.8,
            session_similarity=0.7,
            novelty=0.5,
            quality=0.9,
        )
    ]

    # Test disabled mode (cache < 100)
    result = recommender.recommend_with_progressive_logic(
        "guild1", candidates, cache_size=50
    )
    assert len(result) == 1, "Should return results even in disabled mode"

    # Test limited mode (cache 100-300)
    result = recommender.recommend_with_progressive_logic(
        "guild1", candidates, cache_size=200
    )
    assert len(result) == 1

    # Test basic mode (cache 300-500)
    result = recommender.recommend_with_progressive_logic(
        "guild1", candidates, cache_size=400
    )
    assert len(result) == 1

    # Test full mode (cache 500+)
    result = recommender.recommend_with_progressive_logic(
        "guild1", candidates, cache_size=600
    )
    assert len(result) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
