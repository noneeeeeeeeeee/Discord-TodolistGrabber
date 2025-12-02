"""
Last.fm API Client for V3 Autoplay Engine.

Provides access to Last.fm's music discovery API for:
- chart.getTopTracks: Global trending tracks (first run + new releases)
- track.getSimilar: Similar tracks for exploration
- tag.getTopTracks: Genre-based exploration
- artist.getSimilar: Artist-based exploration
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

import aiohttp

LOG = logging.getLogger(__name__)

# Last.fm API configuration
LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")

# Rate limiting: Last.fm allows 5 requests/second
LASTFM_RATE_LIMIT_DELAY = 0.25  # 250ms between requests


@dataclass
class LastFMTrack:
    """Represents a track from Last.fm API."""
    artist: str
    title: str
    playcount: Optional[int] = None
    listeners: Optional[int] = None
    mbid: Optional[str] = None  # MusicBrainz ID
    url: Optional[str] = None
    match: Optional[float] = None  # Similarity score (0.0-1.0) for getSimilar


class LastFMClient:
    """
    Async client for Last.fm Music Discovery API.
    
    Usage:
        async with LastFMClient() as client:
            tracks = await client.get_top_tracks(limit=200)
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        max_concurrent: int = 3,
        timeout: float = 10.0,
    ):
        self._api_key = api_key or LASTFM_API_KEY
        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._last_request_time = 0.0
    
    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout)
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()
            self._session = None
    
    async def _rate_limit(self):
        """Enforce rate limiting between requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < LASTFM_RATE_LIMIT_DELAY:
            await asyncio.sleep(LASTFM_RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.time()
    
    async def _request(self, method: str, **params) -> Optional[Dict[str, Any]]:
        """Make an API request to Last.fm."""
        if not self._session:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        
        if not self._api_key:
            LOG.warning("⚠️ [Last.fm] No API key configured (LASTFM_API_KEY)")
            return None
        
        async with self._semaphore:
            await self._rate_limit()
            
            request_params = {
                "method": method,
                "api_key": self._api_key,
                "format": "json",
                **params,
            }
            
            try:
                async with self._session.get(LASTFM_API_BASE, params=request_params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Check for API errors
                        if "error" in data:
                            LOG.warning(
                                "⚠️ [Last.fm] API error %s: %s",
                                data.get("error"),
                                data.get("message", "Unknown error"),
                            )
                            return None
                        return data
                    else:
                        LOG.warning("⚠️ [Last.fm] HTTP %d for %s", resp.status, method)
                        return None
            except asyncio.TimeoutError:
                LOG.warning("⚠️ [Last.fm] Timeout for %s", method)
                return None
            except Exception as e:
                LOG.warning("⚠️ [Last.fm] Request failed for %s: %s", method, str(e)[:100])
                return None
    
    def _parse_track(self, track_data: Dict[str, Any]) -> Optional[LastFMTrack]:
        """Parse a track from Last.fm response."""
        try:
            artist_data = track_data.get("artist", {})
            if isinstance(artist_data, dict):
                artist = artist_data.get("name", "")
            else:
                artist = str(artist_data)
            
            title = track_data.get("name", "")
            
            if not artist or not title:
                return None
            
            return LastFMTrack(
                artist=artist.strip(),
                title=title.strip(),
                playcount=int(track_data.get("playcount", 0)) if track_data.get("playcount") else None,
                listeners=int(track_data.get("listeners", 0)) if track_data.get("listeners") else None,
                mbid=track_data.get("mbid") or None,
                url=track_data.get("url"),
                match=float(track_data.get("match", 0)) if track_data.get("match") else None,
            )
        except Exception as e:
            LOG.debug("Failed to parse track: %s", str(e)[:50])
            return None
    
    # =========================================================================
    # chart.getTopTracks - Global trending tracks
    # =========================================================================
    
    async def get_top_tracks(self, limit: int = 200, page: int = 1) -> List[LastFMTrack]:
        """
        Get globally trending tracks from Last.fm charts.
        
        Used for:
        - First run bootstrap (Scenario A): Fetch 200 popular tracks
        - Fallback when exploration yields no results
        
        Args:
            limit: Number of tracks to fetch (max 1000 across pages)
            page: Page number (1-indexed)
        
        Returns:
            List of LastFMTrack objects
        """
        tracks: List[LastFMTrack] = []
        
        # Last.fm returns max 50 per page, so we need multiple requests
        per_page = min(50, limit)
        pages_needed = (limit + per_page - 1) // per_page
        
        for p in range(pages_needed):
            current_page = page + p
            remaining = limit - len(tracks)
            current_limit = min(per_page, remaining)
            
            if remaining <= 0:
                break
            
            data = await self._request(
                "chart.gettoptracks",
                page=current_page,
                limit=current_limit,
            )
            
            if not data:
                break
            
            tracks_data = data.get("tracks", {}).get("track", [])
            if not tracks_data:
                break
            
            for track_data in tracks_data:
                track = self._parse_track(track_data)
                if track:
                    tracks.append(track)
            
            LOG.debug(
                "📊 [Last.fm] chart.getTopTracks page %d: %d tracks",
                current_page,
                len(tracks_data),
            )
        
        LOG.info("📊 [Last.fm] Fetched %d top tracks", len(tracks))
        return tracks
    
    # =========================================================================
    # track.getSimilar - Similar tracks for exploration
    # =========================================================================
    
    async def get_similar_tracks(
        self,
        artist: str,
        title: str,
        limit: int = 50,
    ) -> List[LastFMTrack]:
        """
        Get tracks similar to a given track.
        
        Used for:
        - Daydreaming (Scenario B): Explore tracks similar to cached tracks
        - Building warm pool in recommender
        
        Args:
            artist: Artist name
            title: Track title
            limit: Number of similar tracks to fetch
        
        Returns:
            List of LastFMTrack objects with match scores
        """
        data = await self._request(
            "track.getsimilar",
            artist=artist,
            track=title,
            limit=limit,
            autocorrect=1,  # Auto-correct misspellings
        )
        
        if not data:
            return []
        
        tracks_data = data.get("similartracks", {}).get("track", [])
        if not tracks_data:
            return []
        
        tracks = []
        for track_data in tracks_data:
            track = self._parse_track(track_data)
            if track:
                tracks.append(track)
        
        LOG.debug(
            "📊 [Last.fm] track.getSimilar for '%s - %s': %d tracks",
            artist,
            title,
            len(tracks),
        )
        return tracks
    
    # =========================================================================
    # tag.getTopTracks - Genre-based exploration
    # =========================================================================
    
    async def get_tag_top_tracks(
        self,
        tag: str,
        limit: int = 50,
        page: int = 1,
    ) -> List[LastFMTrack]:
        """
        Get top tracks for a specific tag/genre.
        
        Used for:
        - Daydreaming (Scenario B): Genre-weighted exploration
        - Building cold pool in recommender
        
        Args:
            tag: Genre/tag name (e.g., "pop", "electronic", "rock")
            limit: Number of tracks to fetch
            page: Page number
        
        Returns:
            List of LastFMTrack objects
        """
        data = await self._request(
            "tag.gettoptracks",
            tag=tag,
            page=page,
            limit=limit,
        )
        
        if not data:
            return []
        
        tracks_data = data.get("tracks", {}).get("track", [])
        if not tracks_data:
            return []
        
        tracks = []
        for track_data in tracks_data:
            track = self._parse_track(track_data)
            if track:
                tracks.append(track)
        
        LOG.debug(
            "📊 [Last.fm] tag.getTopTracks for '%s': %d tracks",
            tag,
            len(tracks),
        )
        return tracks
    
    # =========================================================================
    # artist.getSimilar - Artist-based exploration
    # =========================================================================
    
    async def get_similar_artists(
        self,
        artist: str,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Get artists similar to a given artist.
        
        Used for:
        - Building collaboration clusters
        - Expanding artist pool in recommender
        
        Args:
            artist: Artist name
            limit: Number of similar artists to fetch
        
        Returns:
            List of dicts with artist info (name, match, mbid)
        """
        data = await self._request(
            "artist.getsimilar",
            artist=artist,
            limit=limit,
            autocorrect=1,
        )
        
        if not data:
            return []
        
        artists_data = data.get("similarartists", {}).get("artist", [])
        if not artists_data:
            return []
        
        artists = []
        for artist_data in artists_data:
            name = artist_data.get("name", "").strip()
            if name:
                artists.append({
                    "name": name,
                    "match": float(artist_data.get("match", 0)),
                    "mbid": artist_data.get("mbid"),
                    "url": artist_data.get("url"),
                })
        
        LOG.debug(
            "📊 [Last.fm] artist.getSimilar for '%s': %d artists",
            artist,
            len(artists),
        )
        return artists
    
    # =========================================================================
    # artist.getTopTracks - Top tracks by artist
    # =========================================================================
    
    async def get_artist_top_tracks(
        self,
        artist: str,
        limit: int = 20,
    ) -> List[LastFMTrack]:
        """
        Get top tracks by a specific artist.
        
        Used for:
        - Expanding pool when user likes an artist
        - Building anchor artist catalog
        
        Args:
            artist: Artist name
            limit: Number of tracks to fetch
        
        Returns:
            List of LastFMTrack objects
        """
        data = await self._request(
            "artist.gettoptracks",
            artist=artist,
            limit=limit,
            autocorrect=1,
        )
        
        if not data:
            return []
        
        tracks_data = data.get("toptracks", {}).get("track", [])
        if not tracks_data:
            return []
        
        tracks = []
        for track_data in tracks_data:
            track = self._parse_track(track_data)
            if track:
                tracks.append(track)
        
        LOG.debug(
            "📊 [Last.fm] artist.getTopTracks for '%s': %d tracks",
            artist,
            len(tracks),
        )
        return tracks


# Convenience function for quick usage
async def fetch_lastfm_top_tracks(limit: int = 200) -> List[LastFMTrack]:
    """Fetch top tracks from Last.fm (convenience function)."""
    async with LastFMClient() as client:
        return await client.get_top_tracks(limit=limit)
