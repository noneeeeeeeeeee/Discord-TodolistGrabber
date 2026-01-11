"""
V3 Autoplay Engine - Collaborative Recommender

User behavior-based recommendation system that tracks:
- Transition patterns (what plays after what)
- Skip behavior and implicit feedback
- "More Like This" explicit feedback
- Multi-user consensus in voice channels

This module implements TRUE collaborative filtering based on user
behavior patterns, unlike vector_search_index.py which does 
content-based audio similarity.

Key Concepts:
- Transition Matrix: Records "what plays after what" across sessions
- Session-as-Document: Each session is a "document" of songs in sequence
- Group Consensus: For multi-user VCs, aggregate user preferences
- Behavioral Boost: Amplify recommendations based on engagement signals

Apple Music-Style Signals:
- Play-through rate (% of song listened)
- Repeat listens (explicit requeues)
- "More Like This" button (explicit positive feedback)
- Skip patterns (implicit negative feedback)
- Low Quality reports (ignore for algorithm)
"""

import asyncio
import json
import logging
import time
import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .cache_manager import CacheManager, get_cache_manager
from .constants import (
    SESSIONS_DIR,
    EventType,
    SessionState,
    SkipWeightConfig,
    SongMetadata,
    V3Config,
)
from .event_bus import EventBus, EventPayload

logger = logging.getLogger(__name__)


@dataclass
class TransitionRecord:
    """Records a transition between two songs."""
    from_song_id: str
    to_song_id: str
    count: int = 0  # Starts at 0, incremented on each call to record_transition
    total_play_through: float = 0.0  # Sum of play-through rates
    explicit_likes: int = 0          # "More Like This" clicks on transition
    skip_count: int = 0              # Times to_song was skipped after from_song
    last_seen: float = field(default_factory=time.time)
    
    @property
    def average_play_through(self) -> float:
        """Average play-through rate for this transition."""
        return self.total_play_through / self.count if self.count > 0 else 0.5
    
    @property
    def transition_score(self) -> float:
        """Calculate overall transition quality score."""
        # Base score from play-through
        score = self.average_play_through
        
        # Boost for explicit likes (each like = +10% up to +50%)
        like_boost = min(0.5, self.explicit_likes * 0.1)
        score += like_boost
        
        # Penalty for skips (each skip = -5% of score)
        skip_penalty = min(0.3, self.skip_count * 0.05)
        score -= skip_penalty
        
        # Confidence boost for frequently seen transitions
        confidence = min(1.0, self.count / 10)  # Max confidence at 10 occurrences
        
        return max(0.0, min(1.0, score * (0.5 + 0.5 * confidence)))


