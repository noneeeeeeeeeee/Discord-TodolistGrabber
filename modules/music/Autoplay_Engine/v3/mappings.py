"""
V3 Autoplay Engine - Mappings Manager

Handles bidirectional ID mapping between music platforms:
- Deezer (primary - has free 30s previews)
- YouTube (playback via Lavalink)
- Last.fm (similarity data)
- ISRC (universal identifier)

Uses Lavalink's search for YouTube resolution and fuzzy matching
for cross-platform song identification.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional
from urllib.parse import quote_plus

import aiohttp

from .cache_manager import CacheManager, get_cache_manager
from .constants import EventType
from .event_bus import EventBus, EventPayload

logger = logging.getLogger(__name__)


@dataclass
class SongIdentifier:
    """Cross-platform song identifier."""
    deezer_id: Optional[str] = None
    youtube_id: Optional[str] = None
    lastfm_mbid: Optional[str] = None
    isrc: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    duration_ms: Optional[int] = None
    preview_url: Optional[str] = None
    
    @property
    def primary_id(self) -> Optional[str]:
        """Get the primary identifier (Deezer preferred)."""
        return self.deezer_id or self.youtube_id or self.lastfm_mbid or self.isrc
    
    def is_complete(self) -> bool:
        """Check if we have all platform IDs."""
        return all([
            self.deezer_id,
            self.youtube_id,
            self.title,
            self.artist
        ])


class MappingsManager:
    """
    Manages cross-platform song ID mappings with fuzzy matching.
    
    Resolution Priority:
    1. Check cache for existing mapping
    2. Use ISRC for exact matching (most reliable)
    3. Use Deezer API for metadata and ISRC lookup
    4. Use Lavalink ytsearch for YouTube resolution
    5. Fuzzy match title/artist as fallback
    
    All mappings are stored bidirectionally in cache for quick lookup.
    """
    
    # API endpoints
    DEEZER_API = "https://api.deezer.com"
    LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
    
    # Fuzzy matching thresholds
    FUZZY_THRESHOLD = 0.85
    TITLE_WEIGHT = 0.6
    ARTIST_WEIGHT = 0.4
    
    def __init__(
        self,
        cache_manager: Optional[CacheManager] = None,
        event_bus: Optional[EventBus] = None,
        lavalink_client: Optional[Any] = None
    ):
        """
        Initialize mappings manager.
        
        Args:
            cache_manager: Cache for storing mappings
            event_bus: Event bus for notifications
            lavalink_client: Lavalink client for YouTube search
        """
        self.cache = cache_manager or get_cache_manager()
        self.event_bus = event_bus or EventBus()
        self.lavalink = lavalink_client
        
        # HTTP session for API calls
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Last.fm API key (optional, for similarity data)
        self._lastfm_key: Optional[str] = None
        
        # Statistics
        self._stats = {
            "cache_hits": 0,
            "deezer_lookups": 0,
            "youtube_searches": 0,
            "fuzzy_matches": 0,
            "failures": 0
        }
        
        self._initialized = False
    
    async def initialize(self, lastfm_key: Optional[str] = None) -> None:
        """Initialize HTTP session and load Last.fm key."""
        if self._initialized:
            return
        
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        
        if lastfm_key:
            self._lastfm_key = lastfm_key
        else:
            import os
            self._lastfm_key = os.environ.get("LASTFM_API_KEY")
        
        await self.cache.initialize()
        
        self._initialized = True
        logger.info("Mappings manager initialized")
    
    async def shutdown(self) -> None:
        """Clean up resources."""
        if self._session:
            await self._session.close()
            self._session = None
        
        self._initialized = False
    
    def set_lavalink(self, client: Any) -> None:
        """Set Lavalink client for YouTube searches."""
        self.lavalink = client
    
    async def resolve_song(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        deezer_id: Optional[str] = None,
        youtube_id: Optional[str] = None,
        isrc: Optional[str] = None
    ) -> Optional[SongIdentifier]:
        """
        Resolve a song to get all platform IDs.
        
        Tries multiple strategies to find complete mappings.
        
        Args:
            title: Song title
            artist: Artist name
            deezer_id: Known Deezer ID
            youtube_id: Known YouTube ID
            isrc: Known ISRC
            
        Returns:
            SongIdentifier with all available platform IDs
        """
        await self.initialize()
        
        # Check cache first
        cached = await self._check_cache(deezer_id, youtube_id, isrc)
        if cached and cached.is_complete():
            self._stats["cache_hits"] += 1
            return cached
        
        # Start building identifier
        identifier = cached or SongIdentifier(
            deezer_id=deezer_id,
            youtube_id=youtube_id,
            isrc=isrc,
            title=title,
            artist=artist
        )
        
        # Strategy 1: Resolve via Deezer
        if deezer_id and not identifier.is_complete():
            deezer_data = await self._fetch_deezer_track(deezer_id)
            if deezer_data:
                identifier = self._merge_deezer_data(identifier, deezer_data)
        
        # Strategy 2: Search Deezer by ISRC
        if isrc and not identifier.deezer_id:
            deezer_data = await self._search_deezer_by_isrc(isrc)
            if deezer_data:
                identifier = self._merge_deezer_data(identifier, deezer_data)
        
        # Strategy 3: Search Deezer by title/artist
        if title and artist and not identifier.deezer_id:
            deezer_data = await self._search_deezer(title, artist)
            if deezer_data:
                identifier = self._merge_deezer_data(identifier, deezer_data)
        
        # Strategy 4: Resolve YouTube via Lavalink
        if not identifier.youtube_id and identifier.title and identifier.artist:
            yt_id = await self._search_youtube(identifier.title, identifier.artist)
            if yt_id:
                identifier.youtube_id = yt_id
        
        # Strategy 5: Get Last.fm mbid if we have title/artist
        if not identifier.lastfm_mbid and identifier.title and identifier.artist:
            mbid = await self._get_lastfm_mbid(identifier.title, identifier.artist)
            if mbid:
                identifier.lastfm_mbid = mbid
        
        # Store mapping if we got useful data
        if identifier.primary_id:
            await self._store_mapping(identifier)
        else:
            self._stats["failures"] += 1
        
        return identifier
    
    async def _check_cache(
        self,
        deezer_id: Optional[str],
        youtube_id: Optional[str],
        isrc: Optional[str]
    ) -> Optional[SongIdentifier]:
        """Check cache for existing mapping."""
        keys_to_check = []
        
        if deezer_id:
            keys_to_check.append(f"deezer:{deezer_id}")
        if youtube_id:
            keys_to_check.append(f"youtube:{youtube_id}")
        if isrc:
            keys_to_check.append(f"isrc:{isrc}")
        
        for key in keys_to_check:
            mapping = await self.cache.get_mapping(key)
            if mapping:
                return SongIdentifier(
                    deezer_id=mapping.get("deezer_id"),
                    youtube_id=mapping.get("youtube_id"),
                    lastfm_mbid=mapping.get("lastfm_id"),
                    isrc=mapping.get("isrc"),
                    title=mapping.get("title"),
                    artist=mapping.get("artist"),
                    album=mapping.get("album"),
                    duration_ms=mapping.get("duration_ms"),
                    preview_url=mapping.get("preview_url")
                )
        
        return None
    
    async def _store_mapping(self, identifier: SongIdentifier) -> None:
        """Store mapping in cache."""
        await self.cache.set_mapping(
            deezer_id=identifier.deezer_id,
            youtube_id=identifier.youtube_id,
            lastfm_id=identifier.lastfm_mbid,
            isrc=identifier.isrc
        )
        
        # Also store metadata for quick access
        if identifier.deezer_id:
            # Store additional metadata that's not in the mapping
            pass  # Metadata is stored separately via cache_manager.set_metadata
    
    async def _fetch_deezer_track(self, track_id: str) -> Optional[dict]:
        """Fetch track data from Deezer API."""
        try:
            url = f"{self.DEEZER_API}/track/{track_id}"
            async with self._session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "error" not in data:
                        self._stats["deezer_lookups"] += 1
                        return data
        except Exception as e:
            logger.warning(f"Deezer API error: {e}")
        
        return None
    
    async def _search_deezer(
        self,
        title: str,
        artist: str
    ) -> Optional[dict]:
        """Search Deezer for a track by title and artist."""
        try:
            query = quote_plus(f'track:"{title}" artist:"{artist}"')
            url = f"{self.DEEZER_API}/search?q={query}&limit=5"
            
            async with self._session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("data", [])
                    
                    if results:
                        self._stats["deezer_lookups"] += 1
                        
                        # Find best match using fuzzy matching
                        best_match = self._find_best_match(
                            results, title, artist
                        )
                        
                        if best_match:
                            # Fetch full track data
                            return await self._fetch_deezer_track(
                                str(best_match["id"])
                            )
        except Exception as e:
            logger.warning(f"Deezer search error: {e}")
        
        return None
    
    async def _search_deezer_by_isrc(self, isrc: str) -> Optional[dict]:
        """Search Deezer by ISRC (most reliable)."""
        try:
            url = f"{self.DEEZER_API}/track/isrc:{isrc}"
            async with self._session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "error" not in data:
                        self._stats["deezer_lookups"] += 1
                        return data
        except Exception as e:
            logger.warning(f"Deezer ISRC lookup error: {e}")
        
        return None
    
    def _merge_deezer_data(
        self,
        identifier: SongIdentifier,
        deezer_data: dict
    ) -> SongIdentifier:
        """Merge Deezer API response into identifier."""
        identifier.deezer_id = str(deezer_data.get("id"))
        identifier.title = identifier.title or deezer_data.get("title")
        identifier.artist = identifier.artist or deezer_data.get("artist", {}).get("name")
        identifier.album = identifier.album or deezer_data.get("album", {}).get("title")
        identifier.duration_ms = deezer_data.get("duration", 0) * 1000
        identifier.preview_url = deezer_data.get("preview")
        identifier.isrc = identifier.isrc or deezer_data.get("isrc")
        
        return identifier
    
    async def _search_youtube(
        self,
        title: str,
        artist: str
    ) -> Optional[str]:
        """Search YouTube via Lavalink."""
        if not self.lavalink:
            logger.debug("No Lavalink client available for YouTube search")
            return None
        
        try:
            query = f"ytsearch:{artist} - {title}"
            
            # Use Lavalink's search
            results = await self.lavalink.get_tracks(query)
            
            if results and results.tracks:
                self._stats["youtube_searches"] += 1
                
                # Find best match
                for track in results.tracks[:5]:
                    # Extract video ID from URI
                    if "youtube.com" in track.uri or "youtu.be" in track.uri:
                        video_id = self._extract_youtube_id(track.uri)
                        
                        # Verify match quality
                        track_title = track.title.lower()
                        if (
                            self._normalize(title) in track_title or
                            self._normalize(artist) in track_title
                        ):
                            return video_id
                
                # Return first result if no exact match
                if results.tracks:
                    return self._extract_youtube_id(results.tracks[0].uri)
                    
        except Exception as e:
            logger.warning(f"Lavalink search error: {e}")
        
        return None
    
    def _extract_youtube_id(self, url: str) -> Optional[str]:
        """Extract video ID from YouTube URL."""
        patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
            r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None
    
    async def _get_lastfm_mbid(
        self,
        title: str,
        artist: str
    ) -> Optional[str]:
        """Get Last.fm MusicBrainz ID for a track."""
        if not self._lastfm_key:
            return None
        
        try:
            params = {
                "method": "track.getInfo",
                "api_key": self._lastfm_key,
                "artist": artist,
                "track": title,
                "format": "json"
            }
            
            async with self._session.get(self.LASTFM_API, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    track = data.get("track", {})
                    return track.get("mbid")
                    
        except Exception as e:
            logger.warning(f"Last.fm API error: {e}")
        
        return None
    
    def _find_best_match(
        self,
        results: list[dict],
        target_title: str,
        target_artist: str
    ) -> Optional[dict]:
        """Find best matching result using fuzzy matching."""
        best_score = 0
        best_match = None
        
        target_title_norm = self._normalize(target_title)
        target_artist_norm = self._normalize(target_artist)
        
        for result in results:
            result_title = result.get("title", "")
            result_artist = result.get("artist", {}).get("name", "")
            
            title_score = SequenceMatcher(
                None,
                target_title_norm,
                self._normalize(result_title)
            ).ratio()
            
            artist_score = SequenceMatcher(
                None,
                target_artist_norm,
                self._normalize(result_artist)
            ).ratio()
            
            combined_score = (
                title_score * self.TITLE_WEIGHT +
                artist_score * self.ARTIST_WEIGHT
            )
            
            if combined_score > best_score:
                best_score = combined_score
                best_match = result
        
        if best_score >= self.FUZZY_THRESHOLD:
            self._stats["fuzzy_matches"] += 1
            return best_match
        
        return None
    
    def _normalize(self, text: str) -> str:
        """Normalize text for fuzzy matching."""
        if not text:
            return ""
        
        # Convert to lowercase
        text = text.lower()
        
        # Remove common suffixes/prefixes
        patterns_to_remove = [
            r'\(feat\..*?\)',
            r'\(ft\..*?\)',
            r'\(featuring.*?\)',
            r'\[.*?\]',
            r'\(.*?remix\)',
            r'\(.*?version\)',
            r'\(.*?remaster.*?\)',
            r'- remaster.*$',
            r'- single.*$',
        ]
        
        for pattern in patterns_to_remove:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)
        
        # Remove extra whitespace
        text = ' '.join(text.split())
        
        return text.strip()
    
    async def get_similar_tracks(
        self,
        title: str,
        artist: str,
        limit: int = 100
    ) -> list[SongIdentifier]:
        """
        Get similar tracks from Last.fm.
        
        Uses Last.fm's track.getSimilar API which returns up to 100
        similar tracks in a single request.
        
        Args:
            title: Track title
            artist: Artist name
            limit: Maximum tracks to return (up to 100)
            
        Returns:
            List of SongIdentifier for similar tracks
        """
        if not self._lastfm_key:
            logger.warning("Last.fm API key not available for similarity lookup")
            return []
        
        try:
            params = {
                "method": "track.getSimilar",
                "api_key": self._lastfm_key,
                "artist": artist,
                "track": title,
                "limit": min(limit, 100),
                "format": "json"
            }
            
            async with self._session.get(self.LASTFM_API, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    similar = data.get("similartracks", {}).get("track", [])
                    
                    results = []
                    for track in similar:
                        identifier = SongIdentifier(
                            lastfm_mbid=track.get("mbid"),
                            title=track.get("name"),
                            artist=track.get("artist", {}).get("name")
                        )
                        results.append(identifier)
                    
                    logger.debug(f"Found {len(results)} similar tracks for {artist} - {title}")
                    return results
                    
        except Exception as e:
            logger.warning(f"Last.fm similarity lookup error: {e}")
        
        return []
    
    async def batch_resolve(
        self,
        songs: list[dict[str, Any]]
    ) -> list[Optional[SongIdentifier]]:
        """
        Resolve multiple songs concurrently.
        
        Args:
            songs: List of dicts with title, artist, and optional IDs
            
        Returns:
            List of SongIdentifier in same order as input
        """
        tasks = [
            self.resolve_song(
                title=song.get("title"),
                artist=song.get("artist"),
                deezer_id=song.get("deezer_id"),
                youtube_id=song.get("youtube_id"),
                isrc=song.get("isrc")
            )
            for song in songs
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Convert exceptions to None
        return [
            r if isinstance(r, SongIdentifier) else None
            for r in results
        ]
    
    async def get_stats(self) -> dict[str, Any]:
        """Get mapping resolution statistics."""
        return {
            **self._stats,
            "has_lastfm": self._lastfm_key is not None,
            "has_lavalink": self.lavalink is not None
        }


# Singleton instance
_mappings_manager: Optional[MappingsManager] = None


def get_mappings_manager() -> MappingsManager:
    """Get global mappings manager instance."""
    global _mappings_manager
    if _mappings_manager is None:
        _mappings_manager = MappingsManager()
    return _mappings_manager
