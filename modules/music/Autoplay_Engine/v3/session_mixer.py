"""
V3 Autoplay Engine - Session Confidence Mixer

Adaptive recommendation weights based on session confidence and satisfaction.
Replaces fixed pool ratios with dynamic weights derived from:
- Session confidence (queued tracks, completion rate)
- Satisfaction metrics (skip patterns, transition success)
- Recovery state (Normal/Caution/Recovery/Panic)

This is the "Apple Music-like" adaptive intelligence layer.
"""

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .constants import SessionState, SongMetadata

logger = logging.getLogger(__name__)


class RecoveryState(Enum):
    """State machine for handling skip streaks."""
    NORMAL = "normal"
    CAUTION = "caution"      # 2 skips
    RECOVERY = "recovery"    # 3 skips OR 3 early skips
    PANIC = "panic"          # 5 skips OR 4 early skips


class SkipType(Enum):
    """Classification of skip behavior."""
    EARLY = "early"          # < 15s or < 10%
    TRANSITION = "transition" # Early skip + big feature jump
    LATE = "late"            # > 70% played (boredom)
    NORMAL = "normal"        # Skip between 10-70%
    NONE = "none"            # Not a skip (completed)


@dataclass
class SessionAnchor:
    """A queued song that anchors the session taste profile."""
    song_id: str
    title: str
    artist: str
    genres: list[str] = field(default_factory=list)
    bpm: float = 120.0
    energy: float = 0.5
    key: Optional[str] = None
    queued_at: float = field(default_factory=time.time)
    weight: float = 1.0  # Decays over time, boosted if queued again
    
    @property
    def recency_weight(self) -> float:
        """Calculate recency-adjusted weight."""
        hours_since = (time.time() - self.queued_at) / 3600
        decay = math.exp(-0.693 * hours_since / 2)  # 2-hour half-life
        return self.weight * decay


@dataclass
class TransitionFeatures:
    """Audio features for transition scoring."""
    bpm: float = 120.0
    energy: float = 0.5
    key: Optional[str] = None
    loudness: float = -10.0
    
    def distance_to(self, other: "TransitionFeatures") -> float:
        """Calculate feature distance to another track."""
        if other is None:
            return 0.0
        
        # Normalize components
        bpm_diff = abs(self.bpm - other.bpm) / 60.0  # ~60 BPM range
        energy_diff = abs(self.energy - other.energy)
        loudness_diff = abs(self.loudness - other.loudness) / 20.0
        
        # Key distance (simplified - could use Camelot wheel)
        key_penalty = 0.0 if self.key == other.key else 0.3
        
        return (bpm_diff + energy_diff + loudness_diff + key_penalty) / 4.0


@dataclass
class SessionStats:
    """Rolling window statistics for a session."""
    # Window size
    window_size: int = 8
    
    # Completion tracking
    completions: deque = field(default_factory=lambda: deque(maxlen=8))
    skip_types: deque = field(default_factory=lambda: deque(maxlen=8))
    
    # Streak tracking
    skip_streak: int = 0
    early_skip_streak: int = 0
    transition_skip_streak: int = 0
    
    # Transition tracking
    last_track_features: Optional[TransitionFeatures] = None
    transition_skips: int = 0  # Rolling count
    
    # Queued tracks (strong signal)
    queued_count: int = 0
    
    # Track count
    total_played: int = 0
    
    # Last good anchor
    last_good_anchor: Optional[SessionAnchor] = None
    
    @property
    def completion_rate(self) -> float:
        """Calculate rolling completion rate."""
        if not self.completions:
            return 0.0
        return sum(self.completions) / len(self.completions)
    
    @property
    def early_skip_rate(self) -> float:
        """Calculate early skip rate in window."""
        if not self.skip_types:
            return 0.0
        early_count = sum(1 for s in self.skip_types if s == SkipType.EARLY)
        return early_count / len(self.skip_types)
    
    @property
    def late_skip_rate(self) -> float:
        """Calculate late skip rate (boredom indicator)."""
        if not self.skip_types:
            return 0.0
        late_count = sum(1 for s in self.skip_types if s == SkipType.LATE)
        return late_count / len(self.skip_types)
    
    @property
    def transition_skip_rate(self) -> float:
        """Calculate transition failure rate."""
        if not self.skip_types:
            return 0.0
        trans_count = sum(1 for s in self.skip_types if s == SkipType.TRANSITION)
        return trans_count / len(self.skip_types)