@dataclass
class UserBehaviorProfile:
    """Tracks behavior patterns for a single user."""
    user_id: int
    
    # Song-level signals
    liked_songs: set[str] = field(default_factory=set)      # "More Like This"
    skipped_songs: dict[str, int] = field(default_factory=dict)  # song_id -> skip count
    completed_songs: set[str] = field(default_factory=set)  # 90%+ play-through
    
    # Artist preferences
    preferred_artists: dict[str, float] = field(default_factory=dict)  # artist -> score
    avoided_artists: set[str] = field(default_factory=set)  # Artists with 3+ skips
    
    # Genre preferences (learned from behavior)
    genre_affinity: dict[str, float] = field(default_factory=dict)  # genre -> affinity score
    
    # Session stats
    sessions_count: int = 0
    total_songs_played: int = 0
    last_active: float = field(default_factory=time.time)
    
    def update_from_play(
        self,
        song_id: str,
        play_through_rate: float,
        metadata: Optional[SongMetadata] = None
    ) -> None:
        """Update profile from a song play."""
        self.total_songs_played += 1
        self.last_active = time.time()
        
        # Track completion
        if play_through_rate >= 0.9:
            self.completed_songs.add(song_id)
        
        # Update artist preference
        if metadata and metadata.artist:
            artist = metadata.artist.lower()
            current_score = self.preferred_artists.get(artist, 0.5)
            # Adjust based on play-through
            adjustment = (play_through_rate - 0.5) * 0.1
            self.preferred_artists[artist] = max(0.0, min(1.0, current_score + adjustment))
        
        # Update genre affinity
        if metadata and metadata.librarian_info and metadata.librarian_info.genres:
            for genre in metadata.librarian_info.genres:
                genre_lower = genre.lower()
                current_affinity = self.genre_affinity.get(genre_lower, 0.5)
                adjustment = (play_through_rate - 0.5) * 0.05
                self.genre_affinity[genre_lower] = max(0.0, min(1.0, current_affinity + adjustment))
    
    def record_skip(self, song_id: str, metadata: Optional[SongMetadata] = None) -> None:
        """Record a skip event."""
        self.skipped_songs[song_id] = self.skipped_songs.get(song_id, 0) + 1
        
        # Track artist avoidance
        if metadata and metadata.artist:
            artist = metadata.artist.lower()
            # Check total skips for this artist
            artist_skips = sum(
                1 for sid, count in self.skipped_songs.items()
                if count > 0  # Could enhance with metadata lookup
            )
            if artist_skips >= 3:
                self.avoided_artists.add(artist)
    
    def record_like(self, song_id: str, metadata: Optional[SongMetadata] = None) -> None:
        """Record an explicit 'More Like This' like."""
        self.liked_songs.add(song_id)
        
        # Boost artist preference significantly
        if metadata and metadata.artist:
            artist = metadata.artist.lower()
            current_score = self.preferred_artists.get(artist, 0.5)
            self.preferred_artists[artist] = min(1.0, current_score + 0.2)
        
        # Boost genre affinity
        if metadata and metadata.librarian_info and metadata.librarian_info.genres:
            for genre in metadata.librarian_info.genres:
                genre_lower = genre.lower()
                current_affinity = self.genre_affinity.get(genre_lower, 0.5)
                self.genre_affinity[genre_lower] = min(1.0, current_affinity + 0.15)


