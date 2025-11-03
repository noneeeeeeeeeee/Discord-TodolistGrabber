"""
Context Tracker for Autoplay V2 - Recent Session History

Tracks the last N played songs with their metadata to maintain "core vibe" context
for the recommendation algorithm. This enables:
- Genre/mood continuity tracking
- Skip rate monitoring for adaptive exploration
- Artist diversity enforcement
- Temporal session state management

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
    """Record of a played track with context"""

    track_id: str
    artist: str
    title: str
    genres: List[str]
    mood_vector: Optional[
        Union[List[float], Dict]
    ]  # Can be list or dict with 'vector' key
    mood_label: Optional[str]
    timestamp: float
    was_skipped: bool
    skip_type: Optional[str]  # "hard", "medium", "soft", or None
    progress_ratio: float  # 0.0 to 1.0


@dataclass
class SessionContext:
    """Aggregated session state derived from recent history"""

    focus_genres: List[str]  # Most common genres (mode)
    current_mood_vector: Optional[List[float]]  # Average mood
    recent_artists: Set[str]  # Artists played in window
    skip_rate: float  # Ratio of skips in recent window
    consecutive_skips: int  # Current skip streak
    songs_since_novelty: int  # Counter for exploration timing
    session_start: float
    last_activity: float
    disliked_tags: Dict[str, float] = field(
        default_factory=dict
    )  # Tags from skipped tracks with penalties


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

        LOG.info("🎯 [ContextTracker] Initialized (window=%d tracks)", history_size)

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
    ) -> None:
        """
        Record a played track in the history.

        Args:
            track_id: Unique identifier
            artist: Artist name
            title: Track title
            genres: List of genre tags
            mood_vector: Optional [energy, valence, tempo] vector
            mood_label: Optional mood label (e.g., "energetic", "mellow")
            was_skipped: Whether track was skipped
            skip_type: If skipped, type: "hard", "medium", or "soft"
            progress_ratio: How much of track was played (0.0-1.0)
        """
        now = datetime.now().timestamp()

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
        )

        self._history.append(played)

        # Trim to window size
        if len(self._history) > self._history_size:
            self._history.pop(0)

        self._last_activity = now

        if self._verbose >= 1:
            status = "⏭️ skipped" if was_skipped else "✅ finished"
            LOG.info(
                "📊 [Context] Recorded: '%s' by '%s' (%s, %.0f%% played) | History: %d tracks",
                title,
                artist,
                status,
                progress_ratio * 100,
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
            )

        # Focus genres: most common genres (mode)
        all_genres = [g for track in self._history for g in track.genres]
        genre_counts = Counter(all_genres)
        focus_genres = [g for g, _ in genre_counts.most_common(3)]

        # Current mood vector: average of recent moods
        # mood_vector can be either a dict with 'vector' key or a list
        mood_vectors = []
        for t in self._history:
            if t.mood_vector:
                if isinstance(t.mood_vector, dict):
                    vec = t.mood_vector.get("vector")
                    if vec:
                        mood_vectors.append(vec)
                elif isinstance(t.mood_vector, (list, tuple)):
                    mood_vectors.append(t.mood_vector)

        current_mood = None
        if mood_vectors:
            # Average each dimension
            dim_count = len(mood_vectors[0])
            current_mood = [
                sum(v[i] for v in mood_vectors) / len(mood_vectors)
                for i in range(dim_count)
            ]

        # Recent artists (for diversity)
        recent_artists = {t.artist for t in self._history}

        # Skip rate (last 10 tracks or full history if smaller)
        recent_window = self._history[-10:]
        skips = sum(1 for t in recent_window if t.was_skipped)
        skip_rate = skips / len(recent_window) if recent_window else 0.0

        # Consecutive skips (from end)
        consecutive_skips = 0
        for track in reversed(self._history):
            if track.was_skipped:
                consecutive_skips += 1
            else:
                break

        # Track disliked tags from recently skipped tracks
        disliked_tags: Dict[str, float] = {}
        recent_skips = [
            t for t in self._history[-10:] if t.was_skipped and t.progress_ratio < 0.5
        ]  # Early skips

        for track in recent_skips:
            # Weight by how early the skip was (earlier skip = stronger dislike)
            skip_weight = 1.0 - track.progress_ratio  # 0.0-1.0 (higher = earlier skip)

            # Decay based on recency (more recent = stronger signal)
            recency_idx = len(self._history) - 1 - self._history.index(track)
            recency_weight = 1.0 / (1.0 + recency_idx * 0.1)  # Decay over distance

            penalty = skip_weight * recency_weight * 0.5  # Scale to 0.0-0.5

            for genre in track.genres:
                genre_lower = genre.lower()
                if genre_lower in disliked_tags:
                    disliked_tags[genre_lower] = min(
                        0.8, disliked_tags[genre_lower] + penalty
                    )
                else:
                    disliked_tags[genre_lower] = penalty

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
        )

        if self._verbose >= 2:
            LOG.debug(
                "🎯 [Context] Current state: genres=%s, skip_rate=%.2f, consecutive_skips=%d, artists=%d",
                focus_genres[:2],
                skip_rate,
                consecutive_skips,
                len(recent_artists),
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
            return 1.0  # Never played, no penalty

        # Calculate songs since last play
        songs_since = 0
        for track in reversed(self._history):
            if track.timestamp <= last_played:
                break
            songs_since += 1

        if songs_since == 0:
            return 0.01  # Just played, heavy penalty

        # Exponential decay
        penalty = math.exp(-songs_since / tau)

        return 1.0 - penalty  # Invert so 1.0 = no penalty

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
