"""
V3 Autoplay Engine - Context Analyzer

Analyzes user listening behavior to build preference profiles:
- Skip detection using Apple Music-style thresholds
- Time-weighted preference tracking
- Genre/mood affinity scoring
- Session profile building for personalized recommendations
"""

import asyncio
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from .cache_manager import CacheManager, get_cache_manager
from .constants import EventType, SessionState, SongMetadata, V3Config
from .event_bus import EventBus, EventPayload

logger = logging.getLogger(__name__)


@dataclass
class PlaybackEvent:
    """Records a song playback event."""
    song_id: str
    started_at: float
    ended_at: Optional[float] = None
    duration_played_ms: int = 0
    total_duration_ms: int = 0
    was_skipped: bool = False
    skip_position_ms: int = 0
    
    @property
    def play_percentage(self) -> float:
        """Calculate percentage of song played."""
        if self.total_duration_ms <= 0:
            return 0.0
        return min(1.0, self.duration_played_ms / self.total_duration_ms)


@dataclass
class PreferenceScore:
    """Weighted preference score for a feature."""
    value: str
    score: float
    play_count: int
    skip_count: int
    last_played: float
    
    @property
    def net_score(self) -> float:
        """Calculate net preference (plays - skips with decay)."""
        # Recent plays count more
        recency_factor = self._recency_weight()
        return (self.score * recency_factor) - (self.skip_count * 0.5)
    
    def _recency_weight(self) -> float:
        """Calculate recency weight (exponential decay)."""
        hours_ago = (time.time() - self.last_played) / 3600
        # Half-life of 24 hours
        return math.exp(-0.693 * hours_ago / 24)


@dataclass
class SessionProfile:
    """User preference profile for current session."""
    session_id: str
    guild_id: str
    started_at: float
    
    # Playback history
    playback_history: list[PlaybackEvent] = field(default_factory=list)
    
    # Preference scores by category
    genre_preferences: dict[str, PreferenceScore] = field(default_factory=dict)
    mood_preferences: dict[str, PreferenceScore] = field(default_factory=dict)
    artist_preferences: dict[str, PreferenceScore] = field(default_factory=dict)
    energy_preferences: list[float] = field(default_factory=list)
    bpm_preferences: list[float] = field(default_factory=list)
    
    # Skip patterns
    skip_count: int = 0
    total_plays: int = 0
    consecutive_skips: int = 0
    
    # Session state
    state: SessionState = SessionState.COLD
    
    @property
    def skip_rate(self) -> float:
        """Calculate overall skip rate."""
        if self.total_plays == 0:
            return 0.0
        return self.skip_count / self.total_plays
    
    @property
    def avg_energy(self) -> float:
        """Calculate average preferred energy level."""
        if not self.energy_preferences:
            return 0.5
        return sum(self.energy_preferences[-10:]) / len(self.energy_preferences[-10:])
    
    @property
    def avg_bpm(self) -> float:
        """Calculate average preferred BPM."""
        if not self.bpm_preferences:
            return 120.0
        return sum(self.bpm_preferences[-10:]) / len(self.bpm_preferences[-10:])