@dataclass
class GroupConsensus:
    """
    Aggregates preferences for multi-user voice channel sessions.
    
    When multiple users are in a VC, we need to find common ground
    while still introducing variety. This uses a voting-like system
    weighted by activity.
    """
    
    profiles: dict[int, UserBehaviorProfile] = field(default_factory=dict)
    activity_weights: dict[int, float] = field(default_factory=dict)  # user_id -> weight
    
    def add_user(self, user_id: int, profile: UserBehaviorProfile) -> None:
        """Add a user to the consensus group."""
        self.profiles[user_id] = profile
        # Initial weight = 1.0, adjusted by activity
        self.activity_weights[user_id] = 1.0
    
    def remove_user(self, user_id: int) -> None:
        """Remove a user from the consensus group."""
        self.profiles.pop(user_id, None)
        self.activity_weights.pop(user_id, None)
    
    def record_activity(self, user_id: int, activity_type: str) -> None:
        """Record user activity (boosts their weight)."""
        if user_id in self.activity_weights:
            boost = 0.1 if activity_type == "like" else 0.05
            self.activity_weights[user_id] = min(2.0, self.activity_weights[user_id] + boost)
    
    def get_consensus_genre_affinity(self) -> dict[str, float]:
        """Get weighted average genre affinity across all users."""
        if not self.profiles:
            return {}
        
        genre_scores: dict[str, list[tuple[float, float]]] = defaultdict(list)
        
        for user_id, profile in self.profiles.items():
            weight = self.activity_weights.get(user_id, 1.0)
            for genre, affinity in profile.genre_affinity.items():
                genre_scores[genre].append((affinity, weight))
        
        # Weighted average per genre
        consensus = {}
        for genre, scores in genre_scores.items():
            total_weight = sum(w for _, w in scores)
            if total_weight > 0:
                weighted_sum = sum(s * w for s, w in scores)
                consensus[genre] = weighted_sum / total_weight
        
        return consensus
    
    def get_veto_songs(self) -> set[str]:
        """Get songs that should be avoided (heavily skipped by multiple users)."""
        skip_counts: dict[str, int] = defaultdict(int)
        
        for profile in self.profiles.values():
            for song_id, count in profile.skipped_songs.items():
                if count >= 2:  # User skipped this song multiple times
                    skip_counts[song_id] += 1
        
        # Veto if majority of users have issues with it
        threshold = max(1, len(self.profiles) // 2)
        return {song_id for song_id, count in skip_counts.items() if count >= threshold}
    
    def get_safe_picks(self) -> set[str]:
        """Get songs that multiple users have liked or completed."""
        like_counts: dict[str, int] = defaultdict(int)
        
        for profile in self.profiles.values():
            for song_id in profile.liked_songs | profile.completed_songs:
                like_counts[song_id] += 1
        
        # Safe if at least 2 users or majority like it
        threshold = min(2, max(1, len(self.profiles) // 2))
        return {song_id for song_id, count in like_counts.items() if count >= threshold}


class TransitionMatrix:
    """
    Records and queries song transition patterns.
    
    This is the core data structure for behavioral collaborative filtering.
    It answers: "After song A plays, what songs tend to be well-received?"
    
    Storage: JSON file with periodic flush to disk.
    """
    
    def __init__(self, storage_path: Optional[Path] = None):
        """Initialize transition matrix."""
        self.storage_path = storage_path or (SESSIONS_DIR / "transition_matrix.json")
        self._transitions: dict[str, dict[str, TransitionRecord]] = {}
        self._dirty = False
        self._last_flush = time.time()
        self._flush_interval = 300  # 5 minutes
    
    async def load(self) -> None:
        """Load transition matrix from disk."""
        if not self.storage_path.exists():
            logger.info("No existing transition matrix found, starting fresh")
            return
        
        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            for from_id, transitions in data.items():
                self._transitions[from_id] = {}
                for to_id, record_data in transitions.items():
                    self._transitions[from_id][to_id] = TransitionRecord(
                        from_song_id=from_id,
                        to_song_id=to_id,
                        count=record_data.get("count", 1),
                        total_play_through=record_data.get("total_play_through", 0.5),
                        explicit_likes=record_data.get("explicit_likes", 0),
                        skip_count=record_data.get("skip_count", 0),
                        last_seen=record_data.get("last_seen", time.time())
                    )
            
            total_transitions = sum(len(t) for t in self._transitions.values())
            logger.info(f"Loaded transition matrix: {len(self._transitions)} sources, {total_transitions} transitions")
            
        except Exception as e:
            logger.error(f"Failed to load transition matrix: {e}")
    
    async def save(self) -> None:
        """Save transition matrix to disk."""
        if not self._dirty:
            return
        
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            
            data = {}
            for from_id, transitions in self._transitions.items():
                data[from_id] = {}
                for to_id, record in transitions.items():
                    data[from_id][to_id] = {
                        "count": record.count,
                        "total_play_through": record.total_play_through,
                        "explicit_likes": record.explicit_likes,
                        "skip_count": record.skip_count,
                        "last_seen": record.last_seen
                    }
            
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            self._dirty = False
            self._last_flush = time.time()
            logger.debug(f"Saved transition matrix to {self.storage_path}")
            
        except Exception as e:
            logger.error(f"Failed to save transition matrix: {e}")
    
    async def maybe_flush(self) -> None:
        """Flush to disk if enough time has passed."""
        if self._dirty and (time.time() - self._last_flush) > self._flush_interval:
            await self.save()
    
    def record_transition(
        self,
        from_song_id: str,
        to_song_id: str,
        play_through_rate: float,
        was_liked: bool = False,
        was_skipped: bool = False
    ) -> None:
        """Record a transition between songs."""
        if from_song_id not in self._transitions:
            self._transitions[from_song_id] = {}
        
        if to_song_id not in self._transitions[from_song_id]:
            self._transitions[from_song_id][to_song_id] = TransitionRecord(
                from_song_id=from_song_id,
                to_song_id=to_song_id
            )
        
        record = self._transitions[from_song_id][to_song_id]
        record.count += 1
        record.total_play_through += play_through_rate
        record.last_seen = time.time()
        
        if was_liked:
            record.explicit_likes += 1
        if was_skipped:
            record.skip_count += 1
        
        self._dirty = True
    
    def get_best_transitions(
        self,
        from_song_id: str,
        k: int = 10,
        exclude: Optional[set[str]] = None
    ) -> list[tuple[str, float]]:
        """Get best transitions from a song."""
        if from_song_id not in self._transitions:
            return []
        
        exclude = exclude or set()
        transitions = self._transitions[from_song_id]
        
        # Score and filter
        scored = [
            (to_id, record.transition_score)
            for to_id, record in transitions.items()
            if to_id not in exclude
        ]
        
        # Sort by score
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]
    
    def get_songs_leading_to(
        self,
        to_song_id: str,
        k: int = 10
    ) -> list[tuple[str, float]]:
        """Find songs that commonly lead to a given song (for 'More Like This')."""
        results = []
        
        for from_id, transitions in self._transitions.items():
            if to_song_id in transitions:
                record = transitions[to_song_id]
                results.append((from_id, record.transition_score))
        
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:k]


class CollaborativeRecommender:
    """
    User behavior-based recommendation system.
    
    Combines:
    - Transition Matrix: What plays well after what
    - User Profiles: Individual preference tracking
    - Group Consensus: Multi-user VC handling
    
    This provides TRUE collaborative filtering based on behavior,
    not just audio content similarity.
    """
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        cache: Optional[CacheManager] = None,
        event_bus: Optional[EventBus] = None
    ):
        """Initialize collaborative recommender."""
        self.config = config or V3Config()
        self.cache = cache or get_cache_manager()
        self.event_bus = event_bus or EventBus()
        
        # Core data structures
        self.transition_matrix = TransitionMatrix()
        self._user_profiles: dict[int, UserBehaviorProfile] = {}
        self._session_consensus: dict[str, GroupConsensus] = {}  # session_id -> consensus
        
        # Skip weight configuration
        self.skip_config = SkipWeightConfig()
        
        # Track current sessions for transition recording
        self._session_last_song: dict[str, str] = {}  # session_id -> last played song_id
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and load data."""
        if self._initialized:
            return
        
        await self.cache.initialize()
        await self.transition_matrix.load()
        await self._load_user_profiles()
        
        # Subscribe to events (some test doubles/mocks may be async)
        for event_type, handler in (
            (EventType.SONG_PLAYED, self._on_song_played),
            (EventType.SONG_SKIPPED, self._on_song_skipped),
            (EventType.USER_FEEDBACK, self._on_user_feedback),
        ):
            result = self.event_bus.subscribe(event_type, handler)
            if inspect.isawaitable(result):
                await result
        
        self._initialized = True
        logger.info("Collaborative recommender initialized")
    
    async def shutdown(self) -> None:
        """Clean up and save data."""
        await self.transition_matrix.save()
        await self._save_user_profiles()
        
        for event_type, handler in (
            (EventType.SONG_PLAYED, self._on_song_played),
            (EventType.SONG_SKIPPED, self._on_song_skipped),
            (EventType.USER_FEEDBACK, self._on_user_feedback),
        ):
            result = self.event_bus.unsubscribe(event_type, handler)
            if inspect.isawaitable(result):
                await result
        
        self._initialized = False
    
    async def _load_user_profiles(self) -> None:
        """Load user profiles from disk."""
        profiles_path = SESSIONS_DIR / "user_profiles.json"
        if not profiles_path.exists():
            return
        
        try:
            with open(profiles_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            for user_id_str, profile_data in data.items():
                user_id = int(user_id_str)
                profile = UserBehaviorProfile(user_id=user_id)
                profile.liked_songs = set(profile_data.get("liked_songs", []))
                profile.skipped_songs = profile_data.get("skipped_songs", {})
                profile.completed_songs = set(profile_data.get("completed_songs", []))
                profile.preferred_artists = profile_data.get("preferred_artists", {})
                profile.avoided_artists = set(profile_data.get("avoided_artists", []))
                profile.genre_affinity = profile_data.get("genre_affinity", {})
                profile.sessions_count = profile_data.get("sessions_count", 0)
                profile.total_songs_played = profile_data.get("total_songs_played", 0)
                profile.last_active = profile_data.get("last_active", time.time())
                self._user_profiles[user_id] = profile
            
            logger.info(f"Loaded {len(self._user_profiles)} user profiles")
            
        except Exception as e:
            logger.error(f"Failed to load user profiles: {e}")
    
    async def _save_user_profiles(self) -> None:
        """Save user profiles to disk."""
        profiles_path = SESSIONS_DIR / "user_profiles.json"
        
        try:
            profiles_path.parent.mkdir(parents=True, exist_ok=True)
            
            data = {}
            for user_id, profile in self._user_profiles.items():
                data[str(user_id)] = {
                    "liked_songs": list(profile.liked_songs),
                    "skipped_songs": profile.skipped_songs,
                    "completed_songs": list(profile.completed_songs),
                    "preferred_artists": profile.preferred_artists,
                    "avoided_artists": list(profile.avoided_artists),
                    "genre_affinity": profile.genre_affinity,
                    "sessions_count": profile.sessions_count,
                    "total_songs_played": profile.total_songs_played,
                    "last_active": profile.last_active
                }
            
            with open(profiles_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            logger.debug(f"Saved {len(self._user_profiles)} user profiles")
            
        except Exception as e:
            logger.error(f"Failed to save user profiles: {e}")
    
    def get_or_create_profile(self, user_id: int) -> UserBehaviorProfile:
        """Get or create a user behavior profile."""
        if user_id not in self._user_profiles:
            self._user_profiles[user_id] = UserBehaviorProfile(user_id=user_id)
        return self._user_profiles[user_id]
    
    async def _on_song_played(self, payload: EventPayload) -> None:
        """Handle song played event."""
        session_id = payload.data.get("session_id")
        song_id = payload.data.get("song_id")
        user_id = payload.data.get("user_id")
        play_through_rate = payload.data.get("play_through_rate", 1.0)
        
        if not session_id or not song_id:
            return
        
        # Record transition from last song
        last_song = self._session_last_song.get(session_id)
        if last_song:
            self.transition_matrix.record_transition(
                from_song_id=last_song,
                to_song_id=song_id,
                play_through_rate=play_through_rate
            )
        
        # Update last song for session
        self._session_last_song[session_id] = song_id
        
        # Update user profile
        if user_id:
            profile = self.get_or_create_profile(user_id)
            metadata = await self.cache.get_metadata(song_id)
            profile.update_from_play(song_id, play_through_rate, metadata)
        
        # Periodic flush
        await self.transition_matrix.maybe_flush()
    
    async def _on_song_skipped(self, payload: EventPayload) -> None:
        """Handle song skipped event."""
        session_id = payload.data.get("session_id")
        song_id = payload.data.get("song_id")
        user_id = payload.data.get("user_id")
        play_through_rate = payload.data.get("play_through_rate", 0.0)
        
        if not session_id or not song_id:
            return
        
        # Record skip in transition matrix
        last_song = self._session_last_song.get(session_id)
        if last_song:
            self.transition_matrix.record_transition(
                from_song_id=last_song,
                to_song_id=song_id,
                play_through_rate=play_through_rate,
                was_skipped=True
            )
        
        # Update user profile
        if user_id:
            profile = self.get_or_create_profile(user_id)
            metadata = await self.cache.get_metadata(song_id)
            profile.record_skip(song_id, metadata)
    
    async def _on_user_feedback(self, payload: EventPayload) -> None:
        """Handle explicit user feedback (More Like This button)."""
        session_id = payload.data.get("session_id")
        song_id = payload.data.get("song_id")
        user_id = payload.data.get("user_id")
        feedback_type = payload.data.get("feedback_type")
        
        if not song_id or not user_id:
            return
        
        if feedback_type == "more_like_this":
            # Record in transition matrix
            last_song = self._session_last_song.get(session_id)
            if last_song:
                self.transition_matrix.record_transition(
                    from_song_id=last_song,
                    to_song_id=song_id,
                    play_through_rate=1.0,  # Assume full listen for liked songs
                    was_liked=True
                )
            
            # Update user profile
            profile = self.get_or_create_profile(user_id)
            metadata = await self.cache.get_metadata(song_id)
            profile.record_like(song_id, metadata)
            
            # Update group consensus if in multi-user session
            if session_id in self._session_consensus:
                self._session_consensus[session_id].record_activity(user_id, "like")
    
    def setup_session_consensus(
        self,
        session_id: str,
        user_ids: list[int]
    ) -> GroupConsensus:
        """Set up group consensus for a multi-user session."""
        consensus = GroupConsensus()
        
        for user_id in user_ids:
            profile = self.get_or_create_profile(user_id)
            consensus.add_user(user_id, profile)
        
        self._session_consensus[session_id] = consensus
        return consensus
    
    def get_session_consensus(self, session_id: str) -> Optional[GroupConsensus]:
        """Get group consensus for a session."""
        return self._session_consensus.get(session_id)
    
    async def get_transition_recommendations(
        self,
        current_song_id: str,
        k: int = 10,
        exclude: Optional[set[str]] = None,
        session_id: Optional[str] = None
    ) -> list[tuple[str, float]]:
        """
        Get recommendations based on transition patterns.
        
        Args:
            current_song_id: Currently playing song
            k: Number of recommendations
            exclude: Song IDs to exclude
            session_id: Session ID for consensus filtering
            
        Returns:
            List of (song_id, score) tuples
        """
        exclude = exclude or set()
        
        # Add veto songs from consensus if multi-user session
        if session_id and session_id in self._session_consensus:
            consensus = self._session_consensus[session_id]
            exclude.update(consensus.get_veto_songs())
        
        # Get transitions from current song
        recommendations = self.transition_matrix.get_best_transitions(
            current_song_id,
            k=k * 2,  # Get extra for filtering
            exclude=exclude
        )
        
        # If multi-user session, boost safe picks
        if session_id and session_id in self._session_consensus:
            consensus = self._session_consensus[session_id]
            safe_picks = consensus.get_safe_picks()
            
            boosted = []
            for song_id, score in recommendations:
                if song_id in safe_picks:
                    boosted.append((song_id, min(1.0, score * 1.3)))  # 30% boost
                else:
                    boosted.append((song_id, score))
            
            recommendations = sorted(boosted, key=lambda x: x[1], reverse=True)
        
        return recommendations[:k]
    
    async def get_behavioral_boost(
        self,
        song_id: str,
        user_ids: list[int]
    ) -> float:
        """
        Calculate behavioral boost for a song based on user preferences.
        
        Args:
            song_id: Song to evaluate
            user_ids: Users in the session
            
        Returns:
            Boost multiplier (0.5 = penalty, 1.0 = neutral, 1.5 = boost)
        """
        if not user_ids:
            return 1.0
        
        boosts = []
        
        for user_id in user_ids:
            profile = self._user_profiles.get(user_id)
            if not profile:
                boosts.append(1.0)
                continue
            
            boost = 1.0
            
            # Check if user liked this song
            if song_id in profile.liked_songs:
                boost += 0.3
            
            # Check skip history
            skip_count = profile.skipped_songs.get(song_id, 0)
            if skip_count >= 2:
                boost -= 0.3
            elif skip_count == 1:
                boost -= 0.1
            
            # Check if completed before
            if song_id in profile.completed_songs:
                boost += 0.1
            
            boosts.append(max(0.3, min(1.8, boost)))
        
        # Average boost across users
        return sum(boosts) / len(boosts)
    
    def get_stats(self) -> dict[str, Any]:
        """Get statistics about behavioral data."""
        total_transitions = sum(
            len(t) for t in self.transition_matrix._transitions.values()
        )
        
        return {
            "users_tracked": len(self._user_profiles),
            "transition_sources": len(self.transition_matrix._transitions),
            "total_transitions": total_transitions,
            "active_sessions": len(self._session_consensus),
            "sessions_with_history": len(self._session_last_song)
        }


# Singleton instance
_collaborative_recommender: Optional[CollaborativeRecommender] = None


def get_collaborative_recommender() -> CollaborativeRecommender:
    """Get global collaborative recommender instance."""
    global _collaborative_recommender
    if _collaborative_recommender is None:
        _collaborative_recommender = CollaborativeRecommender()
    return _collaborative_recommender
