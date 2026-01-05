"""
V3 Autoplay Engine - Daydreamer

Background exploration module that discovers new songs to expand
the recommendation pool. Runs during idle time to:
- Explore genres underrepresented in the cache
- Pre-analyze songs for future sessions
- Balance genre distribution in the song library
- Find hidden gems through random exploration

Named "daydreamer" because it explores possibilities when
the system isn't busy with active recommendations.
"""

import asyncio
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

from .cache_manager import CacheManager, get_cache_manager
from .constants import AnalysisMode, EventType, SongMetadata, V3Config
from .event_bus import EventBus, EventPayload
from .mappings import MappingsManager, SongIdentifier, get_mappings_manager
from .song_analyzer import AnalysisPriority, SongAnalyzer, get_song_analyzer

logger = logging.getLogger(__name__)


@dataclass
class ExplorationSuggestion:
    """A song suggested by the daydreamer."""
    identifier: SongIdentifier
    metadata: Optional[SongMetadata]
    exploration_reason: str
    discovery_time: float


class Daydreamer:
    """
    Background exploration module for cache enrichment.
    
    Strategies:
    1. Genre Balancing: Find songs in underrepresented genres
    2. Artist Expansion: Explore similar artists to popular ones
    3. Random Discovery: Periodic random exploration
    4. Trend Following: Explore based on session patterns
    
    Runs as a background task, pausing when system is busy
    with active recommendations. Prioritizes analysis at
    LOW priority to not interfere with active sessions.
    
    Configuration:
    - Exploration interval: 30 seconds when idle
    - Max concurrent explorations: 3
    - Target genre balance: 20% max per genre
    """
    
    # Configuration
    EXPLORATION_INTERVAL = 30.0  # Seconds between exploration rounds
    MAX_CONCURRENT = 3
    TARGET_GENRE_BALANCE = 0.20  # Max 20% of cache per genre
    MIN_CACHE_SIZE = 100  # Minimum songs before balancing matters
    
    # Exploration quotas per round
    GENRE_BALANCE_QUOTA = 2
    ARTIST_EXPANSION_QUOTA = 1
    RANDOM_DISCOVERY_QUOTA = 1
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        cache: Optional[CacheManager] = None,
        mappings: Optional[MappingsManager] = None,
        analyzer: Optional[SongAnalyzer] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize daydreamer.
        
        Args:
            config: V3 configuration
            cache: Cache manager for genre analysis
            mappings: Mappings manager for song discovery
            analyzer: Song analyzer for queuing analysis
            event_bus: Event bus for notifications
        """
        self.config = config or V3Config()
        self.cache = cache or get_cache_manager()
        self.mappings = mappings or get_mappings_manager()
        self.analyzer = analyzer or get_song_analyzer()
        self.event_bus = event_bus or EventBus()
        
        # Exploration state
        self._genre_distribution: dict[str, int] = defaultdict(int)
        self._explored_artists: set[str] = set()
        self._pending_suggestions: list[ExplorationSuggestion] = []
        
        # Background task
        self._exploration_task: Optional[asyncio.Task] = None
        self._running = False
        self._paused = False
        
        # Statistics
        self._stats = {
            "rounds": 0,
            "songs_discovered": 0,
            "by_strategy": defaultdict(int)
        }
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and start background exploration."""
        if self._initialized:
            return
        
        await self.cache.initialize()
        await self.mappings.initialize()
        await self.analyzer.initialize()
        
        # Analyze current cache genre distribution
        await self._analyze_cache_distribution()
        
        # Start background exploration
        self._running = True
        self._exploration_task = asyncio.create_task(self._exploration_loop())
        
        self._initialized = True
        logger.info("Daydreamer initialized")
    
    async def shutdown(self) -> None:
        """Stop background exploration."""
        self._running = False
        
        if self._exploration_task:
            self._exploration_task.cancel()
            try:
                await self._exploration_task
            except asyncio.CancelledError:
                pass
        
        self._initialized = False
        logger.info("Daydreamer shutdown")
    
    def pause(self) -> None:
        """Pause exploration (when system is busy)."""
        self._paused = True
        logger.debug("Daydreamer paused")
    
    def resume(self) -> None:
        """Resume exploration."""
        self._paused = False
        logger.debug("Daydreamer resumed")
    
    async def _analyze_cache_distribution(self) -> None:
        """Analyze genre distribution in cache."""
        self._genre_distribution.clear()
        
        # Get all metadata from cache
        # This is simplified - in practice would iterate through cache
        stats = await self.cache.get_stats()
        metadata_count = stats.get("metadata_count", 0)
        
        if metadata_count < self.MIN_CACHE_SIZE:
            logger.debug(f"Cache too small ({metadata_count}) for distribution analysis")
            return
        
        # Would need to iterate cache to build actual distribution
        # For now, this is a placeholder
        logger.info(f"Analyzed cache distribution: {len(self._genre_distribution)} genres")
    
    async def _exploration_loop(self) -> None:
        """Background exploration loop."""
        while self._running:
            try:
                # Check if paused
                if self._paused:
                    await asyncio.sleep(5)
                    continue
                
                # Run exploration round
                await self._run_exploration_round()
                
                self._stats["rounds"] += 1
                
                # Wait for next round
                await asyncio.sleep(self.EXPLORATION_INTERVAL)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Exploration error: {e}")
                await asyncio.sleep(60)  # Longer wait on error
    
    async def _run_exploration_round(self) -> None:
        """Run a single exploration round."""
        discovered = []
        
        # Strategy 1: Genre balancing
        if len(self._genre_distribution) > 0:
            genre_songs = await self._explore_underrepresented_genres()
            discovered.extend(genre_songs)
            self._stats["by_strategy"]["genre_balance"] += len(genre_songs)
        
        # Strategy 2: Artist expansion
        artist_songs = await self._explore_similar_artists()
        discovered.extend(artist_songs)
        self._stats["by_strategy"]["artist_expansion"] += len(artist_songs)
        
        # Strategy 3: Random discovery
        random_songs = await self._random_exploration()
        discovered.extend(random_songs)
        self._stats["by_strategy"]["random"] += len(random_songs)
        
        # Queue all discovered songs for analysis
        for song in discovered:
            await self.analyzer.enqueue(
                song.identifier,
                priority=AnalysisPriority.BATCH,
                mode=AnalysisMode.FULL
            )
            
            self._pending_suggestions.append(song)
            self._stats["songs_discovered"] += 1
        
        # Limit pending suggestions
        if len(self._pending_suggestions) > 50:
            self._pending_suggestions = self._pending_suggestions[-50:]
        
        if discovered:
            logger.debug(f"Exploration round: discovered {len(discovered)} songs")
    
    async def _explore_underrepresented_genres(self) -> list[ExplorationSuggestion]:
        """Find songs in underrepresented genres."""
        if not self._genre_distribution:
            return []
        
        suggestions = []
        
        # Find underrepresented genres
        total_songs = sum(self._genre_distribution.values())
        if total_songs == 0:
            return []
        
        underrepresented = []
        for genre, count in self._genre_distribution.items():
            ratio = count / total_songs
            if ratio < self.TARGET_GENRE_BALANCE:
                underrepresented.append((genre, ratio))
        
        # Sort by most underrepresented
        underrepresented.sort(key=lambda x: x[1])
        
        # Try to find songs for top underrepresented genres
        for genre, ratio in underrepresented[:self.GENRE_BALANCE_QUOTA]:
            song = await self._search_genre(genre)
            if song:
                suggestions.append(ExplorationSuggestion(
                    identifier=song,
                    metadata=None,
                    exploration_reason=f"Genre balance: {genre} ({ratio:.1%})",
                    discovery_time=time.time()
                ))
        
        return suggestions
    
    async def _search_genre(self, genre: str) -> Optional[SongIdentifier]:
        """Search for a random song in a genre using Deezer."""
        # This would use Deezer's genre/chart APIs
        # Simplified placeholder implementation
        return None
    
    async def _explore_similar_artists(self) -> list[ExplorationSuggestion]:
        """Explore songs from artists similar to popular ones."""
        suggestions = []
        
        # Would need to:
        # 1. Identify popular artists in cache
        # 2. Use Last.fm artist.getSimilar
        # 3. Get songs from similar artists
        
        return suggestions
    
    async def _random_exploration(self) -> list[ExplorationSuggestion]:
        """Completely random song discovery."""
        suggestions = []
        
        # Deezer has chart endpoints that could be used for discovery
        # This is a placeholder implementation
        
        return suggestions
    
    async def get_suggestions(
        self,
        session_id: str,
        count: int = 3,
        exclude: Optional[set[str]] = None
    ) -> list[ExplorationSuggestion]:
        """
        Get exploration suggestions for a session.
        
        Called by the recommender in extended sessions.
        
        Args:
            session_id: Session identifier
            count: Number of suggestions
            exclude: Song IDs to exclude
            
        Returns:
            List of exploration suggestions
        """
        exclude = exclude or set()
        
        # Filter and return pending suggestions
        valid = [
            s for s in self._pending_suggestions
            if s.identifier.primary_id not in exclude
        ]
        
        # Take from the end (most recent)
        selected = valid[-count:] if len(valid) > count else valid
        
        # Remove selected from pending
        for s in selected:
            if s in self._pending_suggestions:
                self._pending_suggestions.remove(s)
        
        return selected
    
    async def seed_exploration(
        self,
        artists: list[str],
        genres: list[str]
    ) -> None:
        """
        Seed exploration with session preferences.
        
        Called when a session ends to inform future exploration.
        
        Args:
            artists: Popular artists from session
            genres: Popular genres from session
        """
        # Update genre targets
        for genre in genres:
            self._genre_distribution[genre] = \
                self._genre_distribution.get(genre, 0)
        
        # Track explored artists
        self._explored_artists.update(artists)
    
    async def explore_for_session(
        self,
        session_id: str,
        preferences: dict[str, Any]
    ) -> list[ExplorationSuggestion]:
        """
        Perform targeted exploration based on session preferences.
        
        This is a more focused exploration that runs during
        extended sessions to find diverse content.
        
        Args:
            session_id: Session identifier
            preferences: Session preference data
            
        Returns:
            List of exploration suggestions
        """
        suggestions = []
        
        # Get top genres/artists to explore around
        top_genres = preferences.get("top_genres", [])
        top_artists = preferences.get("top_artists", [])
        
        # Find songs from similar artists
        for artist_name, _ in top_artists[:2]:
            similar = await self._get_similar_artist_songs(artist_name)
            suggestions.extend(similar[:2])
        
        # Find songs in related genres
        for genre_name, _ in top_genres[:2]:
            genre_songs = await self._get_genre_songs(genre_name)
            suggestions.extend(genre_songs[:2])
        
        return suggestions
    
    async def _get_similar_artist_songs(
        self,
        artist_name: str
    ) -> list[ExplorationSuggestion]:
        """Get songs from artists similar to given artist."""
        # Would use Last.fm artist.getSimilar
        return []
    
    async def _get_genre_songs(
        self,
        genre: str
    ) -> list[ExplorationSuggestion]:
        """Get songs from a genre."""
        # Would use Deezer genre endpoints
        return []
    
    def get_stats(self) -> dict[str, Any]:
        """Get exploration statistics."""
        return {
            "rounds": self._stats["rounds"],
            "songs_discovered": self._stats["songs_discovered"],
            "by_strategy": dict(self._stats["by_strategy"]),
            "pending_suggestions": len(self._pending_suggestions),
            "explored_artists": len(self._explored_artists),
            "genre_distribution": dict(self._genre_distribution),
            "running": self._running,
            "paused": self._paused
        }


# Singleton instance
_daydreamer: Optional[Daydreamer] = None


def get_daydreamer() -> Daydreamer:
    """Get global daydreamer instance."""
    global _daydreamer
    if _daydreamer is None:
        _daydreamer = Daydreamer()
    return _daydreamer
