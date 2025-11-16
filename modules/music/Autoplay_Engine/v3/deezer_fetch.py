"""
Deezer API client for canonical music metadata retrieval.

Provides:
- Track search with intelligent fuzzy matching
- Rate limiting and circuit breaker pattern
- Exponential backoff for error handling
- Multi-factor confidence scoring
"""

import asyncio
import logging
import re
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import aiohttp

LOG = logging.getLogger(__name__)

# Deezer API Configuration
DEEZER_API_BASE = "https://api.deezer.com"
DEEZER_SEARCH_URL = f"{DEEZER_API_BASE}/search"
DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_CONCURRENT_REQUESTS = 50
CIRCUIT_BREAKER_THRESHOLD = 5  # Failures before circuit opens
CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minutes


@dataclass
class DeezerTrack:
    """Deezer track search result."""
    id: str
    artist: str
    title: str
    album: str
    duration_ms: int
    preview_url: Optional[str] = None


@dataclass
class MatchResult:
    """Track match result with confidence score."""
    track: DeezerTrack
    confidence: float
    artist_match: float
    title_match: float
    duration_match: float


class DeezerClient:
    """
    Async client for Deezer API with rate limiting and resilience.
    
    Features:
    - asyncio.Semaphore(50) for rate limiting
    - Exponential backoff on 429/5xx errors (2s → 4s → 8s → 16s → 32s)
    - Circuit breaker (5 consecutive failures → skip for 5 minutes)
    - 3-second timeout per request
    - Multi-factor confidence scoring (title 50%, artist 30%, duration 20%)
    """

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_concurrent: int = MAX_CONCURRENT_REQUESTS,
    ):
        """
        Initialize Deezer client.
        
        Args:
            session: Optional aiohttp session for connection pooling
            timeout: Request timeout in seconds
            max_concurrent: Maximum concurrent requests
        """
        self.session = session
        self._owns_session = session is None
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(max_concurrent)
        
        # Circuit breaker state
        self.consecutive_failures = 0
        self.circuit_open_until = 0.0
        
        LOG.info(
            f"🎵 DeezerClient initialized: {max_concurrent} concurrent requests, "
            f"{timeout}s timeout"
        )

    async def __aenter__(self):
        """Async context manager entry."""
        if self._owns_session:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._owns_session and self.session:
            await self.session.close()

    async def search_track(
        self,
        query: str,
        limit: int = 10,
    ) -> List[DeezerTrack]:
        """
        Search Deezer for tracks matching the query.
        
        Args:
            query: Search query string
            limit: Maximum number of results to return
            
        Returns:
            List of DeezerTrack objects
        """
        # Check circuit breaker
        if time.time() < self.circuit_open_until:
            remaining = int(self.circuit_open_until - time.time())
            LOG.warning(
                f"⚠️ Deezer circuit breaker OPEN, skipping search "
                f"(retry in {remaining}s)"
            )
            return []

        # Preprocess query to improve API search success rate
        preprocessed_query = self._preprocess_search_query(query)
        if preprocessed_query != query:
            LOG.debug(f"🔧 Query preprocessing: '{query}' → '{preprocessed_query}'")

        async with self.semaphore:
            return await self._search_with_retry(preprocessed_query, limit)

    def _preprocess_search_query(self, query: str) -> str:
        """
        Preprocess search query to improve Deezer API success rate.
        
        Simplifies queries by removing:
        - Overly specific keywords that may not appear in Deezer titles
        - Common noise words (soundtrack, OST, song, music, theme)
        - Duplicate words
        - Extra whitespace
        
        Args:
            query: Raw search query
            
        Returns:
            Simplified query more likely to find matches
        """
        if not query:
            return ""
        
        # Start with lowercase for processing
        processed = query.lower()
        
        # Remove noise words that don't help Deezer searches
        noise_words = [
            r'\b(soundtrack|ost|song|music|theme|audio|video|official|original)\b',
            r'\b(opening|ending|op|ed)\b',
            r'\b(cover|remix|acoustic|live|version)\b',
            r'\b(ultimate|knockout)\b',  # Game-specific decorations
        ]
        for pattern in noise_words:
            processed = re.sub(pattern, ' ', processed, flags=re.IGNORECASE)
        
        # Remove parenthetical content (often contains noise)
        processed = re.sub(r'\([^)]*\)', ' ', processed)
        processed = re.sub(r'\[[^\]]*\]', ' ', processed)
        
        # Remove pipe separators (take first part only)
        if '|' in processed:
            processed = processed.split('|')[0]
        
        # Remove years (often cause mismatches)
        processed = re.sub(r'\b(19|20)\d{2}\b', ' ', processed)
        
        # Collapse multiple spaces
        processed = ' '.join(processed.split())
        
        # Deduplicate consecutive words
        words = processed.split()
        seen = set()
        deduped = []
        for word in words:
            if word.lower() not in seen:
                seen.add(word.lower())
                deduped.append(word)
        
        result = ' '.join(deduped).strip()
        
        # If we simplified too much (< 2 words), keep at least artist + partial title
        if result and len(result.split()) < 2:
            # Fall back to original query with minimal cleanup
            fallback = ' '.join(query.split())
            if len(fallback.split()) >= 2:
                return fallback
        
        return result if result else query

    async def _search_with_retry(
        self,
        query: str,
        limit: int,
        max_retries: int = 5,
    ) -> List[DeezerTrack]:
        """
        Search with exponential backoff on errors.
        
        Args:
            query: Search query
            limit: Result limit
            max_retries: Maximum retry attempts
            
        Returns:
            List of DeezerTrack objects
        """
        if not self.session:
            LOG.error("❌ No aiohttp session available")
            return []

        backoff_delays = [2, 4, 8, 16, 32]  # Exponential backoff
        
        for attempt in range(max_retries):
            try:
                params = {"q": query, "limit": limit}
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                
                async with self.session.get(
                    DEEZER_SEARCH_URL,
                    params=params,
                    timeout=timeout,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        tracks = self._parse_search_results(data)
                        
                        # Reset circuit breaker on success
                        self.consecutive_failures = 0
                        
                        LOG.debug(
                            f"✅ Deezer search: '{query}' returned {len(tracks)} results"
                        )
                        return tracks
                    
                    elif response.status == 429:
                        # Rate limited
                        if attempt < max_retries - 1:
                            delay = backoff_delays[attempt]
                            LOG.warning(
                                f"⚠️ Deezer rate limit (429), retrying in {delay}s "
                                f"(attempt {attempt + 1}/{max_retries})"
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            LOG.error(f"❌ Deezer rate limit exhausted after {max_retries} attempts")
                            self._handle_failure()
                            return []
                    
                    elif response.status >= 500:
                        # Server error
                        if attempt < max_retries - 1:
                            delay = backoff_delays[attempt]
                            LOG.warning(
                                f"⚠️ Deezer server error ({response.status}), retrying in {delay}s "
                                f"(attempt {attempt + 1}/{max_retries})"
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            LOG.error(
                                f"❌ Deezer server error ({response.status}) after {max_retries} attempts"
                            )
                            self._handle_failure()
                            return []
                    
                    else:
                        LOG.error(f"❌ Deezer search failed with status {response.status}")
                        self._handle_failure()
                        return []
                        
            except asyncio.TimeoutError:
                LOG.warning(f"⏱️ Deezer search timeout after {self.timeout}s: '{query}'")
                self._handle_failure()
                return []
                
            except Exception as e:
                LOG.error(f"❌ Deezer search error: {e}")
                self._handle_failure()
                return []
        
        return []

    def _parse_search_results(self, data: Dict[str, Any]) -> List[DeezerTrack]:
        """Parse Deezer API response into DeezerTrack objects."""
        tracks = []
        
        for item in data.get("data", []):
            try:
                track = DeezerTrack(
                    id=str(item.get("id", "")),
                    artist=item.get("artist", {}).get("name", "Unknown Artist"),
                    title=item.get("title", "Unknown Title"),
                    album=item.get("album", {}).get("title", ""),
                    duration_ms=int(item.get("duration", 0)) * 1000,  # Deezer uses seconds
                    preview_url=item.get("preview"),
                )
                tracks.append(track)
            except (KeyError, ValueError) as e:
                LOG.warning(f"⚠️ Failed to parse Deezer track: {e}")
                continue
        
        return tracks

    def _handle_failure(self) -> None:
        """Handle API failure for circuit breaker."""
        self.consecutive_failures += 1
        
        if self.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            self.circuit_open_until = time.time() + CIRCUIT_BREAKER_COOLDOWN
            LOG.error(
                f"🚨 Deezer circuit breaker OPENED after {self.consecutive_failures} failures, "
                f"cooldown for {CIRCUIT_BREAKER_COOLDOWN}s"
            )
            self.consecutive_failures = 0  # Reset counter

    def get_best_match(
        self,
        results: List[DeezerTrack],
        threshold: float = 0.75,
        expected_artist: Optional[str] = None,
        expected_title: Optional[str] = None,
        expected_duration_ms: Optional[int] = None,
        relax_margin: float = 0.05,
    ) -> Optional[MatchResult]:
        """
        Find best matching track from search results using multi-factor scoring.
        
        Scoring weights:
        - Title similarity: 50%
        - Artist similarity: 30%
        - Duration matching: 20%
        
        Args:
            results: List of DeezerTrack search results
            threshold: Minimum confidence score (0.0-1.0)
            expected_artist: Expected artist name for comparison
            expected_title: Expected track title for comparison
            expected_duration_ms: Expected track duration for scoring
            
        Returns:
            MatchResult if confidence >= threshold, else None
        """
        if not results:
            return None

        best_match: Optional[MatchResult] = None
        best_score = 0.0

        for track in results:
            confidence, components = self._calculate_match_confidence(
                track,
                expected_artist=expected_artist,
                expected_title=expected_title,
                expected_duration_ms=expected_duration_ms,
            )

            if confidence > best_score:
                best_score = confidence
                best_match = MatchResult(
                    track=track,
                    confidence=confidence,
                    artist_match=components["artist"],
                    title_match=components["title"],
                    duration_match=components["duration"],
                )

        if best_match and best_match.confidence >= threshold:
            LOG.info(
                f"✅ Deezer match: '{best_match.track.artist} - {best_match.track.title}' "
                f"(confidence={best_match.confidence:.2f}, "
                f"title={best_match.title_match:.2f}, "
                f"artist={best_match.artist_match:.2f}, "
                f"duration={best_match.duration_match:.2f})"
            )
            return best_match
        if best_match and best_score >= threshold - relax_margin:
            LOG.warning(
                f"⚠️ Deezer match fell just below threshold ({best_score:.2f} < {threshold:.2f})"
                f" but within relax margin ({relax_margin:.2f}); trusting best candidate."
            )
            return best_match

        LOG.debug(
            f"⚠️ No match above threshold {threshold:.2f} "
            f"(best={best_score:.2f})"
        )
        return None

    def _calculate_match_confidence(
        self,
        track: DeezerTrack,
        expected_artist: Optional[str] = None,
        expected_title: Optional[str] = None,
        expected_duration_ms: Optional[int] = None,
    ) -> tuple[float, Dict[str, float]]:
        """
        Calculate multi-factor confidence score.
        
        Weights:
        - Title: 50%
        - Artist: 30%
        - Duration: 20%
        
        Returns:
            Tuple of (overall_confidence, component_scores_dict)
        """
        # Title similarity (50% weight)
        if expected_title:
            title_score = self._string_similarity(
                self.normalize_music_title(track.title),
                self.normalize_music_title(expected_title),
            )
        else:
            # No expected title to compare - assume perfect match
            title_score = 1.0

        # Artist similarity (30% weight)
        if expected_artist:
            artist_score = self._string_similarity(
                self.normalize_music_title(track.artist),
                self.normalize_music_title(expected_artist),
            )
        else:
            # No expected artist to compare - assume perfect match
            artist_score = 1.0

        # Duration matching (20% weight)
        duration_score = 1.0
        if expected_duration_ms and track.duration_ms:
            duration_diff_ratio = abs(track.duration_ms - expected_duration_ms) / expected_duration_ms
            # Tolerance: within 30% is acceptable
            if duration_diff_ratio <= 0.10:
                duration_score = 1.0
            elif duration_diff_ratio <= 0.30:
                duration_score = 0.7
            else:
                duration_score = 0.3

        # Weighted overall score
        confidence = (
            title_score * 0.5 +
            artist_score * 0.3 +
            duration_score * 0.2
        )

        components = {
            "title": title_score,
            "artist": artist_score,
            "duration": duration_score,
        }

        return confidence, components

    @staticmethod
    def _string_similarity(a: str, b: str) -> float:
        """Calculate string similarity using SequenceMatcher (0.0-1.0)."""
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    @staticmethod
    def normalize_music_title(text: str) -> str:
        """
        Normalize music title for matching.
        
        Removes:
        - "official", "official audio", "lyric video", etc.
        - "ft.", "feat.", featuring artists
        - Season markers (S1, S2, etc.)
        - Pipe separators and content after them
        - Extra whitespace
        
        Args:
            text: Raw title text
            
        Returns:
            Normalized title
        """
        if not text:
            return ""

        # Convert to lowercase for case-insensitive matching
        normalized = text.lower()

        # Remove YouTube suffixes
        suffixes = [
            r"\(official audio\)",
            r"\(official video\)",
            r"\(lyric video\)",
            r"\(lyrics\)",
            r"\(official\)",
            r"\(nightcore\)",
            r"\(slowed\)",
            r"\(reverb\)",
            r"\(sped up\)",
            r"official audio",
            r"official video",
            r"lyric video",
            r"lyrics",
            r"nightcore",
            r"slowed",
            r"reverb",
        ]
        for suffix in suffixes:
            normalized = re.sub(suffix, "", normalized, flags=re.IGNORECASE)

        # Remove featuring artists
        normalized = re.sub(r"\s+ft\.?\s+.*$", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+feat\.?\s+.*$", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+featuring\s+.*$", "", normalized, flags=re.IGNORECASE)

        # Remove season markers
        normalized = re.sub(r"\s+s\d+\s*", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+season\s+\d+\s*", " ", normalized, flags=re.IGNORECASE)

        # Remove pipe separators and content after them
        if "|" in normalized:
            normalized = normalized.split("|")[0]

        # Remove extra whitespace
        normalized = " ".join(normalized.split())

        return normalized.strip()
    
    async def get_track_details(self, track_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch detailed track information including BPM and gain.
        
        This provides acoustic features that can be used as fallback
        when V3 audio analysis (Librosa+MobileNet) is unavailable.
        
        Args:
            track_id: Deezer track ID
            
        Returns:
            Dict with BPM and gain/loudness or None on failure
        """
        if not self.session:
            LOG.error("❌ No aiohttp session available for track details")
            return None
        
        url = f"{DEEZER_API_BASE}/track/{track_id}"
        
        async with self.semaphore:
            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                
                async with self.session.get(url, timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Extract acoustic features
                        bpm = float(data.get("bpm", 0) or 0)
                        gain = float(data.get("gain", 0) or 0)
                        
                        LOG.debug(
                            f"✅ Deezer track details for {track_id}: "
                            f"BPM={bpm}, gain={gain}dB"
                        )
                        
                        return {
                            "bpm": bpm,
                            "gain": gain,
                            "duration_ms": int(data.get("duration", 0) * 1000),
                        }
                    else:
                        LOG.debug(f"⚠️ Deezer track details failed: HTTP {response.status}")
                        return None
                        
            except asyncio.TimeoutError:
                LOG.debug(f"⏱️ Deezer track details timeout for {track_id}")
                return None
            except Exception as exc:
                LOG.debug(f"❌ Deezer track details error: {exc}")
                return None

