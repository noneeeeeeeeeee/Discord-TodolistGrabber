import asyncio
import logging
import re
import time
from typing import Any, Dict, Optional

from .cache_manager import CacheManager, MappingEntry

LOG = logging.getLogger(__name__)

# Spam detection thresholds
HASHTAG_LIMIT = 3
DURATION_TOLERANCE_PERCENT = 0.20  # 20%
DURATION_TOLERANCE_MIN_MS = 30000  # 30 seconds minimum tolerance

# High-quality channel indicators (boost priority)
GOOD_CHANNEL_HINTS = [
    "vevo", "official artist channel", "topic", "records", "music", "label",
]

# Official title markers (boost priority)
GOOD_TITLE_KEYWORDS = [
    "official audio", "official video", "official mv", "official music video",
    "official visualizer", "official lyric video", "official",
    "album version", "single version",
]

# Spam keywords that indicate non-music content or low-quality uploads
BAD_TITLE_KEYWORDS = [
    # Pitch/speed manipulations
    "nightcore", "night core", "slowed", "slowed + reverb", "slowed+reverb",
    "sped up", "speed up", "speedup", "pitch",
    
    # Audio effects
    "8d", "8d audio", "3d audio", "binaural", "spatial audio", "ambisonic",
    "sound spatial", "stereo widened",
    
    # Fan uploads / non-canonical
    "cover", "karaoke", "karaoke version", "karaoke instrumental",
    "instrumental", "backing track", "minus one", "tutorial", "lesson", "practice",
    
    # Remix / edits / unofficial versions
    "remix", "remixed", "edit", "rework", "bootleg", "mashup", "reimagined",
    "redux", "acapella", "a cappella", "midi", "black midi", "audio spectrum",
    "remake", "music project", "fan edit", "fanmix", "fan mix", "covered by",
    "extended version", "extended",
    
    # Performance / live (usually not studio quality)
    "live", "live at", "live from", "session", "concert", "performance", "tour",
    
    # Long-play / loop / compilation
    "hour", "hours", "loop", "mix", "mixes", "dj set", "set",
    
    # Low-quality / user-added modifiers
    "lyric", "lyrics", "lyric video", "visualizer", "reupload", "fanmade",
    
    # Non-English karaoke markers
    "伴唱", "カラオケ", "노래방",
    
    # Teaser / promo content
    "teaser", "trailer", "preview", "snippet", "sample", "promo", "promotional",
    "promotion", "promos",
    
    # Music mix / compilations
    "music mix", "top hits", "best of", "amazing", "greatest", "hits",
    "collection", "compilation", "playlist",
    
    # Subscriber specials & spam entries
    "subscriber special", "subscriber special mix", "subscriber special edition",
    "plz", "hz",
    
    # Tutorial / educational content
    "how to", "tutorial", "lesson", "practice", "learn", "teach",
    
    # Reactions / commentary
    "reaction", "review", "critique", "commentary", "analysis",
    
    # Gaming / unrelated content
    "unboxing", "gameplay", "walkthrough",
    
    # Announcement / milestone content
    "milestone", "announcement",
    
    # Year tags (often indicate compilation)
    "2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025",
    
    # Studio remixes
    "studio vocals",
    
    # Sound test / unofficial versions
    "sound test", "(sound test)",
]


