"""
V3 Autoplay Engine - Novelty Controller

Prevents recommendation staleness through diversity nudging:
- Genre stagnation detection and correction
- Artist repetition prevention
- Energy/mood variety enforcement
- Exploration vs exploitation balance

Inspired by Apple Music's "radio" algorithm that maintains variety
while respecting user preferences.
"""

import logging
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from .constants import EventType, SessionState, SongMetadata, V3Config
from .context_analyzer import ContextAnalyzer, SessionProfile, get_context_analyzer
from .event_bus import EventBus, EventPayload

logger = logging.getLogger(__name__)


@dataclass
class DiversityMetrics:
    """Tracks diversity metrics for a session."""
    genre_distribution: dict[str, int] = field(default_factory=dict)
    artist_plays: dict[str, list[float]] = field(default_factory=dict)
    energy_history: list[float] = field(default_factory=list)
    bpm_history: list[float] = field(default_factory=list)
    mood_distribution: dict[str, int] = field(default_factory=dict)
    
    # Stagnation counters
    same_genre_streak: int = 0
    same_artist_streak: int = 0
    similar_energy_streak: int = 0
    
    last_genre: Optional[str] = None
    last_artist: Optional[str] = None
    last_energy: Optional[float] = None


@dataclass
class NoveltyNudge:
    """A recommendation adjustment to increase variety."""
    nudge_type: str  # "genre", "artist", "energy", "mood", "explore"
    reason: str
    target_value: Optional[str] = None  # For genre/mood nudges
    target_range: Optional[tuple[float, float]] = None  # For energy/BPM nudges
    strength: float = 0.5  # 0-1, how strongly to apply


