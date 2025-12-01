"""
Context Tracker for Autoplay V3 - Dual Vector Architecture (9D)

Tracks recent session history with advanced vibe steering and replay detection.

VIBE VECTORS (5D each):
- current_mood_vector: Weighted average of all recent tracks
- liked_mood_vector: Weighted average of liked tracks (PULL target)
- disliked_mood_vector: Average of skipped/disliked tracks (PUSH away)
  Format: [energy, valence, danceability, acousticness, instrumentalness]

FLOW FEATURES (4D):
- last_loudness, last_tempo, last_key, last_mode from most recent track
  Used for DJ-smooth transitions and harmonic mixing

Features:
- Replay Detection: Resets genre skip streaks, treats as super-like
- Fatigue Skip Detection: Skipped recent song = novelty signal, no genre penalty
- 3-Strike Rule: Genre reaches 3 skips → strong penalty (0.85)
- Vibe Tracking: Maintains last 5 disliked vibe vectors for push logic

Part of Issue #3 - Contextual Arc Recommender
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Union
from datetime import datetime, timedelta
from collections import Counter

LOG = logging.getLogger(__name__)


@dataclass
class PlayedTrack:
    """Record of a played track with context and multi-user feedback
    
    Dual Vector Architecture (9D Total):
    - VIBE VECTOR (5D): mood_vector = [energy, valence, danceability, acousticness, instrumentalness]
    - FLOW VECTOR (4D): computed_loudness, computed_tempo, computed_key, computed_mode
    """

    track_id: str
    artist: str
    title: str
    genres: List[str]
    mood_vector: Optional[
        Union[List[float], Dict]
    ]  # 5D vibe vector or dict with 'vector' key
    mood_label: Optional[str]
    timestamp: float
    was_skipped: bool
    skip_type: Optional[str]  # "hard", "medium", "soft", or None
    progress_ratio: float

    # Multi-user feedback fields
    num_likes: int = 0
    num_dislikes: int = 0
    num_active_listeners: int = 1
    consensus_signal: str = "neutral"  # "liked", "disliked", "weak_like", "neutral"
    recency_weight: float = (
        1.0  # Temporal weighting (1.0 = most recent, decays with age)
    )
    energy: Optional[float] = None  # DEPRECATED: Use computed_loudness instead

    # OST-Aware fields (Phase 0.5)
    track_type: str = "music"  # "music", "ost", "game_soundtrack", "anime_opening"
    primary_entity: Optional[str] = None  # Franchise/show/game name for OST content
    
    # Dual Vector Architecture: Flow Features (for smooth transitions)
    # Dual Vector Architecture: Flow features (4D) for harmonic mixing
    computed_loudness: Optional[float] = None  # Loudness in dB (e.g., -5.883)
    computed_tempo: Optional[float] = None  # BPM as float (e.g., 120.0)
    computed_key: Optional[int] = None  # 0-11 (C=0, C#=1, D=2, ..., B=11)
    computed_mode: Optional[int] = None  # 0=minor, 1=major
    event_type: Optional[str] = None  # "replay", "skip", "finish", "dislike", etc.


@dataclass
class SessionContext:
    """Aggregated session state derived from recent history
    
    Dual Vector Architecture (9D Total):
    - VIBE VECTORS (5D each): current_mood_vector, liked_mood_vector, disliked_mood_vector
      Format: [energy, valence, danceability, acousticness, instrumentalness]
    - FLOW FEATURES (4D): last_loudness, last_tempo, last_key, last_mode
    """

    focus_genres: List[str]  # Most common genres (mode)
    current_mood_vector: Optional[List[float]]  # Average session vibe (5D)
    recent_artists: Set[str]  # Artists played in window
    skip_rate: float  # Ratio of skips in recent window
    consecutive_skips: int  # Current skip streak
    songs_since_novelty: int  # Counter for exploration timing
    session_start: float
    last_activity: float
    disliked_tags: Dict[str, float] = field(default_factory=dict)

    # Vibe steering vectors (5D each)
    liked_mood_vector: Optional[List[float]] = (
        None  # Weighted average of liked tracks (PULL target)
    )
    disliked_mood_vector: Optional[List[float]] = (
        None  # Average of skipped/disliked tracks (PUSH away)
    )
    artist_diversity_pool: List[str] = field(
        default_factory=list
    )  # For enforcing max 3 per artist
    energy_trend: float = (
        0.0  # Rising/falling energy across last 3 tracks (-1.0 to 1.0)
    )
    
    # Flow features (4D) from last played track for transitions
    last_loudness: Optional[float] = None  # Loudness in dB
    last_tempo: Optional[float] = None  # BPM
    last_key: Optional[int] = None  # 0-11 (C=0, C#=1, ..., B=11)
    last_mode: Optional[int] = None  # 0=minor, 1=major


class ContextTracker:
    """
    Maintains rolling history of recently played tracks and derives
    session context for recommendation algorithm.

    Implements the "RecentContext Tracker" component from Issue #3.
    """

    def __init__(
        self,
        *,
        history_size: int = 15,
        verbose: int = 0,
    ):
        """
        Args:
            history_size: Number of recent tracks to maintain (default 15)
            verbose: Verbosity level (0=off, 1=info, 2=debug)
        """
        self._history: List[PlayedTrack] = []
        self._history_size = history_size
        self._verbose = verbose

        # Session state
        self._session_start = datetime.now().timestamp()
        self._last_activity = self._session_start
        self._songs_since_novelty = 0
        
        # Vibe steering state (Dual Vector Architecture)
        self._disliked_vectors: List[List[float]] = []  # Last ~5 disliked/skipped vibe vectors
        self._genre_skip_streaks: Dict[str, int] = {}  # Track skip count per genre (3-strike rule)

        LOG.info("🎯 [ContextTracker] Initialized (window=%d tracks)", history_size)

    @staticmethod
    def _calculate_consensus_signal(
        num_likes: int, num_dislikes: int, num_active_listeners: int
    ) -> str:
        """
        Calculate consensus signal from multi-user feedback.

        This is for CLASSIFICATION only. Safe Anchor and Fast Rollback use
        different, looser filters (num_likes > 0 and dislike_ratio >= 0.4).

        Logic:
        - Solo (n=1): any like='liked', any dislike='disliked'
        - Multi: like_ratio>=0.5='liked', dislike_ratio>=0.4='disliked',
                 likes>dislikes='weak_like', else='neutral'

        Args:
            num_likes: Count of like reactions
            num_dislikes: Count of dislike reactions
            num_active_listeners: Total active (non-AFK) listeners

        Returns:
            Consensus signal: "liked", "disliked", "weak_like", or "neutral"
        """
        if num_active_listeners == 0:
            return "neutral"

        # Solo listener (n=1): definitive signal
        if num_active_listeners == 1:
            if num_likes > 0:
                return "liked"
            elif num_dislikes > 0:
                return "disliked"
            else:
                return "neutral"

        # Multi-user: calculate ratios
        like_ratio = num_likes / num_active_listeners
        dislike_ratio = num_dislikes / num_active_listeners

        # Strong consensus thresholds
        if like_ratio >= 0.5:
            return "liked"
        elif dislike_ratio >= 0.4:
            return "disliked"
        elif num_likes > num_dislikes and num_likes > 0:
            return "weak_like"
        else:
            return "neutral"

    def record_play(
        self,
        *,
        track_id: str,
        artist: str,
        title: str,
        genres: List[str],
        mood_vector: Optional[List[float]] = None,
        mood_label: Optional[str] = None,
        was_skipped: bool = False,
        skip_type: Optional[str] = None,
        progress_ratio: float = 1.0,
        num_likes: int = 0,
        num_dislikes: int = 0,
        num_active_listeners: int = 1,
        track_type: str = "music",
        primary_entity: Optional[str] = None,
        event_type: Optional[str] = None,  # "replay", "skip", "finish", "dislike"
        computed_loudness: Optional[float] = None,
        computed_tempo: Optional[float] = None,
        computed_key: Optional[int] = None,
        computed_mode: Optional[int] = None,
    ) -> None:
        """
        Record a played track in the history with enhanced vibe steering logic.

        Args:
            track_id: Unique identifier
            artist: Artist name
            title: Track title
            genres: List of genre tags
            mood_vector: Optional 5D vibe vector [energy, valence, danceability, acousticness, instrumentalness]
            mood_label: Optional mood label (e.g., "energetic", "mellow")
            was_skipped: Whether track was skipped
            skip_type: If skipped, type: "hard", "medium", or "soft"
            progress_ratio: How much of track was played (0.0-1.0)
            num_likes: Count of like reactions
            num_dislikes: Count of dislike reactions
            num_active_listeners: Total active (non-AFK) listeners
            track_type: Track classification (music/ost/game_soundtrack/anime_opening)
            primary_entity: Franchise/show/game name for OST content
            event_type: Event classification for special handling (replay, skip, finish, dislike)
            computed_loudness: Loudness in dB for transition smoothness
            computed_tempo: BPM for transition smoothness
            computed_key: Key 0-11 for harmonic mixing
            computed_mode: Mode 0=minor, 1=major for harmonic mixing
        """
        now = datetime.now().timestamp()

        # Detect replay: track_id exists in recent history
        is_replay = event_type == "replay" or any(
            track.track_id == track_id for track in self._history[-5:]
        )

        # Detect fatigue skip: track was skipped AND exists in last 5 songs
        is_fatigue_skip = False
        if was_skipped and not is_replay:
            is_fatigue_skip = any(
                track.track_id == track_id for track in self._history[-5:]
            )

        # Calculate consensus signal (may be overridden for replays)
        consensus_signal = self._calculate_consensus_signal(
            num_likes, num_dislikes, num_active_listeners
        )

        # --- REPLAY HANDLING: Treat as super-like ---
        if is_replay:
            # Set consensus to "liked" with maximum weight (100% consensus)
            consensus_signal = "liked"
            num_likes = num_active_listeners  # Everyone "loves" this replay
            num_dislikes = 0
            
            # Reset genre skip streaks for all genres of this track
            for genre in (genres or []):
                genre_lower = genre.lower()
                if genre_lower in self._genre_skip_streaks:
                    if self._verbose >= 1:
                        LOG.info(
                            "🔄 [Replay] Resetting skip streak for genre '%s' (was %d skips)",
                            genre_lower,
                            self._genre_skip_streaks[genre_lower],
                        )
                    self._genre_skip_streaks[genre_lower] = 0
            
            if self._verbose >= 1:
                LOG.info(
                    "🔄 [Replay Detected] '%s' by '%s' - Treating as super-like, genres re-anchored",
                    title,
                    artist,
                )

        # Calculate recency weight based on position in history
        history_position = len(self._history)
        recency_weight = max(0.3, 1.0 - (history_position * 0.05))

        # Extract energy from mood_vector for backward compatibility
        energy = None
        if mood_vector:
            if isinstance(mood_vector, dict):
                vec = mood_vector.get("vector")
                if vec and len(vec) > 0:
                    energy = vec[0]  # First dimension is typically energy
            elif isinstance(mood_vector, (list, tuple)) and len(mood_vector) > 0:
                energy = mood_vector[0]

        played = PlayedTrack(
            track_id=track_id,
            artist=artist,
            title=title,
            genres=genres or [],
            mood_vector=mood_vector,
            mood_label=mood_label,
            timestamp=now,
            was_skipped=was_skipped,
            skip_type=skip_type,
            progress_ratio=progress_ratio,
            num_likes=num_likes,
            num_dislikes=num_dislikes,
            num_active_listeners=num_active_listeners,
            consensus_signal=consensus_signal,
            recency_weight=recency_weight,
            energy=energy,
            track_type=track_type,
            primary_entity=primary_entity,
            computed_loudness=computed_loudness,
            computed_tempo=computed_tempo,
            computed_key=computed_key,
            computed_mode=computed_mode,
            event_type=event_type or ("replay" if is_replay else None),
        )

        self._history.append(played)

        # --- VIBE STEERING: Track disliked vectors & genre skip streaks ---
        vec = None
        if mood_vector:
            if isinstance(mood_vector, dict):
                vec = mood_vector.get("vector")
            elif isinstance(mood_vector, (list, tuple)):
                vec = list(mood_vector)

        # Handle skip/dislike feedback
        if (was_skipped or consensus_signal == "disliked") and not is_replay:
            if is_fatigue_skip:
                # Fatigue Skip: Don't increment genre streaks, only track vibe
                if vec:
                    self._disliked_vectors.append(vec)
                    if len(self._disliked_vectors) > 5:
                        self._disliked_vectors.pop(0)
                
                if self._verbose >= 1:
                    LOG.info(
                        "⏭️ [Fatigue Skip] '%s' by '%s' - Novelty signal, no genre penalty",
                        title,
                        artist,
                    )
            else:
                # Normal Skip or Dislike: Increment genre streaks AND track vibe
                for genre in (genres or []):
                    genre_lower = genre.lower()
                    self._genre_skip_streaks[genre_lower] = (
                        self._genre_skip_streaks.get(genre_lower, 0) + 1
                    )
                    
                    if self._verbose >= 2:
                        LOG.debug(
                            "📊 [Genre Skip] '%s' streak now %d/3",
                            genre_lower,
                            self._genre_skip_streaks[genre_lower],
                        )
                
                if vec:
                    self._disliked_vectors.append(vec)
                    if len(self._disliked_vectors) > 5:
                        self._disliked_vectors.pop(0)
                
                if self._verbose >= 1:
                    skip_reason = "explicit dislike" if consensus_signal == "disliked" else "normal skip"
                    LOG.info(
                        "👎 [%s] '%s' by '%s' - Vibe tracked, genre strike +1",
                        skip_reason.title(),
                        title,
                        artist,
                    )

        # Trim to window size
        if len(self._history) > self._history_size:
            self._history.pop(0)

        self._last_activity = now

        if self._verbose >= 1:
            status = "🔄 replay" if is_replay else ("⏭️ skipped" if was_skipped else "✅ finished")
            feedback_str = (
                f"👍{num_likes} 👎{num_dislikes}" if num_active_listeners > 1 else ""
            )
            LOG.info(
                "📊 [Context] Recorded: '%s' by '%s' (%s, %.0f%% played, consensus=%s %s) | History: %d tracks",
                title,
                artist,
                status,
                progress_ratio * 100,
                consensus_signal,
                feedback_str,
                len(self._history),
            )

    def get_context(self) -> SessionContext:
        """
        Derive current session context from recent history.

        Returns:
            SessionContext with aggregated state
        """
        if not self._history:
            return SessionContext(
                focus_genres=[],
                current_mood_vector=None,
                recent_artists=set(),
                skip_rate=0.0,
                consecutive_skips=0,
                songs_since_novelty=self._songs_since_novelty,
                session_start=self._session_start,
                last_activity=self._last_activity,
                liked_mood_vector=None,
                artist_diversity_pool=[],
                energy_trend=0.0,
            )

        # ============================================================
        # Enhanced Context Tracker with Temporal Weighting
        # ============================================================

        # Calculate temporal recency weights for all tracks
        # Most recent track = 1.0, oldest track = 0.3 (minimum)
        recency_weights = []
        for idx in range(len(self._history)):
            position_from_recent = len(self._history) - 1 - idx
            weight = max(0.3, 1.0 - (position_from_recent * 0.05))
            recency_weights.append(weight)

        # Focus genres: most common genres with temporal weighting
        genre_weights: Dict[str, float] = {}
        for idx, track in enumerate(self._history):
            weight = recency_weights[idx]
            for genre in track.genres:
                genre_lower = genre.lower()
                if genre_lower in genre_weights:
                    genre_weights[genre_lower] += weight
                else:
                    genre_weights[genre_lower] = weight

        # Sort by weighted count and take top 3
        focus_genres = [
            g
            for g, _ in sorted(genre_weights.items(), key=lambda x: x[1], reverse=True)[
                :3
            ]
        ]

        # Current mood vector: weighted average with temporal weighting
        mood_vectors = []
        mood_weights = []
        for idx, t in enumerate(self._history):
            if t.mood_vector:
                if isinstance(t.mood_vector, dict):
                    vec = t.mood_vector.get("vector")
                    if vec:
                        mood_vectors.append(vec)
                        mood_weights.append(recency_weights[idx])
                elif isinstance(t.mood_vector, (list, tuple)):
                    mood_vectors.append(t.mood_vector)
                    mood_weights.append(recency_weights[idx])

        current_mood = None
        if mood_vectors and mood_weights:
            # Weighted average each dimension
            dim_count = len(mood_vectors[0])
            total_weight = sum(mood_weights)
            current_mood = [
                sum(v[i] * w for v, w in zip(mood_vectors, mood_weights)) / total_weight
                for i in range(dim_count)
            ]

        # Safe Anchor - liked_mood_vector
        # Filter tracks with ANY likes (num_likes > 0), not just consensus='liked'
        liked_tracks = [
            (idx, t) for idx, t in enumerate(self._history) if t.num_likes > 0
        ]

        liked_mood_vector = None
        if liked_tracks:
            liked_mood_vectors = []
            liked_mood_weights = []
            for idx, track in liked_tracks:
                if track.mood_vector:
                    if isinstance(track.mood_vector, dict):
                        vec = track.mood_vector.get("vector")
                        if vec:
                            liked_mood_vectors.append(vec)
                            liked_mood_weights.append(recency_weights[idx])
                    elif isinstance(track.mood_vector, (list, tuple)):
                        liked_mood_vectors.append(track.mood_vector)
                        liked_mood_weights.append(recency_weights[idx])

            if liked_mood_vectors and liked_mood_weights:
                dim_count = len(liked_mood_vectors[0])
                total_weight = sum(liked_mood_weights)
                liked_mood_vector = [
                    sum(
                        v[i] * w for v, w in zip(liked_mood_vectors, liked_mood_weights)
                    )
                    / total_weight
                    for i in range(dim_count)
                ]

        # Vibe Steering - disliked_mood_vector (from _disliked_vectors tracked in record_play)
        disliked_mood_vector = None
        if self._disliked_vectors:
            dim_count = len(self._disliked_vectors[0])
            total_vecs = len(self._disliked_vectors)
            disliked_mood_vector = [
                sum(v[i] for v in self._disliked_vectors) / total_vecs
                for i in range(dim_count)
            ]

        # Recent artists (for diversity)
        recent_artists = {t.artist for t in self._history}

        # Skip rate (last 10 tracks with temporal weighting)
        recent_window_size = min(10, len(self._history))
        recent_window = self._history[-recent_window_size:]
        recent_window_weights = recency_weights[-recent_window_size:]

        weighted_skips = sum(
            w for t, w in zip(recent_window, recent_window_weights) if t.was_skipped
        )
        total_weight = sum(recent_window_weights)
        skip_rate = weighted_skips / total_weight if total_weight > 0 else 0.0

        # Consecutive skips (from end)
        consecutive_skips = 0
        for track in reversed(self._history):
            if track.was_skipped:
                consecutive_skips += 1
            else:
                break

        # Fast Rollback - disliked tags with temporal-weighted penalties + 3-strike rule
        disliked_tags: Dict[str, float] = {}

        for idx, track in enumerate(self._history):
            # Calculate dislike ratio for multi-user feedback
            if track.num_active_listeners > 0:
                dislike_ratio = track.num_dislikes / track.num_active_listeners
            else:
                dislike_ratio = 0.0

            # Apply penalty if: dislike_ratio >= 0.4 OR track was skipped
            if dislike_ratio >= 0.4 or track.was_skipped:
                # Penalty strength based on skip type or dislike ratio
                if track.was_skipped:
                    # Hard skip = stronger penalty, soft skip = weaker
                    if track.skip_type == "hard":
                        base_penalty = 0.8
                    elif track.skip_type == "medium":
                        base_penalty = 0.6
                    else:  # soft or None
                        base_penalty = 0.5
                else:
                    # Dislike penalty scales with ratio
                    base_penalty = 0.7 * min(1.0, dislike_ratio / 0.4)

                # Apply temporal weighting (recent = stronger)
                penalty = recency_weights[idx] * base_penalty

                # Apply to all genres in this track
                for genre in track.genres:
                    genre_lower = genre.lower()
                    if genre_lower in disliked_tags:
                        # Accumulate penalties, cap at 0.9
                        disliked_tags[genre_lower] = min(
                            0.9, disliked_tags[genre_lower] + penalty
                        )
                    else:
                        disliked_tags[genre_lower] = penalty

        # 3-Strike Rule: Apply STRONG penalties to genres with >=3 skip streaks
        for genre_lower, streak_count in self._genre_skip_streaks.items():
            if streak_count >= 3:
                #强力惩罚 - this genre should be heavily avoided
                disliked_tags[genre_lower] = max(
                    disliked_tags.get(genre_lower, 0.0), 0.85
                )
                if self._verbose >= 2:
                    LOG.debug(
                        "🚫 [3-Strike Rule] Genre '%s' reached %d skips, penalty=0.85",
                        genre_lower,
                        streak_count,
                    )

        # Flow Features: Extract from last played track for harmonic mixing
        last_loudness = None
        last_tempo = None
        last_key = None
        last_mode = None
        if self._history:
            last_track = self._history[-1]
            last_loudness = last_track.computed_loudness
            last_tempo = last_track.computed_tempo
            last_key = last_track.computed_key
            last_mode = last_track.computed_mode

        # Task 2.4: Energy Trend - calculate energy delta from last 3 tracks
        energy_trend = 0.0
        energy_values = []

        for track in self._history[-3:]:  # Last 3 tracks
            if track.energy is not None:
                energy_values.append(track.energy)

        if len(energy_values) >= 2:
            # Energy trend = change from oldest to newest in window
            energy_trend = energy_values[-1] - energy_values[0]

        context = SessionContext(
            focus_genres=focus_genres,
            current_mood_vector=current_mood,
            recent_artists=recent_artists,
            skip_rate=skip_rate,
            consecutive_skips=consecutive_skips,
            songs_since_novelty=self._songs_since_novelty,
            session_start=self._session_start,
            last_activity=self._last_activity,
            disliked_tags=disliked_tags,
            liked_mood_vector=liked_mood_vector,
            disliked_mood_vector=disliked_mood_vector,
            artist_diversity_pool=[],  
            energy_trend=energy_trend,
            last_loudness=last_loudness,
            last_tempo=last_tempo,
            last_key=last_key,
            last_mode=last_mode,
        )

        if self._verbose >= 2:
            LOG.debug(
                "🎯 [Context] Current state: genres=%s (weighted), skip_rate=%.2f (temporal), consecutive_skips=%d, artists=%d, liked_tracks=%d, energy_trend=%.2f",
                focus_genres[:2],
                skip_rate,
                consecutive_skips,
                len(recent_artists),
                len(liked_tracks),
                energy_trend,
            )

        return context

    def increment_novelty_counter(self) -> None:
        """Increment counter for songs since last novelty injection."""
        self._songs_since_novelty += 1

    def reset_novelty_counter(self) -> None:
        """Reset novelty counter after exploration pick."""
        self._songs_since_novelty = 0
        if self._verbose >= 1:
            LOG.info("🔄 [Context] Novelty counter reset (exploration occurred)")

    def should_reset_session(self, *, idle_threshold_minutes: int = 15) -> bool:
        """
        Check if session should be reset due to inactivity.

        Args:
            idle_threshold_minutes: Minutes of inactivity before reset

        Returns:
            True if session should reset
        """
        now = datetime.now().timestamp()
        idle_seconds = now - self._last_activity
        idle_minutes = idle_seconds / 60

        return idle_minutes >= idle_threshold_minutes

    def reset_session(self) -> None:
        """Clear history and reset session state."""
        old_count = len(self._history)
        self._history.clear()
        self._session_start = datetime.now().timestamp()
        self._last_activity = self._session_start
        self._songs_since_novelty = 0

        if self._verbose >= 1:
            LOG.info("🔄 [Context] Session reset (cleared %d tracks)", old_count)

    def get_history(self) -> List[PlayedTrack]:
        """Get readonly copy of play history."""
        return list(self._history)

    def get_artist_play_count(self, artist: str) -> int:
        """Count how many times an artist appears in recent history."""
        return sum(1 for t in self._history if t.artist.lower() == artist.lower())

    def get_track_last_played(self, track_id: str) -> Optional[float]:
        """
        Get timestamp when track was last played, or None if never.

        Args:
            track_id: Track identifier

        Returns:
            Timestamp or None
        """
        for track in reversed(self._history):
            if track.track_id == track_id:
                return track.timestamp
        return None

    def compute_repetition_penalty(
        self,
        track_id: str,
        *,
        tau: int = 6,
    ) -> float:
        """
        Compute soft repetition penalty for a track based on recency.

        Uses exponential decay: penalty = exp(-time_since_play / tau)

        Args:
            track_id: Track to check
            tau: Decay constant (number of songs for significant forgiveness)

        Returns:
            Penalty multiplier (0.0 to 1.0, where 1.0 = no penalty)
        """
        import math

        last_played = self.get_track_last_played(track_id)
        if last_played is None:
            return 1.0

        # Calculate songs since last play
        songs_since = 0
        for track in reversed(self._history):
            if track.timestamp <= last_played:
                break
            songs_since += 1

        if songs_since == 0:
            return 0.01

        # Exponential decay
        penalty = math.exp(-songs_since / tau)

        return 1.0 - penalty

    def get_stats(self) -> Dict:
        """Get diagnostic stats for observability commands."""
        context = self.get_context()

        return {
            "history_size": len(self._history),
            "session_duration_minutes": (self._last_activity - self._session_start)
            / 60,
            "focus_genres": context.focus_genres,
            "skip_rate": context.skip_rate,
            "consecutive_skips": context.consecutive_skips,
            "unique_artists": len(context.recent_artists),
            "songs_since_novelty": self._songs_since_novelty,
        }