class TrackResolver:
    """Resolves tracks via Pomice and maintains mapping cache."""

    def __init__(self, cache_manager: CacheManager) -> None:
        self._cache = cache_manager

    async def resolve_track(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
        prefer_cache: bool = True,
    ) -> Optional[Any]:
        artist = artist.strip()
        title = title.strip()
        if not artist or not title:
            return None

        if prefer_cache:
            mapping = await self._cache.get_mapping(artist, title)
            if mapping:
                # Validate cached mapping against spam filters
                if self._is_spam_mapping(mapping, expected_duration_ms):
                    LOG.warning(
                        "🚫 [CACHE SPAM] Invalidating cached mapping for '%s - %s' (youtube_id=%s, reason=spam detected)",
                        artist,
                        title,
                        mapping.youtube_id,
                    )
                    # Delete bad mapping from cache
                    await self._cache.delete_mapping(artist, title)
                else:
                    rebuilt = await self._create_track_obj_from_mapping(mapping)
                    if rebuilt:
                        if LOG.isEnabledFor(logging.DEBUG):
                            LOG.debug(
                                "Track resolved from cache: %s",
                                {
                                    "artist": artist,
                                    "title": title,
                                    "youtube_id": mapping.youtube_id,
                                    "channel": mapping.channel_name,
                                    "verified": mapping.verified,
                                    "source": "cache",
                                },
                            )
                        return rebuilt

        track_obj = await self._search_with_pomice(
            artist,
            title,
            expected_duration_ms=expected_duration_ms,
        )
        if not track_obj:
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "Track resolution failed: %s",
                    {
                        "artist": artist,
                        "title": title,
                        "expected_duration_ms": expected_duration_ms,
                    },
                )
            return None

        metadata = self._extract_track_metadata(track_obj)
        youtube_id_val = metadata["youtube_id"]
        url_val = metadata["url"]
        if youtube_id_val and url_val:
            duration_val = metadata.get("duration_ms")
            if expected_duration_ms and duration_val:
                duration_val = int(duration_val)

            entry = MappingEntry(
                youtube_id=str(youtube_id_val),
                url=str(url_val),
                timestamp=time.time(),
                channel_name=metadata.get("channel_name"),
                verified=bool(metadata.get("verified", False)),
                duration_ms=duration_val,
                track_identifier=metadata.get("track_identifier"),
            )
            await self._cache.set_mapping(artist, title, entry)
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "Track mapping stored: %s",
                    {
                        "artist": artist,
                        "title": title,
                        "youtube_id": entry.youtube_id,
                        "duration_ms": entry.duration_ms,
                        "channel": entry.channel_name,
                        "verified": entry.verified,
                        "source": "search",
                    },
                )
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(
                "Track resolved via Pomice: %s",
                {
                    "artist": artist,
                    "title": title,
                    "youtube_id": metadata.get("youtube_id"),
                    "channel": metadata.get("channel_name"),
                    "verified": metadata.get("verified"),
                    "score_duration_ms": metadata.get("duration_ms"),
                },
            )
        return track_obj

    async def validate_pomice_path(self) -> bool:
        node = await self._get_node()
        return node is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _create_track_obj_from_mapping(self, mapping: MappingEntry) -> Optional[Any]:
        node = await self._get_node()
        if not node:
            return None

        identifier = mapping.track_identifier
        if identifier:
            builder = getattr(node, "build_track", None)
            if builder:
                try:
                    if asyncio.iscoroutinefunction(builder):
                        track_obj = await builder(identifier)
                    else:
                        track_obj = builder(identifier)
                    if track_obj:
                        return track_obj
                except Exception as exc:
                    LOG.debug("Pomice build_track failed for identifier %s: %s", identifier, exc)

        query = mapping.url or mapping.youtube_id
        if not query:
            return None

        try:
            tracks = await node.get_tracks(query)
        except Exception as exc:
            LOG.debug("Pomice get_tracks failed for query %s: %s", query, exc)
            tracks = []

        if not tracks and mapping.youtube_id:
            try:
                tracks = await node.get_tracks(f"https://www.youtube.com/watch?v={mapping.youtube_id}")
            except Exception:
                tracks = []

        if not tracks:
            return None

        for track_obj in tracks:
            metadata = self._extract_track_metadata(track_obj)
            if metadata["youtube_id"] == mapping.youtube_id:
                return track_obj
        return tracks[0]

    async def _search_with_pomice(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
    ) -> Optional[Any]:
        node = await self._get_node()
        if not node:
            return None

        query = f"ytsearch:{artist} {title} official audio"
        try:
            results = await node.get_tracks(query)
        except Exception as exc:
            LOG.warning("Pomice search failed for %s: %s", query, exc)
            return None

        if not results:
            LOG.debug("No Pomice results for %s", query)
            return None

        best_track: Optional[Any] = None
        best_score = float("-inf")
        duration_target = expected_duration_ms or 0
        normalized_artist = artist.casefold()
        normalized_title = title.casefold()
        
        # Duration tolerance: ±20% or ±30 seconds (whichever is larger)
        duration_tolerance_ms = max(duration_target * 0.20, 30000) if duration_target else 0

        for index, candidate in enumerate(results):
            metadata = self._extract_track_metadata(candidate)
            
            # SPAM FILTERING
            candidate_title_raw = getattr(candidate, "title", None) or metadata.get("title") or ""
            
            # Filter 1: Comprehensive spam title check (hashtags + keywords)
            if self._is_spam_title(candidate_title_raw):
                continue
            
            # Filter 2: Very short videos (likely shorts, not music)
            duration_val = metadata.get("duration_ms") or 0
            if duration_val and duration_val < 45000:  # < 45 seconds
                LOG.debug(
                    "🚫 [SHORT VIDEO] Rejected '%s' - too short (duration=%dms, likely YouTube Short)",
                    candidate_title_raw[:60],
                    duration_val,
                )
                continue
            
            # Filter 3: Duration tolerance check (reject if outside tolerance)
            if duration_target and duration_val and duration_tolerance_ms:
                delta = abs(duration_target - int(duration_val))
                if delta > duration_tolerance_ms:
                    LOG.debug(
                        "🚫 [DURATION FILTER] Rejected '%s' - duration mismatch (expected=%dms, got=%dms, delta=%dms, tolerance=%dms)",
                        candidate_title_raw[:60],
                        duration_target,
                        duration_val,
                        delta,
                        int(duration_tolerance_ms)
                    )
                    continue
            
            # SCORING
            score = 0.0

            # Verified badge = huge boost
            if metadata["verified"]:
                score += 4.0

            channel = (metadata.get("channel_name") or "").casefold()
            
            # Channel name matches artist = official/authentic
            if channel and normalized_artist in channel:
                score += 2.0
            
            # Channel has official markers (VEVO, Topic, Records, etc.)
            for hint in GOOD_CHANNEL_HINTS:
                if hint in channel:
                    score += 1.5
                    break
            
            # Title has official markers
            candidate_title = candidate_title_raw.casefold()
            for good_keyword in GOOD_TITLE_KEYWORDS:
                if good_keyword in candidate_title:
                    score += 0.8
                    break

            # Title matches = relevance boost
            if candidate_title and normalized_title and normalized_title in candidate_title:
                score += 1.0

            # Duration similarity bonus (within tolerance)
            if duration_target and duration_val:
                delta = abs(duration_target - int(duration_val))
                score -= min(delta / 1000.0, 10.0)

            # Search rank penalty (lower index = higher rank)
            score -= index * 0.15

            if score > best_score:
                best_score = score
                best_track = candidate

        if not best_track and results:
            # If all filtered out, fall back to first result but log warning
            LOG.warning("⚠️ All candidates filtered out for '%s - %s', using first result", artist, title)
            return results[0]
        
        return best_track

    async def _get_node(self) -> Optional[Any]:
        try:
            import pomice  # type: ignore[import-error]
        except Exception as exc:  # pragma: no cover - dependency optional
            LOG.error("Pomice not available: %s", exc)
            return None

        node_pool = getattr(pomice, "NodePool", None)
        if node_pool is None:
            LOG.error("Pomice NodePool not available")
            return None

        try:
            node = node_pool.get_node()
        except Exception as exc:
            LOG.error("Failed to get Pomice node: %s", exc)
            return None

        if not node:
            LOG.error("Pomice NodePool returned no node")
            return None
        return node

    @staticmethod
    def _extract_track_metadata(track_obj: Any) -> Dict[str, Optional[Any]]:
        info = getattr(track_obj, "info", {}) or {}
        youtube_id = info.get("identifier") or getattr(track_obj, "identifier", None)
        url = info.get("uri") or getattr(track_obj, "uri", None)
        channel = info.get("author") or getattr(track_obj, "author", None)
        duration_ms = info.get("length") or getattr(track_obj, "length", None)
        title = info.get("title") or getattr(track_obj, "title", None)
        verified = bool(info.get("isVerified") or info.get("isOfficial"))
        track_identifier = getattr(track_obj, "track_id", None) or getattr(track_obj, "track", None)
        if youtube_id and not url:
            url = f"https://www.youtube.com/watch?v={youtube_id}"

        try:
            duration_int = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            duration_int = None

        if track_identifier is not None:
            track_identifier = str(track_identifier)

        return {
            "youtube_id": youtube_id,
            "url": url,
            "channel_name": channel,
            "verified": verified,
            "duration_ms": duration_int,
            "track_identifier": track_identifier,
            "title": title,
        }

    def _is_spam_mapping(self, mapping: MappingEntry, expected_duration_ms: Optional[int]) -> bool:
        """Check if a cached mapping is spam using heuristics."""
        
        # Check 1: Duration mismatch (if we have expected duration)
        if expected_duration_ms and mapping.duration_ms:
            tolerance_ms = max(expected_duration_ms * DURATION_TOLERANCE_PERCENT, DURATION_TOLERANCE_MIN_MS)
            delta = abs(expected_duration_ms - mapping.duration_ms)
            if delta > tolerance_ms:
                LOG.debug(
                    "🚫 [SPAM] Duration mismatch: expected=%dms, got=%dms, delta=%dms, tolerance=%dms",
                    expected_duration_ms,
                    mapping.duration_ms,
                    delta,
                    int(tolerance_ms),
                )
                return True
        
        # Check 2: Very short videos (likely shorts/clips, not music)
        if mapping.duration_ms and mapping.duration_ms < 45000:  # < 45 seconds
            LOG.debug(
                "🚫 [SPAM] Video too short: duration=%dms (likely YouTube Short)",
                mapping.duration_ms,
            )
            return True
        
        # Check 3: Check URL/ID for spam patterns (if available from cached data)
        # Note: MappingEntry doesn't store title, so we can't check hashtags here
        # The hashtag check happens during live search in _search_with_pomice
        
        return False

    def _is_spam_title(self, title: str) -> bool:
        """Check if a YouTube title indicates spam/non-music content."""
        
        if not title:
            return False
        
        title_lower = title.lower()
        
        # Check 1: Excessive hashtags
        hashtag_count = title.count('#')
        if hashtag_count > HASHTAG_LIMIT:
            LOG.debug(
                "🚫 [SPAM] Excessive hashtags: count=%d in '%s'",
                hashtag_count,
                title[:60],
            )
            return True
        
        # Check 2: Bad keywords
        for keyword in BAD_TITLE_KEYWORDS:
            if keyword in title_lower:
                LOG.debug(
                    "🚫 [SPAM] Spam keyword '%s' found in '%s'",
                    keyword,
                    title[:60],
                )
                return True
        
        return False


__all__ = ["TrackResolver"]