class NoveltyController:
    """
    Controls recommendation novelty to prevent staleness.
    
    Monitors listening patterns and generates "nudges" to push
    recommendations toward more variety when stagnation is detected.
    
    Stagnation Detection:
    - Same primary genre for 5+ songs → genre nudge
    - Same artist within 10 songs → artist cooldown
    - Energy in ±0.1 range for 4+ songs → energy nudge
    - No mood variety in 8+ songs → mood nudge
    
    Exploration Triggers:
    - Every 10th song gets exploration bonus
    - Extended sessions (25+ songs) increase exploration
    - Low skip rate allows more adventurous picks
    """
    
    # Stagnation thresholds
    GENRE_STREAK_THRESHOLD = 5
    ARTIST_COOLDOWN_SONGS = 10
    ENERGY_STREAK_THRESHOLD = 4
    ENERGY_SIMILARITY_RANGE = 0.1
    MOOD_VARIETY_THRESHOLD = 8
    
    # Exploration settings
    EXPLORATION_INTERVAL = 10
    BASE_EXPLORATION_RATE = 0.1
    EXTENDED_EXPLORATION_BOOST = 0.15
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        context_analyzer: Optional[ContextAnalyzer] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize novelty controller.
        
        Args:
            config: V3 configuration
            context_analyzer: For session context
            event_bus: For event notifications
        """
        self.config = config or V3Config()
        self.context = context_analyzer or get_context_analyzer()
        self.event_bus = event_bus or EventBus()
        
        # Diversity metrics per session
        self._metrics: dict[str, DiversityMetrics] = {}
        
        # Recent nudges (for debugging/logging)
        self._recent_nudges: list[tuple[float, NoveltyNudge]] = []
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and subscribe to events."""
        if self._initialized:
            return
        
        await self.context.initialize()
        
        # Subscribe to track events (subscribe is synchronous)
        self.event_bus.subscribe(
            EventType.SONG_PLAYED,
            self._on_SONG_PLAYED
        )
        
        self._initialized = True
        logger.info("Novelty controller initialized")
    
    async def shutdown(self) -> None:
        """Clean up."""
        self.event_bus.unsubscribe(
            EventType.SONG_PLAYED,
            self._on_SONG_PLAYED
        )
        self._initialized = False
    
    def get_metrics(self, session_id: str) -> DiversityMetrics:
        """Get or create diversity metrics for a session."""
        if session_id not in self._metrics:
            self._metrics[session_id] = DiversityMetrics()
        return self._metrics[session_id]
    
    def end_session(self, session_id: str) -> None:
        """Clean up session metrics."""
        self._metrics.pop(session_id, None)
    
    async def record_played(
        self,
        session_id: str,
        metadata: SongMetadata
    ) -> None:
        """
        Record a played song for diversity tracking.
        
        Args:
            session_id: Session identifier
            metadata: Song metadata
        """
        metrics = self.get_metrics(session_id)
        now = time.time()
        
        # Extract features
        primary_genre = None
        if metadata.librarian_info and metadata.librarian_info.genres:
            primary_genre = metadata.librarian_info.genres[0]
            
            # Update genre distribution
            for genre in metadata.librarian_info.genres[:3]:
                metrics.genre_distribution[genre] = \
                    metrics.genre_distribution.get(genre, 0) + 1
            
            # Update mood distribution
            for mood in (metadata.librarian_info.moods or [])[:3]:
                metrics.mood_distribution[mood] = \
                    metrics.mood_distribution.get(mood, 0) + 1
        
        # Track artist plays
        if metadata.artist:
            if metadata.artist not in metrics.artist_plays:
                metrics.artist_plays[metadata.artist] = []
            metrics.artist_plays[metadata.artist].append(now)
            
            # Update artist streak
            if metrics.last_artist == metadata.artist:
                metrics.same_artist_streak += 1
            else:
                metrics.same_artist_streak = 1
            metrics.last_artist = metadata.artist
        
        # Track energy
        energy = 0.5
        if metadata.librarian_info:
            energy = metadata.librarian_info.energy_level
        elif metadata.audio_features:
            energy = metadata.audio_features.energy
        
        metrics.energy_history.append(energy)
        
        # Update energy streak
        if metrics.last_energy is not None:
            if abs(energy - metrics.last_energy) < self.ENERGY_SIMILARITY_RANGE:
                metrics.similar_energy_streak += 1
            else:
                metrics.similar_energy_streak = 1
        metrics.last_energy = energy
        
        # Track BPM
        if metadata.audio_features and metadata.audio_features.bpm:
            metrics.bpm_history.append(metadata.audio_features.bpm)
        
        # Update genre streak
        if primary_genre:
            if metrics.last_genre == primary_genre:
                metrics.same_genre_streak += 1
            else:
                metrics.same_genre_streak = 1
            metrics.last_genre = primary_genre
    
    # Placeholder event handler (not actively used but EventBus compatible)
    async def _on_SONG_PLAYED(self, payload: EventPayload) -> None:
        pass
    
    def get_nudges(
        self,
        session_id: str,
        session_state: SessionState,
        play_count: int,
        skip_rate: float
    ) -> list[NoveltyNudge]:
        """
        Get novelty nudges based on current session state.
        
        Args:
            session_id: Session identifier
            session_state: Current session state
            play_count: Number of songs played
            skip_rate: Session skip rate
            
        Returns:
            List of nudges to apply to recommendations
        """
        metrics = self.get_metrics(session_id)
        nudges = []
        
        # Genre stagnation check
        if metrics.same_genre_streak >= self.GENRE_STREAK_THRESHOLD:
            # Find underrepresented genres
            target_genre = self._get_underrepresented_genre(metrics)
            nudges.append(NoveltyNudge(
                nudge_type="genre",
                reason=f"Same genre ({metrics.last_genre}) for {metrics.same_genre_streak} songs",
                target_value=target_genre,
                strength=min(0.8, 0.3 + (metrics.same_genre_streak - 5) * 0.1)
            ))
        
        # Artist repetition check
        if metrics.same_artist_streak >= 2:
            nudges.append(NoveltyNudge(
                nudge_type="artist",
                reason=f"Same artist ({metrics.last_artist}) played {metrics.same_artist_streak}x",
                target_value=metrics.last_artist,
                strength=0.9  # Strong nudge to avoid same artist
            ))
        
        # Artist cooldown check
        artist_cooldowns = self._get_artist_cooldowns(metrics)
        if artist_cooldowns:
            nudges.append(NoveltyNudge(
                nudge_type="artist_cooldown",
                reason=f"{len(artist_cooldowns)} artists in cooldown",
                target_value=",".join(artist_cooldowns),
                strength=1.0  # Hard block
            ))
        
        # Energy variety check
        if metrics.similar_energy_streak >= self.ENERGY_STREAK_THRESHOLD:
            target_energy = self._get_contrasting_energy(metrics)
            nudges.append(NoveltyNudge(
                nudge_type="energy",
                reason=f"Similar energy for {metrics.similar_energy_streak} songs",
                target_range=target_energy,
                strength=0.5
            ))
        
        # Mood variety check
        if self._needs_mood_variety(metrics):
            nudges.append(NoveltyNudge(
                nudge_type="mood",
                reason="Low mood variety",
                strength=0.4
            ))
        
        # Exploration trigger
        if self._should_explore(play_count, session_state, skip_rate):
            exploration_strength = self._calculate_exploration_strength(
                session_state, skip_rate
            )
            nudges.append(NoveltyNudge(
                nudge_type="explore",
                reason=f"Exploration trigger (song {play_count})",
                strength=exploration_strength
            ))
        
        # Record nudges for debugging
        now = time.time()
        for nudge in nudges:
            self._recent_nudges.append((now, nudge))
        
        # Keep only recent nudges
        self._recent_nudges = [
            (t, n) for t, n in self._recent_nudges
            if now - t < 3600  # Keep 1 hour of history
        ]
        
        return nudges
    
    def _get_underrepresented_genre(
        self,
        metrics: DiversityMetrics
    ) -> Optional[str]:
        """Find a genre that hasn't been played much."""
        if not metrics.genre_distribution:
            return None
        
        # Find genre with lowest play count (but at least played once)
        sorted_genres = sorted(
            metrics.genre_distribution.items(),
            key=lambda x: x[1]
        )
        
        # Return genre that's not the current one
        for genre, count in sorted_genres:
            if genre != metrics.last_genre:
                return genre
        
        return None
    
    def _get_artist_cooldowns(
        self,
        metrics: DiversityMetrics
    ) -> list[str]:
        """Get list of artists that should be on cooldown."""
        # Calculate how many songs ago each artist was played
        total_plays = sum(len(plays) for plays in metrics.artist_plays.values())
        
        cooldowns = []
        for artist, plays in metrics.artist_plays.items():
            if len(plays) >= 2:
                # Artist has been played multiple times
                recent_plays = len([
                    p for p in plays
                    if total_plays - plays.index(p) <= self.ARTIST_COOLDOWN_SONGS
                ])
                if recent_plays >= 2:
                    cooldowns.append(artist)
        
        return cooldowns
    
    def _get_contrasting_energy(
        self,
        metrics: DiversityMetrics
    ) -> tuple[float, float]:
        """Get target energy range that contrasts with recent songs."""
        if not metrics.energy_history:
            return (0.3, 0.7)
        
        recent_energy = metrics.last_energy or 0.5
        
        # If recent is high, suggest low, and vice versa
        if recent_energy > 0.6:
            return (0.2, 0.5)
        elif recent_energy < 0.4:
            return (0.5, 0.8)
        else:
            # Mid-energy, suggest either direction
            if random.random() > 0.5:
                return (0.6, 0.9)
            else:
                return (0.1, 0.4)
    
    def _needs_mood_variety(self, metrics: DiversityMetrics) -> bool:
        """Check if mood variety is too low."""
        if len(metrics.mood_distribution) == 0:
            return False
        
        total_moods = sum(metrics.mood_distribution.values())
        if total_moods < self.MOOD_VARIETY_THRESHOLD:
            return False
        
        # Check if one mood dominates
        max_mood_count = max(metrics.mood_distribution.values())
        if max_mood_count / total_moods > 0.6:
            return True
        
        return False
    
    def _should_explore(
        self,
        play_count: int,
        session_state: SessionState,
        skip_rate: float
    ) -> bool:
        """Determine if this should be an exploration pick."""
        # Every Nth song is exploration
        if play_count % self.EXPLORATION_INTERVAL == 0:
            return True
        
        # Random exploration based on rate
        exploration_rate = self.BASE_EXPLORATION_RATE
        
        # Boost in extended sessions
        if session_state == SessionState.EXTENDED:
            exploration_rate += self.EXTENDED_EXPLORATION_BOOST
        
        # Reduce if high skip rate
        if skip_rate > 0.4:
            exploration_rate *= 0.5
        
        return random.random() < exploration_rate
    
    def _calculate_exploration_strength(
        self,
        session_state: SessionState,
        skip_rate: float
    ) -> float:
        """Calculate how adventurous exploration should be."""
        base_strength = 0.5
        
        # More exploration in warm/hot sessions (more data to judge)
        if session_state in [SessionState.HOT, SessionState.EXTENDED]:
            base_strength = 0.7
        elif session_state == SessionState.WARM:
            base_strength = 0.6
        
        # Reduce strength if high skip rate
        if skip_rate > 0.3:
            base_strength *= (1 - skip_rate)
        
        return base_strength
    
    def apply_nudges(
        self,
        candidates: list[SongMetadata],
        nudges: list[NoveltyNudge]
    ) -> list[tuple[SongMetadata, float]]:
        """
        Apply nudges to candidate songs, returning adjusted scores.
        
        Args:
            candidates: List of candidate songs
            nudges: Nudges to apply
            
        Returns:
            List of (song, adjustment) tuples
        """
        adjustments = []
        
        for song in candidates:
            adjustment = 0.0
            
            for nudge in nudges:
                adj = self._calculate_nudge_adjustment(song, nudge)
                adjustment += adj
            
            adjustments.append((song, adjustment))
        
        return adjustments
    
    def _calculate_nudge_adjustment(
        self,
        song: SongMetadata,
        nudge: NoveltyNudge
    ) -> float:
        """Calculate score adjustment for a single nudge."""
        if nudge.nudge_type == "genre":
            # Boost songs matching target genre
            if song.librarian_info and nudge.target_value:
                if nudge.target_value in song.librarian_info.genres:
                    return nudge.strength * 0.5
                # Penalize songs matching avoided genre
                elif song.librarian_info.genres and song.librarian_info.genres[0] == self._metrics.get("last_genre"):
                    return -nudge.strength * 0.3
        
        elif nudge.nudge_type == "artist":
            # Penalize same artist
            if song.artist == nudge.target_value:
                return -nudge.strength * 0.8
        
        elif nudge.nudge_type == "artist_cooldown":
            # Block artists in cooldown
            if nudge.target_value and song.artist in nudge.target_value.split(","):
                return -1.0  # Strong penalty
        
        elif nudge.nudge_type == "energy":
            # Boost songs in target energy range
            if song.librarian_info and nudge.target_range:
                energy = song.librarian_info.energy_level
                if nudge.target_range[0] <= energy <= nudge.target_range[1]:
                    return nudge.strength * 0.4
        
        elif nudge.nudge_type == "explore":
            # Boost less popular/common picks
            # This would need popularity data to implement properly
            pass
        
        return 0.0
    
    def get_diversity_report(self, session_id: str) -> dict[str, Any]:
        """Get a report of diversity metrics for a session."""
        metrics = self.get_metrics(session_id)
        
        return {
            "genre_distribution": dict(metrics.genre_distribution),
            "top_genres": sorted(
                metrics.genre_distribution.items(),
                key=lambda x: x[1],
                reverse=True
            )[:5],
            "artists_played": len(metrics.artist_plays),
            "genre_streak": metrics.same_genre_streak,
            "artist_streak": metrics.same_artist_streak,
            "energy_streak": metrics.similar_energy_streak,
            "avg_energy": (
                sum(metrics.energy_history) / len(metrics.energy_history)
                if metrics.energy_history else 0.5
            ),
            "energy_variance": self._calculate_variance(metrics.energy_history),
            "recent_nudges": [
                {"type": n.nudge_type, "reason": n.reason}
                for _, n in self._recent_nudges[-5:]
            ]
        }
    
    def _calculate_variance(self, values: list[float]) -> float:
        """Calculate variance of a list of values."""
        if len(values) < 2:
            return 0.0
        
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return variance


# Singleton instance
_novelty_controller: Optional[NoveltyController] = None


def get_novelty_controller() -> NoveltyController:
    """Get global novelty controller instance."""
    global _novelty_controller
    if _novelty_controller is None:
        _novelty_controller = NoveltyController()
    return _novelty_controller
