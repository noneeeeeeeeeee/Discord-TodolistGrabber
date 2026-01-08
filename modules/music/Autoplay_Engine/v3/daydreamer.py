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

API Sources:
- Deezer: chart/0/tracks, chart/{genre_id}/tracks, search, genre
- Last.fm: artist.getSimilar, tag.getTopTracks
"""

import asyncio
import logging
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote_plus

import aiohttp

from .cache_manager import CacheManager, get_cache_manager
from .constants import AnalysisMode, AnalysisPriority, EventType, SongMetadata, V3Config
from .event_bus import EventBus, EventPayload
from .mappings import MappingsManager, SongIdentifier, get_mappings_manager
from .song_analyzer import SongAnalyzer, get_song_analyzer

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
    5. Chart Exploration: Discover popular tracks from charts
    
    Runs as a background task, pausing when system is busy
    with active recommendations. Prioritizes analysis at
    LOW priority to not interfere with active sessions.
    
    Configuration:
    - Exploration interval: 30 seconds when idle
    - Max concurrent explorations: 3
    - Target genre balance: 20% max per genre
    
    API Sources:
    - Deezer API: chart, genre, search
    - Last.fm API: artist.getSimilar, tag.getTopTracks
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
    CHART_EXPLORATION_QUOTA = 2
    
    # API endpoints
    DEEZER_API = "https://api.deezer.com"
    LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
    
    # Deezer genre IDs (common ones)
    GENRE_IDS = {
        "pop": 132,
        "rock": 152,
        "hip-hop": 116,
        "rap": 116,
        "r&b": 165,
        "electronic": 106,
        "dance": 113,
        "jazz": 129,
        "classical": 98,
        "metal": 464,
        "reggae": 144,
        "blues": 153,
        "country": 84,
        "folk": 466,
        "latin": 197,
        "soul": 169,
        "funk": 85,
        "indie": 467,
        "alternative": 85,
    }
    
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
        
        # HTTP session for API calls
        self._session: Optional[aiohttp.ClientSession] = None
        
        # API keys
        self._lastfm_key: Optional[str] = None
        
        # Statistics
        self._stats = {
            "rounds": 0,
            "songs_discovered": 0,
            "api_calls": 0,
            "api_errors": 0,
            "by_strategy": defaultdict(int)
        }
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and start background exploration."""
        if self._initialized:
            return
        
        # Initialize HTTP session
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        
        # Load Last.fm API key
        self._lastfm_key = os.environ.get("LASTFM_API_KEY")
        if not self._lastfm_key:
            logger.warning("LASTFM_API_KEY not set - artist similarity disabled")
        
        await self.cache.initialize()
        await self.mappings.initialize()
        await self.analyzer.initialize()
        
        # Analyze current cache genre distribution
        await self._analyze_cache_distribution()
        
        # Start background exploration
        self._running = True
        self._exploration_task = asyncio.create_task(self._exploration_loop())
        
        self._initialized = True
        logger.info("Daydreamer initialized with real API access")
    
    async def shutdown(self) -> None:
        """Stop background exploration."""
        self._running = False
        
        if self._exploration_task:
            self._exploration_task.cancel()
            try:
                await self._exploration_task
            except asyncio.CancelledError:
                pass
        
        if self._session:
            await self._session.close()
            self._session = None
        
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
        
        # Strategy 3: Chart exploration (new)
        chart_songs = await self._explore_charts()
        discovered.extend(chart_songs)
        self._stats["by_strategy"]["chart"] += len(chart_songs)
        
        # Strategy 4: Random discovery
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
        """Search for a random song in a genre using Deezer chart API."""
        if not self._session:
            return None
        
        try:
            # Map genre name to Deezer genre ID
            genre_lower = genre.lower().replace(" ", "-")
            genre_id = self.GENRE_IDS.get(genre_lower)
            
            if not genre_id:
                # Try searching for the genre instead
                return await self._search_deezer_by_query(f"genre:{genre}")
            
            # Fetch chart for this genre
            url = f"{self.DEEZER_API}/chart/{genre_id}/tracks"
            
            async with self._session.get(url) as response:
                self._stats["api_calls"] += 1
                
                if response.status == 200:
                    data = await response.json()
                    tracks = data.get("data", [])
                    
                    if tracks:
                        # Pick a random track from the chart
                        track = random.choice(tracks)
                        return SongIdentifier(
                            deezer_id=str(track.get("id")),
                            title=track.get("title"),
                            artist=track.get("artist", {}).get("name"),
                            album=track.get("album", {}).get("title"),
                            preview_url=track.get("preview"),
                            duration_ms=track.get("duration", 0) * 1000
                        )
                else:
                    self._stats["api_errors"] += 1
                    logger.warning(f"Deezer genre chart error: {response.status}")
                    
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Genre search error for '{genre}': {e}")
        
        return None
    
    async def _search_deezer_by_query(self, query: str) -> Optional[SongIdentifier]:
        """Search Deezer with a custom query."""
        if not self._session:
            return None
        
        try:
            encoded_query = quote_plus(query)
            url = f"{self.DEEZER_API}/search?q={encoded_query}&limit=20"
            
            async with self._session.get(url) as response:
                self._stats["api_calls"] += 1
                
                if response.status == 200:
                    data = await response.json()
                    tracks = data.get("data", [])
                    
                    if tracks:
                        track = random.choice(tracks)
                        return SongIdentifier(
                            deezer_id=str(track.get("id")),
                            title=track.get("title"),
                            artist=track.get("artist", {}).get("name"),
                            album=track.get("album", {}).get("title"),
                            preview_url=track.get("preview"),
                            duration_ms=track.get("duration", 0) * 1000
                        )
                        
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Deezer search error: {e}")
        
        return None
    
    async def _explore_similar_artists(self) -> list[ExplorationSuggestion]:
        """Explore songs from artists similar to popular ones using Last.fm."""
        suggestions = []
        
        if not self._lastfm_key or not self._session:
            return suggestions
        
        # Get artists from explored list or pick from cache
        artists_to_explore = list(self._explored_artists)[:5]
        
        if not artists_to_explore:
            # No artists tracked yet, skip this round
            return suggestions
        
        try:
            # Pick a random artist to explore
            artist_name = random.choice(artists_to_explore)
            
            # Get similar artists from Last.fm
            similar_artists = await self._get_similar_artists_lastfm(artist_name)
            
            if similar_artists:
                # Pick up to 2 similar artists
                for similar_artist in similar_artists[:self.ARTIST_EXPANSION_QUOTA]:
                    # Search for a popular song by this artist
                    song = await self._get_artist_top_track(similar_artist)
                    if song:
                        suggestions.append(ExplorationSuggestion(
                            identifier=song,
                            metadata=None,
                            exploration_reason=f"Similar to {artist_name}: {similar_artist}",
                            discovery_time=time.time()
                        ))
                        
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Artist expansion error: {e}")
        
        return suggestions
    
    async def _get_similar_artists_lastfm(self, artist_name: str) -> list[str]:
        """Get similar artists from Last.fm."""
        if not self._lastfm_key or not self._session:
            return []
        
        try:
            params = {
                "method": "artist.getSimilar",
                "api_key": self._lastfm_key,
                "artist": artist_name,
                "limit": 10,
                "format": "json"
            }
            
            async with self._session.get(self.LASTFM_API, params=params) as response:
                self._stats["api_calls"] += 1
                
                if response.status == 200:
                    data = await response.json()
                    similar = data.get("similarartists", {}).get("artist", [])
                    
                    # Extract artist names
                    return [a.get("name") for a in similar if a.get("name")]
                else:
                    self._stats["api_errors"] += 1
                    
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Last.fm artist.getSimilar error: {e}")
        
        return []
    
    async def _get_artist_top_track(self, artist_name: str) -> Optional[SongIdentifier]:
        """Get a top track for an artist from Deezer."""
        if not self._session:
            return None
        
        try:
            # Search Deezer for artist
            encoded = quote_plus(f'artist:"{artist_name}"')
            url = f"{self.DEEZER_API}/search?q={encoded}&limit=10"
            
            async with self._session.get(url) as response:
                self._stats["api_calls"] += 1
                
                if response.status == 200:
                    data = await response.json()
                    tracks = data.get("data", [])
                    
                    if tracks:
                        # Pick a random track from top results
                        track = random.choice(tracks)
                        return SongIdentifier(
                            deezer_id=str(track.get("id")),
                            title=track.get("title"),
                            artist=track.get("artist", {}).get("name"),
                            album=track.get("album", {}).get("title"),
                            preview_url=track.get("preview"),
                            duration_ms=track.get("duration", 0) * 1000
                        )
                        
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Deezer artist search error: {e}")
        
        return None
    
    async def _random_exploration(self) -> list[ExplorationSuggestion]:
        """Completely random song discovery using Deezer charts."""
        suggestions = []
        
        if not self._session:
            return suggestions
        
        try:
            # Fetch global chart
            url = f"{self.DEEZER_API}/chart/0/tracks"
            
            async with self._session.get(url) as response:
                self._stats["api_calls"] += 1
                
                if response.status == 200:
                    data = await response.json()
                    tracks = data.get("data", [])
                    
                    if tracks:
                        # Pick random tracks from the chart
                        sample_size = min(self.RANDOM_DISCOVERY_QUOTA, len(tracks))
                        sampled = random.sample(tracks, sample_size)
                        
                        for track in sampled:
                            identifier = SongIdentifier(
                                deezer_id=str(track.get("id")),
                                title=track.get("title"),
                                artist=track.get("artist", {}).get("name"),
                                album=track.get("album", {}).get("title"),
                                preview_url=track.get("preview"),
                                duration_ms=track.get("duration", 0) * 1000
                            )
                            suggestions.append(ExplorationSuggestion(
                                identifier=identifier,
                                metadata=None,
                                exploration_reason="Chart discovery",
                                discovery_time=time.time()
                            ))
                else:
                    self._stats["api_errors"] += 1
                    
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Random exploration error: {e}")
        
        return suggestions
    
    async def _explore_charts(self) -> list[ExplorationSuggestion]:
        """Explore genre-specific charts for variety."""
        suggestions = []
        
        if not self._session:
            return suggestions
        
        try:
            # Pick random genres to explore
            genres = list(self.GENRE_IDS.keys())
            selected_genres = random.sample(genres, min(self.CHART_EXPLORATION_QUOTA, len(genres)))
            
            for genre in selected_genres:
                genre_id = self.GENRE_IDS[genre]
                url = f"{self.DEEZER_API}/chart/{genre_id}/tracks"
                
                try:
                    async with self._session.get(url) as response:
                        self._stats["api_calls"] += 1
                        
                        if response.status == 200:
                            data = await response.json()
                            tracks = data.get("data", [])
                            
                            if tracks:
                                # Pick a random track
                                track = random.choice(tracks)
                                identifier = SongIdentifier(
                                    deezer_id=str(track.get("id")),
                                    title=track.get("title"),
                                    artist=track.get("artist", {}).get("name"),
                                    album=track.get("album", {}).get("title"),
                                    preview_url=track.get("preview"),
                                    duration_ms=track.get("duration", 0) * 1000
                                )
                                suggestions.append(ExplorationSuggestion(
                                    identifier=identifier,
                                    metadata=None,
                                    exploration_reason=f"Chart: {genre}",
                                    discovery_time=time.time()
                                ))
                except Exception as e:
                    self._stats["api_errors"] += 1
                    logger.debug(f"Genre chart error for {genre}: {e}")
                    
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Chart exploration error: {e}")
        
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
        """Get songs from artists similar to given artist using Last.fm + Deezer."""
        suggestions = []
        
        if not self._lastfm_key or not self._session:
            return suggestions
        
        try:
            # Get similar artists from Last.fm
            similar_artists = await self._get_similar_artists_lastfm(artist_name)
            
            for similar_artist in similar_artists[:3]:
                # Get a top track from each similar artist
                song = await self._get_artist_top_track(similar_artist)
                if song:
                    suggestions.append(ExplorationSuggestion(
                        identifier=song,
                        metadata=None,
                        exploration_reason=f"Similar to {artist_name}",
                        discovery_time=time.time()
                    ))
                    
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Similar artist songs error: {e}")
        
        return suggestions
    
    async def _get_genre_songs(
        self,
        genre: str
    ) -> list[ExplorationSuggestion]:
        """Get songs from a genre using Deezer."""
        suggestions = []
        
        if not self._session:
            return suggestions
        
        try:
            # Try genre chart first
            genre_lower = genre.lower().replace(" ", "-")
            genre_id = self.GENRE_IDS.get(genre_lower)
            
            if genre_id:
                url = f"{self.DEEZER_API}/chart/{genre_id}/tracks"
                
                async with self._session.get(url) as response:
                    self._stats["api_calls"] += 1
                    
                    if response.status == 200:
                        data = await response.json()
                        tracks = data.get("data", [])
                        
                        # Get a few random tracks
                        sample_size = min(3, len(tracks))
                        if tracks:
                            for track in random.sample(tracks, sample_size):
                                identifier = SongIdentifier(
                                    deezer_id=str(track.get("id")),
                                    title=track.get("title"),
                                    artist=track.get("artist", {}).get("name"),
                                    album=track.get("album", {}).get("title"),
                                    preview_url=track.get("preview"),
                                    duration_ms=track.get("duration", 0) * 1000
                                )
                                suggestions.append(ExplorationSuggestion(
                                    identifier=identifier,
                                    metadata=None,
                                    exploration_reason=f"Genre: {genre}",
                                    discovery_time=time.time()
                                ))
            else:
                # Fall back to search
                song = await self._search_deezer_by_query(f"genre:{genre}")
                if song:
                    suggestions.append(ExplorationSuggestion(
                        identifier=song,
                        metadata=None,
                        exploration_reason=f"Genre search: {genre}",
                        discovery_time=time.time()
                    ))
                    
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Genre songs error: {e}")
        
        return suggestions
    
    async def get_tag_top_tracks(
        self,
        tag: str,
        limit: int = 10
    ) -> list[SongIdentifier]:
        """
        Get top tracks for a tag/genre using Last.fm.
        
        Args:
            tag: Tag name (genre, mood, etc.)
            limit: Maximum tracks to return
            
        Returns:
            List of SongIdentifier for top tracks
        """
        if not self._lastfm_key or not self._session:
            return []
        
        try:
            params = {
                "method": "tag.getTopTracks",
                "api_key": self._lastfm_key,
                "tag": tag,
                "limit": limit,
                "format": "json"
            }
            
            async with self._session.get(self.LASTFM_API, params=params) as response:
                self._stats["api_calls"] += 1
                
                if response.status == 200:
                    data = await response.json()
                    tracks = data.get("tracks", {}).get("track", [])
                    
                    results = []
                    for track in tracks:
                        identifier = SongIdentifier(
                            lastfm_mbid=track.get("mbid"),
                            title=track.get("name"),
                            artist=track.get("artist", {}).get("name")
                        )
                        results.append(identifier)
                    
                    return results
                    
        except Exception as e:
            self._stats["api_errors"] += 1
            logger.warning(f"Last.fm tag.getTopTracks error: {e}")
        
        return []
    
    def get_stats(self) -> dict[str, Any]:
        """Get exploration statistics."""
        return {
            "rounds": self._stats["rounds"],
            "songs_discovered": self._stats["songs_discovered"],
            "api_calls": self._stats["api_calls"],
            "api_errors": self._stats["api_errors"],
            "by_strategy": dict(self._stats["by_strategy"]),
            "pending_suggestions": len(self._pending_suggestions),
            "explored_artists": len(self._explored_artists),
            "genre_distribution": dict(self._genre_distribution),
            "running": self._running,
            "paused": self._paused,
            "has_lastfm_key": self._lastfm_key is not None,
            "has_session": self._session is not None
        }


# Singleton instance
_daydreamer: Optional[Daydreamer] = None


def get_daydreamer() -> Daydreamer:
    """Get global daydreamer instance."""
    global _daydreamer
    if _daydreamer is None:
        _daydreamer = Daydreamer()
    return _daydreamer