class ContextAnalyzer:
    """
    Analyzes listening context to inform recommendations.
    
    Tracks:
    - Song completion vs skip patterns
    - Genre and mood affinities over time
    - Energy and BPM trends
    - Artist preferences
    
    Uses Apple Music-style skip detection:
    - Skip if < 30s played for songs > 30s
    - Skip if < 50% played for songs < 30s
    - Early skip (< 10s) counts as strong negative
    """
    
    # Skip detection thresholds (Apple Music style)
    SKIP_THRESHOLD_SHORT = 0.5  # 50% for songs < 30s
    SKIP_THRESHOLD_MS = 30000   # 30s absolute for longer songs
    EARLY_SKIP_MS = 10000       # < 10s is "strong" skip
    
    # Preference decay
    PREFERENCE_DECAY_HOURS = 24
    MAX_HISTORY_SIZE = 100
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        cache: Optional[CacheManager] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize context analyzer.
        
        Args:
            config: V3 configuration
            cache: Cache manager for metadata lookup
            event_bus: Event bus for notifications
        """
        self.config = config or V3Config()
        self.cache = cache or get_cache_manager()
        self.event_bus = event_bus or EventBus()
        
        # Active session profiles
        self._sessions: dict[str, SessionProfile] = {}
        
        # Global preference aggregates (across sessions)
        self._global_genres: dict[str, PreferenceScore] = {}
        self._global_artists: dict[str, PreferenceScore] = {}
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and subscribe to events."""
        if self._initialized:
            return
        
        await self.cache.initialize()
        
        # Subscribe to playback events (subscribe is synchronous)
        self.event_bus.subscribe(
            EventType.SONG_PLAYED,
            self._on_SONG_PLAYED
        )
        self.event_bus.subscribe(
            EventType.SONG_SKIPPED,
            self._on_SONG_SKIPPED
        )
        
        self._initialized = True
        logger.info("Context analyzer initialized")
    
    async def shutdown(self) -> None:
        """Clean up subscriptions."""
        self.event_bus.unsubscribe(
            EventType.SONG_PLAYED,
            self._on_SONG_PLAYED
        )
        self.event_bus.unsubscribe(
            EventType.SONG_SKIPPED,
            self._on_SONG_SKIPPED
        )
        
        self._initialized = False
    
    def get_or_create_session(
        self,
        session_id: str,
        guild_id: str
    ) -> SessionProfile:
        """
        Get existing session or create new one.
        
        Args:
            session_id: Unique session identifier
            guild_id: Discord guild ID
            
        Returns:
            SessionProfile for the session
        """
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionProfile(
                session_id=session_id,
                guild_id=guild_id,
                started_at=time.time()
            )
        
        return self._sessions[session_id]
    
    def end_session(self, session_id: str) -> Optional[SessionProfile]:
        """
        End a session and return final profile.
        
        Args:
            session_id: Session to end
            
        Returns:
            Final session profile, or None if not found
        """
        return self._sessions.pop(session_id, None)
    
    async def record_playback(
        self,
        session_id: str,
        song_id: str,
        duration_played_ms: int,
        total_duration_ms: int,
        ended_normally: bool = True
    ) -> None:
        """
        Record a song playback event.
        
        Args:
            session_id: Session identifier
            song_id: Song that was played
            duration_played_ms: How long the song was played
            total_duration_ms: Total song duration
            ended_normally: Whether song ended naturally (not skipped)
        """
        session = self._sessions.get(session_id)
        if not session:
            return
        
        # Determine if this was a skip
        was_skipped = self._detect_skip(
            duration_played_ms,
            total_duration_ms,
            ended_normally
        )
        
        # Create playback event
        event = PlaybackEvent(
            song_id=song_id,
            started_at=time.time() - (duration_played_ms / 1000),
            ended_at=time.time(),
            duration_played_ms=duration_played_ms,
            total_duration_ms=total_duration_ms,
            was_skipped=was_skipped,
            skip_position_ms=duration_played_ms if was_skipped else 0
        )
        
        # Add to history (with size limit)
        session.playback_history.append(event)
        if len(session.playback_history) > self.MAX_HISTORY_SIZE:
            session.playback_history.pop(0)
        
        # Update counters
        session.total_plays += 1
        if was_skipped:
            session.skip_count += 1
            session.consecutive_skips += 1
        else:
            session.consecutive_skips = 0
        
        # Update session state
        self._update_session_state(session)
        
        # Update preferences from metadata
        await self._update_preferences(session, song_id, was_skipped, event)
        
        # Publish event
        event_type = EventType.SONG_SKIPPED if was_skipped else EventType.SONG_PLAYED
        await self.event_bus.publish(EventPayload(
            event_type=event_type,
            data={
                "session_id": session_id,
                "song_id": song_id,
                "play_percentage": event.play_percentage,
                "was_skipped": was_skipped,
                "consecutive_skips": session.consecutive_skips
            }
        ))
    
    def _detect_skip(
        self,
        duration_played_ms: int,
        total_duration_ms: int,
        ended_normally: bool
    ) -> bool:
        """
        Detect if playback was a skip using Apple Music thresholds.
        
        Args:
            duration_played_ms: How long song was played
            total_duration_ms: Total song duration
            ended_normally: Whether song ended naturally
            
        Returns:
            True if this counts as a skip
        """
        if ended_normally:
            return False
        
        # Short songs: skip if < 50% played
        if total_duration_ms < self.SKIP_THRESHOLD_MS:
            return duration_played_ms < (total_duration_ms * self.SKIP_THRESHOLD_SHORT)
        
        # Longer songs: skip if < 30s played
        return duration_played_ms < self.SKIP_THRESHOLD_MS
    
    def _update_session_state(self, session: SessionProfile) -> None:
        """Update session state based on play count."""
        play_count = len(session.playback_history)
        
        if play_count <= self.config.thresholds.cold_max:
            session.state = SessionState.COLD
        elif play_count <= self.config.thresholds.warm_max:
            session.state = SessionState.WARM
        elif play_count < self.config.thresholds.extended_min:
            session.state = SessionState.HOT
        else:
            session.state = SessionState.EXTENDED
    
    async def _update_preferences(
        self,
        session: SessionProfile,
        song_id: str,
        was_skipped: bool,
        event: PlaybackEvent
    ) -> None:
        """Update preference scores based on playback."""
        # Get song metadata
        metadata = await self.cache.get_metadata(song_id)
        if not metadata:
            return
        
        # Calculate score based on play behavior
        # Full play = +1.0, early skip = -0.5, late skip = -0.2
        if was_skipped:
            if event.duration_played_ms < self.EARLY_SKIP_MS:
                score = -0.5  # Strong negative for early skip
            else:
                score = -0.2  # Mild negative for late skip
        else:
            score = 1.0 * event.play_percentage  # Scale by completion
        
        now = time.time()
        
        # Update genre preferences
        if metadata.librarian_info:
            for genre in metadata.librarian_info.genres:
                self._update_preference_score(
                    session.genre_preferences,
                    genre,
                    score,
                    was_skipped,
                    now
                )
            
            # Update mood preferences
            for mood in metadata.librarian_info.moods:
                self._update_preference_score(
                    session.mood_preferences,
                    mood,
                    score,
                    was_skipped,
                    now
                )
            
            # Track energy preference
            session.energy_preferences.append(
                metadata.librarian_info.energy_level
            )
        
        # Update artist preference
        if metadata.artist:
            self._update_preference_score(
                session.artist_preferences,
                metadata.artist,
                score,
                was_skipped,
                now
            )
        
        # Track BPM preference
        if metadata.audio_features and metadata.audio_features.bpm:
            session.bpm_preferences.append(metadata.audio_features.bpm)
    
    def _update_preference_score(
        self,
        preferences: dict[str, PreferenceScore],
        value: str,
        score: float,
        was_skipped: bool,
        timestamp: float
    ) -> None:
        """Update or create a preference score."""
        if value not in preferences:
            preferences[value] = PreferenceScore(
                value=value,
                score=0.0,
                play_count=0,
                skip_count=0,
                last_played=timestamp
            )
        
        pref = preferences[value]
        pref.score += score
        pref.last_played = timestamp
        
        if was_skipped:
            pref.skip_count += 1
        else:
            pref.play_count += 1
    
    # Event handlers (registered with EventBus but tracking done via record_playback)
    async def _on_SONG_PLAYED(self, payload: EventPayload) -> None:
        pass
    
    async def _on_SONG_SKIPPED(self, payload: EventPayload) -> None:
        pass
    
    def get_top_preferences(
        self,
        session_id: str,
        category: str = "genres",
        limit: int = 5
    ) -> list[tuple[str, float]]:
        """
        Get top preferences for a category.
        
        Args:
            session_id: Session identifier
            category: "genres", "moods", or "artists"
            limit: Number of top items to return
            
        Returns:
            List of (value, score) tuples
        """
        session = self._sessions.get(session_id)
        if not session:
            return []
        
        preferences = getattr(session, f"{category.rstrip('s')}_preferences", {})
        
        # Sort by net score
        sorted_prefs = sorted(
            preferences.items(),
            key=lambda x: x[1].net_score,
            reverse=True
        )
        
        return [(p[0], p[1].net_score) for p in sorted_prefs[:limit]]
    
    def get_avoided_values(
        self,
        session_id: str,
        category: str = "genres"
    ) -> list[str]:
        """
        Get values that should be avoided based on skip patterns.
        
        Args:
            session_id: Session identifier
            category: "genres", "moods", or "artists"
            
        Returns:
            List of values with high skip rates
        """
        session = self._sessions.get(session_id)
        if not session:
            return []
        
        preferences = getattr(session, f"{category.rstrip('s')}_preferences", {})
        
        avoided = []
        for value, pref in preferences.items():
            # Avoid if skip rate > 70% with enough samples
            total = pref.play_count + pref.skip_count
            if total >= 3 and pref.skip_count / total > 0.7:
                avoided.append(value)
        
        return avoided
    
    def should_trigger_reanalysis(self, session_id: str) -> bool:
        """
        Check if context should be reanalyzed (Apple Music trigger).
        
        Triggers when:
        - 3+ consecutive skips
        - Skip rate > 50% in last 10 songs
        - Sudden energy preference shift
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if reanalysis recommended
        """
        session = self._sessions.get(session_id)
        if not session:
            return False
        
        # Trigger on consecutive skips
        if session.consecutive_skips >= 3:
            return True
        
        # Check recent skip rate
        recent = session.playback_history[-10:]
        if len(recent) >= 5:
            recent_skips = sum(1 for e in recent if e.was_skipped)
            if recent_skips / len(recent) > 0.5:
                return True
        
        # Check energy shift
        if len(session.energy_preferences) >= 10:
            early = session.energy_preferences[:5]
            recent = session.energy_preferences[-5:]
            early_avg = sum(early) / len(early)
            recent_avg = sum(recent) / len(recent)
            
            if abs(recent_avg - early_avg) > 0.3:
                return True
        
        return False
    
    def get_session_context(
        self,
        session_id: str
    ) -> Optional[dict[str, Any]]:
        """
        Get full session context for recommendation engine.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary with all context information
        """
        session = self._sessions.get(session_id)
        if not session:
            return None
        
        return {
            "session_id": session_id,
            "guild_id": session.guild_id,
            "state": session.state.value,
            "play_count": len(session.playback_history),
            "skip_rate": session.skip_rate,
            "consecutive_skips": session.consecutive_skips,
            "avg_energy": session.avg_energy,
            "avg_bpm": session.avg_bpm,
            "top_genres": self.get_top_preferences(session_id, "genres", 5),
            "top_moods": self.get_top_preferences(session_id, "moods", 5),
            "top_artists": self.get_top_preferences(session_id, "artists", 3),
            "avoided_genres": self.get_avoided_values(session_id, "genres"),
            "recent_songs": [
                e.song_id for e in session.playback_history[-5:]
            ],
            "needs_reanalysis": self.should_trigger_reanalysis(session_id)
        }
    
    def get_preference_vector(
        self,
        session_id: str
    ) -> Optional[dict[str, float]]:
        """
        Get normalized preference vector for similarity matching.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dictionary of feature -> normalized score
        """
        session = self._sessions.get(session_id)
        if not session:
            return None
        
        vector = {}
        
        # Normalize genre scores
        genre_total = sum(abs(p.net_score) for p in session.genre_preferences.values())
        if genre_total > 0:
            for genre, pref in session.genre_preferences.items():
                vector[f"genre:{genre}"] = pref.net_score / genre_total
        
        # Normalize mood scores
        mood_total = sum(abs(p.net_score) for p in session.mood_preferences.values())
        if mood_total > 0:
            for mood, pref in session.mood_preferences.items():
                vector[f"mood:{mood}"] = pref.net_score / mood_total
        
        # Add energy and BPM as features
        vector["energy"] = session.avg_energy
        vector["bpm_normalized"] = min(1.0, session.avg_bpm / 200.0)
        
        return vector


# Singleton instance
_context_analyzer: Optional[ContextAnalyzer] = None


def get_context_analyzer() -> ContextAnalyzer:
    """Get global context analyzer instance."""
    global _context_analyzer
    if _context_analyzer is None:
        _context_analyzer = ContextAnalyzer()
    return _context_analyzer