class SessionMixer:
    """
    Adaptive weight mixer for recommendation sources.
    
    Replaces fixed Hot/Warm/Cold/Extended pool ratios with
    dynamic weights based on session confidence and recovery state.
    
    Key concepts:
    - Confidence: How certain are we about the user's taste?
    - Satisfaction: Is the user completing songs?
    - Recovery: Are we in a skip streak?
    
    Source weights adapt to:
    - High confidence + high completion → more Hot (risky picks)
    - Low confidence or many skips → more Warm/Cold (safe picks)
    - Extended is on-demand, not a fixed share
    """
    
    # Thresholds
    EARLY_SKIP_THRESHOLD_MS = 15000  # 15s
    EARLY_SKIP_THRESHOLD_PCT = 0.10  # 10%
    LATE_SKIP_THRESHOLD_PCT = 0.70   # 70%
    TRANSITION_DISTANCE_THRESHOLD = 0.5  # Feature distance for "big jump"
    
    # Skip streak thresholds for state transitions
    CAUTION_THRESHOLD = 2
    RECOVERY_THRESHOLD = 3
    PANIC_THRESHOLD = 5
    EARLY_RECOVERY_THRESHOLD = 3
    EARLY_PANIC_THRESHOLD = 4
    
    # Confidence weights
    QUEUED_TRACK_CONFIDENCE = 0.2    # Each queued track adds this
    COMPLETED_TRACK_CONFIDENCE = 0.1  # Each completion adds this
    SKIP_CONFIDENCE_PENALTY = 0.15    # Each skip removes this
    MAX_CONFIDENCE = 1.0
    MIN_CONFIDENCE = 0.1
    
    # Phase thresholds (first N tracks)
    PHASE_A_TRACKS = 3
    PHASE_B_TRACKS = 5
    
    def __init__(self):
        """Initialize the session mixer."""
        self._sessions: dict[str, SessionStats] = {}
        self._anchors: dict[str, dict[str, SessionAnchor]] = {}  # session_id -> song_id -> anchor
        self._recovery_states: dict[str, RecoveryState] = {}
        self._confidence: dict[str, float] = {}
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize mixer."""
        self._initialized = True
        logger.info("Session mixer initialized")
    
    async def shutdown(self) -> None:
        """Shutdown and clear state."""
        self._sessions.clear()
        self._anchors.clear()
        self._recovery_states.clear()
        self._confidence.clear()
        self._initialized = False
    
    # ========================================================================
    # Session Management
    # ========================================================================
    
    def create_session(self, session_id: str) -> SessionStats:
        """Create a new session stats tracker."""
        stats = SessionStats()
        self._sessions[session_id] = stats
        self._anchors[session_id] = {}
        self._recovery_states[session_id] = RecoveryState.NORMAL
        self._confidence[session_id] = 0.0
        return stats
    
    def get_session(self, session_id: str) -> Optional[SessionStats]:
        """Get session stats."""
        return self._sessions.get(session_id)
    
    def end_session(self, session_id: str) -> None:
        """Clean up session data."""
        self._sessions.pop(session_id, None)
        self._anchors.pop(session_id, None)
        self._recovery_states.pop(session_id, None)
        self._confidence.pop(session_id, None)
    
    # ========================================================================
    # Anchor Management (Queued Tracks = Strong Signal)
    # ========================================================================
    
    def add_anchor(
        self,
        session_id: str,
        song_id: str,
        title: str,
        artist: str,
        metadata: Optional[SongMetadata] = None
    ) -> SessionAnchor:
        """
        Add or boost a session anchor from a queued track.
        
        Queued tracks are the strongest signal of user intent.
        """
        if session_id not in self._anchors:
            self._anchors[session_id] = {}
        
        anchors = self._anchors[session_id]
        
        # If already an anchor, boost its weight
        if song_id in anchors:
            anchors[song_id].weight += 0.5
            anchors[song_id].queued_at = time.time()
            return anchors[song_id]
        
        # Create new anchor
        anchor = SessionAnchor(
            song_id=song_id,
            title=title,
            artist=artist
        )
        
        # Extract features from metadata if available
        if metadata:
            if metadata.librarian_info:
                anchor.genres = metadata.librarian_info.genres or []
                anchor.energy = metadata.librarian_info.energy_level or 0.5
            if metadata.bpm:
                anchor.bpm = metadata.bpm
            if metadata.musical_key:
                anchor.key = metadata.musical_key
        
        anchors[song_id] = anchor
        
        # Update session stats
        if session_id in self._sessions:
            self._sessions[session_id].queued_count += 1
        
        # Boost confidence
        self._update_confidence(session_id, self.QUEUED_TRACK_CONFIDENCE)
        
        return anchor
    
    def get_anchors(self, session_id: str) -> list[SessionAnchor]:
        """Get all anchors for a session, sorted by recency weight."""
        if session_id not in self._anchors:
            return []
        
        anchors = list(self._anchors[session_id].values())
        anchors.sort(key=lambda a: a.recency_weight, reverse=True)
        return anchors
    
    def get_taste_vector(self, session_id: str) -> dict[str, Any]:
        """
        Build a taste vector from session anchors.
        
        Returns weighted mix of anchor features.
        """
        anchors = self.get_anchors(session_id)
        if not anchors:
            return {
                "genres": [],
                "avg_bpm": 120.0,
                "avg_energy": 0.5,
                "artists": []
            }
        
        # Weight by recency
        total_weight = sum(a.recency_weight for a in anchors)
        if total_weight == 0:
            total_weight = 1.0
        
        # Weighted genre counts
        genre_weights: dict[str, float] = {}
        for anchor in anchors:
            w = anchor.recency_weight / total_weight
            for genre in anchor.genres:
                genre_weights[genre] = genre_weights.get(genre, 0) + w
        
        # Sort genres by weight
        sorted_genres = sorted(genre_weights.items(), key=lambda x: x[1], reverse=True)
        
        # Weighted averages
        avg_bpm = sum(a.bpm * a.recency_weight for a in anchors) / total_weight
        avg_energy = sum(a.energy * a.recency_weight for a in anchors) / total_weight
        
        # Artist list
        artists = [a.artist for a in anchors[:10]]
        
        return {
            "genres": [g[0] for g in sorted_genres[:5]],
            "genre_weights": dict(sorted_genres[:10]),
            "avg_bpm": avg_bpm,
            "avg_energy": avg_energy,
            "artists": artists
        }
    
    # ========================================================================
    # Skip Classification and Tracking
    # ========================================================================
    
    def classify_skip(
        self,
        session_id: str,
        duration_played_ms: int,
        total_duration_ms: int,
        current_features: Optional[TransitionFeatures] = None
    ) -> SkipType:
        """
        Classify a skip by timing and context.
        
        Returns:
            SkipType indicating the nature of the skip
        """
        if total_duration_ms <= 0:
            return SkipType.NORMAL
        
        play_pct = duration_played_ms / total_duration_ms
        
        # Early skip detection
        is_early = (
            duration_played_ms < self.EARLY_SKIP_THRESHOLD_MS or
            play_pct < self.EARLY_SKIP_THRESHOLD_PCT
        )
        
        # Late skip detection (boredom)
        is_late = play_pct >= self.LATE_SKIP_THRESHOLD_PCT
        
        # Transition failure detection
        stats = self._sessions.get(session_id)
        is_transition_failure = False
        
        if is_early and stats and stats.last_track_features and current_features:
            distance = stats.last_track_features.distance_to(current_features)
            if distance > self.TRANSITION_DISTANCE_THRESHOLD:
                is_transition_failure = True
        
        if is_transition_failure:
            return SkipType.TRANSITION
        elif is_early:
            return SkipType.EARLY
        elif is_late:
            return SkipType.LATE
        else:
            return SkipType.NORMAL
    
    def record_playback(
        self,
        session_id: str,
        song_id: str,
        duration_played_ms: int,
        total_duration_ms: int,
        was_skipped: bool,
        features: Optional[TransitionFeatures] = None
    ) -> SkipType:
        """
        Record a playback event and update session stats.
        
        Returns the skip type if skipped, NONE if completed.
        """
        if session_id not in self._sessions:
            self.create_session(session_id)
        
        stats = self._sessions[session_id]
        stats.total_played += 1
        
        skip_type = SkipType.NONE
        
        if was_skipped:
            # Classify the skip
            skip_type = self.classify_skip(
                session_id,
                duration_played_ms,
                total_duration_ms,
                features
            )
            
            # Update streaks
            stats.skip_streak += 1
            if skip_type == SkipType.EARLY:
                stats.early_skip_streak += 1
            if skip_type == SkipType.TRANSITION:
                stats.transition_skip_streak += 1
                stats.transition_skips += 1
            
            # Record in window
            stats.completions.append(0)
            stats.skip_types.append(skip_type)
            
            # Penalize confidence
            self._update_confidence(session_id, -self.SKIP_CONFIDENCE_PENALTY)
            
        else:
            # Track completed
            stats.skip_streak = 0
            stats.early_skip_streak = 0
            stats.transition_skip_streak = 0
            
            # Record in window
            stats.completions.append(1)
            stats.skip_types.append(SkipType.NONE)
            
            # Boost confidence
            self._update_confidence(session_id, self.COMPLETED_TRACK_CONFIDENCE)
            
            # Update last good anchor
            anchors = self._anchors.get(session_id, {})
            if song_id in anchors:
                stats.last_good_anchor = anchors[song_id]
            elif features:
                # Create anchor from completed song
                stats.last_good_anchor = SessionAnchor(
                    song_id=song_id,
                    title="",  # Would need metadata
                    artist="",
                    bpm=features.bpm,
                    energy=features.energy,
                    key=features.key
                )
        
        # Update last track features
        if features:
            stats.last_track_features = features
        
        # Update recovery state
        self._update_recovery_state(session_id)
        
        return skip_type
    
    # ========================================================================
    # Recovery State Machine
    # ========================================================================
    
    def _update_recovery_state(self, session_id: str) -> RecoveryState:
        """Update recovery state based on skip streaks."""
        stats = self._sessions.get(session_id)
        if not stats:
            return RecoveryState.NORMAL
        
        old_state = self._recovery_states.get(session_id, RecoveryState.NORMAL)
        new_state = RecoveryState.NORMAL
        
        # Check thresholds
        if (stats.skip_streak >= self.PANIC_THRESHOLD or 
            stats.early_skip_streak >= self.EARLY_PANIC_THRESHOLD):
            new_state = RecoveryState.PANIC
        elif (stats.skip_streak >= self.RECOVERY_THRESHOLD or 
              stats.early_skip_streak >= self.EARLY_RECOVERY_THRESHOLD):
            new_state = RecoveryState.RECOVERY
        elif stats.skip_streak >= self.CAUTION_THRESHOLD:
            new_state = RecoveryState.CAUTION
        else:
            new_state = RecoveryState.NORMAL
        
        self._recovery_states[session_id] = new_state
        
        if new_state != old_state:
            logger.info(f"Session {session_id} recovery state: {old_state.value} → {new_state.value}")
        
        return new_state
    
    def get_recovery_state(self, session_id: str) -> RecoveryState:
        """Get current recovery state."""
        return self._recovery_states.get(session_id, RecoveryState.NORMAL)
    
    # ========================================================================
    # Confidence Management
    # ========================================================================
    
    def _update_confidence(self, session_id: str, delta: float) -> float:
        """Update session confidence, clamped to [MIN, MAX]."""
        current = self._confidence.get(session_id, 0.0)
        new_conf = max(self.MIN_CONFIDENCE, min(self.MAX_CONFIDENCE, current + delta))
        self._confidence[session_id] = new_conf
        return new_conf
    
    def get_confidence(self, session_id: str) -> float:
        """Get current session confidence level."""
        return self._confidence.get(session_id, 0.0)
    
    # ========================================================================
    # Adaptive Weight Calculation
    # ========================================================================
    
    def get_source_weights(self, session_id: str) -> dict[str, float]:
        """
        Calculate adaptive weights for recommendation sources.
        
        Returns dict with weights for:
        - hot: High-confidence picks (similar to taste, low risk)
        - warm: Bridge picks (transition-safe, moderate exploration)
        - cold: Exploration picks (controlled diversity)
        - extended: On-demand generator (called when needed)
        
        Weights adapt based on:
        - Session confidence
        - Recovery state
        - Completion rate
        - Phase (early vs established session)
        """
        stats = self._sessions.get(session_id)
        confidence = self._confidence.get(session_id, 0.0)
        recovery = self._recovery_states.get(session_id, RecoveryState.NORMAL)
        
        # Base weights
        weights = {
            "hot": 0.4,
            "warm": 0.35,
            "cold": 0.2,
            "extended": 0.05
        }
        
        if not stats:
            return weights
        
        # Phase adjustment (early session = more conservative)
        if stats.total_played < self.PHASE_A_TRACKS:
            # Phase A: Very conservative, transition-safe
            weights["hot"] = 0.2
            weights["warm"] = 0.7
            weights["cold"] = 0.1
            weights["extended"] = 0.0
        elif stats.total_played < self.PHASE_B_TRACKS:
            # Phase B: Starting to open up
            weights["hot"] = 0.3
            weights["warm"] = 0.5
            weights["cold"] = 0.2
            weights["extended"] = 0.0
        
        # Confidence adjustment
        if confidence > 0.7:
            # High confidence: More risk-taking
            weights["hot"] += 0.2
            weights["cold"] += 0.1
            weights["warm"] -= 0.3
        elif confidence < 0.3:
            # Low confidence: Play it safe
            weights["hot"] -= 0.2
            weights["warm"] += 0.2
        
        # Recovery state adjustment
        if recovery == RecoveryState.CAUTION:
            weights["hot"] -= 0.15
            weights["warm"] += 0.2
            weights["cold"] -= 0.05
        elif recovery == RecoveryState.RECOVERY:
            weights["hot"] = 0.1
            weights["warm"] = 0.8
            weights["cold"] = 0.1
            weights["extended"] = 0.0
        elif recovery == RecoveryState.PANIC:
            # Panic: Only safe picks
            weights["hot"] = 0.0
            weights["warm"] = 0.9
            weights["cold"] = 0.1
            weights["extended"] = 0.0
        
        # Completion rate adjustment
        completion_rate = stats.completion_rate
        if completion_rate > 0.8:
            # High completion: Can take more risks
            weights["hot"] += 0.1
            weights["cold"] += 0.05
        elif completion_rate < 0.3:
            # Low completion: Be safer
            weights["hot"] -= 0.15
            weights["warm"] += 0.15
        
        # Transition skip adjustment
        if stats.transition_skip_rate > 0.3:
            # Too many transition failures: Prioritize smooth transitions
            weights["warm"] += 0.2
            weights["hot"] -= 0.15
            weights["cold"] -= 0.05
        
        # Clamp to non-negative first (some adjustments can push values negative)
        weights = {k: max(0.0, float(v)) for k, v in weights.items()}

        # Normalize weights so they sum to ~1.0
        total = sum(weights.values())
        if total <= 0:
            # Fallback to conservative defaults if everything was clamped to 0
            weights = {"hot": 0.4, "warm": 0.35, "cold": 0.2, "extended": 0.05}
            total = sum(weights.values())

        weights = {k: v / total for k, v in weights.items()}
        return weights
    
    def should_use_extended(self, session_id: str) -> bool:
        """
        Check if we should draw from extended/generator pool.
        
        Extended is on-demand, not a fixed share. Use when:
        - Running out of good candidates
        - In recovery and need fresh content
        - High genre fatigue
        """
        stats = self._sessions.get(session_id)
        recovery = self._recovery_states.get(session_id, RecoveryState.NORMAL)
        
        if not stats:
            return False
        
        # Recovery states need fresh content
        if recovery in (RecoveryState.RECOVERY, RecoveryState.PANIC):
            return stats.skip_streak > 4
        
        # High late skip rate = boredom, need novelty
        if stats.late_skip_rate > 0.4:
            return True
        
        return False
    
    def get_safe_fallback_strategy(self, session_id: str) -> str:
        """
        Get fallback strategy when in panic mode.
        
        Returns:
            "best_session" - Use best-performing tracks this session
            "anchor_popular" - Use popular tracks close to anchors
            "hard_pivot" - Pivot to different anchor cluster
        """
        stats = self._sessions.get(session_id)
        
        if not stats:
            return "anchor_popular"
        
        # If we have a last good anchor, try similar tracks
        if stats.last_good_anchor:
            return "best_session"
        
        # If we have queued tracks, use anchor neighborhood
        if stats.queued_count > 0:
            return "anchor_popular"
        
        # No data: hard pivot (but keep transition-safe)
        return "hard_pivot"
    
    def get_transition_weight(self, session_id: str) -> float:
        """
        Get weight for transition smoothness in scoring.
        
        Higher when transition skips are high or in recovery.
        """
        stats = self._sessions.get(session_id)
        recovery = self._recovery_states.get(session_id, RecoveryState.NORMAL)
        
        base_weight = 0.3
        
        if recovery == RecoveryState.CAUTION:
            base_weight = 0.5
        elif recovery == RecoveryState.RECOVERY:
            base_weight = 0.7
        elif recovery == RecoveryState.PANIC:
            base_weight = 0.9
        
        if stats and stats.transition_skip_rate > 0.3:
            base_weight = min(1.0, base_weight + 0.2)
        
        return base_weight
    
    # ========================================================================
    # Statistics
    # ========================================================================
    
    def get_session_stats(self, session_id: str) -> dict[str, Any]:
        """Get detailed session statistics."""
        stats = self._sessions.get(session_id)
        if not stats:
            return {}
        
        return {
            "total_played": stats.total_played,
            "queued_count": stats.queued_count,
            "completion_rate": stats.completion_rate,
            "early_skip_rate": stats.early_skip_rate,
            "late_skip_rate": stats.late_skip_rate,
            "transition_skip_rate": stats.transition_skip_rate,
            "skip_streak": stats.skip_streak,
            "early_skip_streak": stats.early_skip_streak,
            "confidence": self._confidence.get(session_id, 0.0),
            "recovery_state": self._recovery_states.get(session_id, RecoveryState.NORMAL).value,
            "has_last_good_anchor": stats.last_good_anchor is not None,
            "anchor_count": len(self._anchors.get(session_id, {}))
        }


# Singleton instance
_session_mixer: Optional[SessionMixer] = None


def get_session_mixer() -> SessionMixer:
    """Get global session mixer instance."""
    global _session_mixer
    if _session_mixer is None:
        _session_mixer = SessionMixer()
    return _session_mixer
