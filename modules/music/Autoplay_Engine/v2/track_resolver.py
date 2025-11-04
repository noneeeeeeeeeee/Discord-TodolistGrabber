import asyncio
import logging
import math
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from .cache_manager import CacheManager, MappingEntry

LOG = logging.getLogger(__name__)

# Spam detection thresholds
HASHTAG_LIMIT = 3
DURATION_TOLERANCE_PERCENT = 0.20  # 20%
DURATION_TOLERANCE_MIN_MS = 30000  # 30 seconds minimum tolerance
SHORT_CLIP_THRESHOLD_MS = 30000  # clips shorter than 30s are likely YouTube Shorts/spam

# Confidence threshold for hybrid resolution
LOW_CONFIDENCE_THRESHOLD = 7.0

# High-quality channel indicators (boost priority)
GOOD_CHANNEL_HINTS = [
    "vevo",
    "official artist channel",
    "topic",
    "records",
    "music",
    "label",
]

# Official title markers (boost priority)
GOOD_TITLE_KEYWORDS = [
    "official audio",
    "official video",
    "official mv",
    "official music video",
    "official visualizer",
    "official lyric video",
    "official",
    "album version",
    "single version",
]

# Spam keywords that indicate non-music content or low-quality uploads
# Note: These are checked UNLESS the video is from a verified/official channel
BAD_TITLE_KEYWORDS = [
    # Pitch/speed manipulations
    "nightcore",
    "night core",
    "slowed",
    "slowed + reverb",
    "slowed+reverb",
    "sped up",
    "speed up",
    "speedup",
    "pitch shifted",
    # Audio effects
    "8d",
    "8d audio",
    "3d audio",
    "binaural",
    "spatial audio",
    "ambisonic",
    "sound spatial",
    "stereo widened",
    # Fan uploads / non-canonical (but allow instrumental if official)
    "cover",
    "karaoke",
    "karaoke version",
    "karaoke instrumental",
    "backing track",
    "minus one",
    "tutorial",
    "lesson",
    "practice",
    # Remix / edits / unofficial versions (but be lenient with official remixes)
    "bootleg",
    "reimagined",
    "redux",
    "acapella",
    "a cappella",
    "midi",
    "black midi",
    "audio spectrum",
    "remake",
    "music project",
    "fan edit",
    "fanmix",
    "fan mix",
    "covered by",
    # Performance / live (usually not studio quality)
    "live",
    "live at",
    "live from",
    "session",
    "concert",
    "performance",
    "tour",
    # Long-play / loop / compilation
    "hour",
    "hours",
    "loop",
    "dj set",
    # Low-quality / user-added modifiers (but allow "lyric video" if official)
    "reupload",
    "fanmade",
    "fan made",
    # Non-English karaoke markers
    "伴唱",
    "カラオケ",
    "노래방",
    # Teaser / promo content
    "teaser",
    "trailer",
    "preview",
    "snippet",
    "sample",
    "promo",
    "promotional",
    "promotion",
    "promos",
    # Music mix / compilations
    "music mix",
    "top hits",
    "best of",
    "amazing",
    "greatest",
    "hits",
    "collection",
    "compilation",
    "playlist",
    # Subscriber specials & spam entries
    "subscriber special",
    "subscriber special mix",
    "subscriber special edition",
    "plz",
    "hz",
    # Tutorial / educational content
    "how to",
    "tutorial",
    "lesson",
    "practice",
    "learn",
    "teach",
    # Reactions / commentary
    "reaction",
    "review",
    "critique",
    "commentary",
    "analysis",
    # Gaming / unrelated content
    "unboxing",
    "gameplay",
    "walkthrough",
    # Announcement / milestone content
    "milestone",
    "announcement",
    # Sound test / unofficial versions
    "sound test",
    "(sound test)",
]

# Keywords that are OK when from official/verified channels
ALLOWED_IF_OFFICIAL = [
    "instrumental",
    "extended",
    "extended version",
    "remix",
    "remixed",
    "lyric",
    "lyrics",
    "lyric video",
    "visualizer",
    "official visualizer",
    "edit",
    "rework",
    "mashup",
    "mix",
    "mixes",
    "set",
]

# Keywords associated with episodic or non-music content. Values represent penalty weights.
CONTENT_PENALTY_KEYWORDS: Dict[str, float] = {
    "episode": 1.5,
    "season": 1.1,
    "s0": 0.9,
    "s1e": 0.9,
    "s2e": 0.9,
    "s3e": 0.9,
    "s4e": 0.9,
    "chapter": 1.0,
    "part ": 0.7,
    "pt.": 0.7,
    "comic dub": 1.6,
    "animation meme": 1.4,
    "audio drama": 1.6,
    "roleplay": 1.1,
    "story": 0.6,
    "fanfic": 1.1,
    "fan fiction": 1.1,
    "full episode": 1.8,
    "full movie": 1.8,
    "short film": 1.2,
    "pilot": 1.2,
}

POSITIVE_MUSIC_HINTS = [
    "song",
    "music",
    "official",
    "audio",
    "lyrics",
    "lyric",
    "ost",
]

# Critical spam keywords that nearly always indicate non-music content
CRITICAL_SPAM_KEYWORDS = {
    "reaction",
    "review",
    "commentary",
    "analysis",
    "gameplay",
    "walkthrough",
    "tutorial",
    "how to",
    "lesson",
    "practice",
    "teach",
    "unboxing",
    "milestone",
    "announcement",
}


class TrackResolver:
    """Resolves tracks via Pomice and maintains mapping cache."""

    def __init__(self, cache_manager: CacheManager, gemini_service: Any = None) -> None:
        self._cache = cache_manager
        self._gemini_service = gemini_service
        self._banned_tracks: Dict[Tuple[str, str], List[Tuple[str, str, str]]] = {}

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
                                    "heuristic_score": round(
                                        float(mapping.heuristic_score or 0.0), 3
                                    ),
                                    "title_similarity": mapping.title_similarity,
                                    "artist_similarity": mapping.artist_similarity,
                                    "channel_similarity": mapping.channel_similarity,
                                    "engagement_score": mapping.engagement_score,
                                    "duration_score": mapping.duration_score,
                                    "content_penalty": mapping.content_penalty,
                                    "spam_penalty": mapping.spam_penalty,
                                    "spam_flags": mapping.spam_flags,
                                    "heuristic_version": mapping.heuristic_version,
                                },
                            )
                        return rebuilt

        search_result = await self._search_with_pomice(
            artist,
            title,
            expected_duration_ms=expected_duration_ms,
        )

        # Phase 4: Confidence Check & Branching
        if not search_result:
            # No heuristic result at all - try Gemini if available
            if self._gemini_service:
                LOG.warning(
                    "⚠️ [Hybrid] Heuristic search failed for '%s - %s', falling back to Gemini AI",
                    artist,
                    title,
                )
                return await self.resolve_track_with_gemini(
                    artist, title, expected_duration_ms=expected_duration_ms
                )

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

        track_obj, heuristics = search_result
        heuristic_score = float(heuristics.get("score", 0.0) or 0.0)

        # Check confidence threshold
        if heuristic_score < LOW_CONFIDENCE_THRESHOLD:
            # Low confidence - fallback to Gemini AI for better accuracy
            if self._gemini_service:
                LOG.info(
                    "🔄 [Hybrid] Low confidence score %.2f < %.2f for '%s - %s', using Gemini AI fallback",
                    heuristic_score,
                    LOW_CONFIDENCE_THRESHOLD,
                    artist,
                    title,
                )
                gemini_track = await self.resolve_track_with_gemini(
                    artist, title, expected_duration_ms=expected_duration_ms
                )
                if gemini_track:
                    return gemini_track
                # Gemini failed, use heuristic result as fallback
                LOG.warning(
                    "⚠️ [Hybrid] Gemini failed, using low-confidence heuristic result (score=%.2f)",
                    heuristic_score,
                )
            else:
                LOG.debug(
                    "[Hybrid] Low confidence score %.2f but Gemini not available, using heuristic result",
                    heuristic_score,
                )

        # High confidence or Gemini unavailable - use heuristic result
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
                title=str(
                    metadata.get("title") or heuristics.get("candidate_title") or ""
                )
                or None,
                heuristic_score=float(heuristics.get("score", 0.0) or 0.0),
                title_similarity=self._safe_float(heuristics.get("title_similarity")),
                artist_similarity=self._safe_float(heuristics.get("artist_similarity")),
                channel_similarity=self._safe_float(
                    heuristics.get("channel_similarity")
                ),
                engagement_score=self._safe_float(heuristics.get("engagement_score")),
                duration_score=self._safe_float(heuristics.get("duration_score")),
                content_penalty=self._safe_float(heuristics.get("content_penalty")),
                spam_penalty=self._safe_float(heuristics.get("spam_penalty")),
                spam_flags=list(heuristics.get("spam_flags", [])),
                search_rank=int(heuristics.get("search_rank", 0) or 0),
                heuristic_version=int(heuristics.get("heuristic_version", 2) or 2),
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
                        "heuristic_score": round(entry.heuristic_score, 3),
                        "title_similarity": entry.title_similarity,
                        "artist_similarity": entry.artist_similarity,
                        "channel_similarity": entry.channel_similarity,
                        "engagement_score": entry.engagement_score,
                        "duration_score": entry.duration_score,
                        "content_penalty": entry.content_penalty,
                        "spam_penalty": entry.spam_penalty,
                        "spam_flags": entry.spam_flags,
                        "heuristic_version": entry.heuristic_version,
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
                    "heuristic_score": round(
                        float(heuristics.get("score", 0.0) or 0.0), 3
                    ),
                    "title_similarity": heuristics.get("title_similarity"),
                    "artist_similarity": heuristics.get("artist_similarity"),
                    "channel_similarity": heuristics.get("channel_similarity"),
                    "engagement_score": heuristics.get("engagement_score"),
                    "duration_score": heuristics.get("duration_score"),
                    "content_penalty": heuristics.get("content_penalty"),
                    "spam_penalty": heuristics.get("spam_penalty"),
                    "spam_flags": heuristics.get("spam_flags"),
                },
            )
        return track_obj

    async def ban_track(
        self,
        artist: str,
        title: str,
        youtube_id: str,
        channel_name: str,
        video_title: str,
    ) -> None:
        """
        Ban a specific YouTube video for a given artist/title combination.
        This prevents it from being selected again in future resolutions.
        """
        key = (artist.strip().lower(), title.strip().lower())
        if key not in self._banned_tracks:
            self._banned_tracks[key] = []

        ban_entry = (youtube_id, channel_name, video_title)
        if ban_entry not in self._banned_tracks[key]:
            self._banned_tracks[key].append(ban_entry)
            LOG.info(
                "🚫 [BAN] Banned YouTube video for future resolutions: artist='%s', title='%s', youtube_id='%s', channel='%s'",
                artist,
                title,
                youtube_id,
                channel_name,
            )

    async def resolve_track_with_gemini(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
    ) -> Optional[Any]:
        """
        Resolve track using Gemini AI to intelligently select the best match.
        This is more expensive but produces better results for ambiguous cases.
        Used when user reports low quality on a track.
        """
        artist = artist.strip()
        title = title.strip()
        if not artist or not title:
            return None

        if not self._gemini_service:
            LOG.warning(
                "[Gemini Resolution] Gemini service not available, falling back to heuristic search"
            )
            return await self.resolve_track(
                artist,
                title,
                expected_duration_ms=expected_duration_ms,
                prefer_cache=False,
            )

        LOG.info(
            "🤖 [Gemini Resolution] Using AI-powered search for '%s' by '%s'",
            title,
            artist,
        )

        # Get search results from Pomice (up to 15 candidates for Gemini to analyze)
        node = await self._get_node()
        if not node:
            return None

        search_query = f"{artist} {title}"
        try:
            search_results = await node.get_tracks(
                query=f"ytsearch:{search_query}", ctx=None
            )
        except Exception as e:
            LOG.error(f"Pomice search failed: {e}")
            return None

        if not search_results:
            return None

        # Filter out banned tracks
        ban_key = (artist.lower(), title.lower())
        banned_ids = set()
        if ban_key in self._banned_tracks:
            banned_ids = {entry[0] for entry in self._banned_tracks[ban_key]}

        # Collect candidate info for Gemini
        candidates = []
        for idx, track in enumerate(search_results[:15]):  # Analyze top 15
            metadata = self._extract_track_metadata(track)
            youtube_id = metadata.get("youtube_id", "")

            # Skip banned tracks
            if youtube_id in banned_ids:
                LOG.debug(
                    "[Gemini Resolution] Skipping banned track: youtube_id=%s, channel=%s",
                    youtube_id,
                    metadata.get("channel_name"),
                )
                continue

            candidates.append(
                {
                    "index": idx,
                    "video_title": metadata.get("title", ""),
                    "channel_name": metadata.get("channel_name", ""),
                    "duration_ms": metadata.get("duration_ms", 0),
                    "verified": metadata.get("verified", False),
                    "view_count": metadata.get("view_count", 0),
                    "track_obj": track,
                }
            )

        if not candidates:
            LOG.warning(
                "[Gemini Resolution] No valid candidates after filtering banned tracks"
            )
            return None

        # Build Gemini prompt
        prompt = self._build_gemini_selection_prompt(
            artist=artist,
            title=title,
            expected_duration_ms=expected_duration_ms,
            candidates=candidates,
        )

        # Query Gemini for best match
        try:
            response = await self._gemini_service.query_gemini(prompt)
            selected_index = self._parse_gemini_selection(response, len(candidates))

            if selected_index is None:
                LOG.warning(
                    "[Gemini Resolution] Failed to parse Gemini response, falling back to heuristic"
                )
                # Fallback to first non-banned candidate
                selected_index = 0

            selected_candidate = candidates[selected_index]
            track_obj = selected_candidate["track_obj"]

            # Extract metadata and cache the result
            metadata = self._extract_track_metadata(track_obj)

            LOG.info(
                "✅ [Gemini Resolution] Selected: '%s' from '%s' (verified=%s, index=%d/%d)",
                selected_candidate["video_title"],
                selected_candidate["channel_name"],
                selected_candidate["verified"],
                selected_index + 1,
                len(candidates),
            )

            # Cache the Gemini-selected mapping (with high confidence score)
            entry = MappingEntry(
                artist=artist,
                title=title,
                youtube_id=metadata.get("youtube_id", ""),
                track_identifier=getattr(track_obj, "identifier", None),
                track_title=metadata.get("title", ""),
                channel_name=metadata.get("channel_name", ""),
                duration_ms=metadata.get("duration_ms"),
                verified=metadata.get("verified", False),
                heuristic_score=10.0,  # High score for Gemini selection
                title_similarity=1.0,
                artist_similarity=1.0,
                channel_similarity=1.0,
                engagement_score=1.0,
                duration_score=1.0,
                content_penalty=0.0,
                spam_penalty=0.0,
                spam_flags=[],
                search_rank=selected_index,
                heuristic_version=3,  # Version 3 = Gemini-powered
            )
            await self._cache.set_mapping(artist, title, entry)

            return track_obj

        except Exception as e:
            LOG.error(
                f"[Gemini Resolution] Error during AI selection: {e}", exc_info=True
            )
            # Fallback to heuristic
            return await self.resolve_track(
                artist,
                title,
                expected_duration_ms=expected_duration_ms,
                prefer_cache=False,
            )

    def _build_gemini_selection_prompt(
        self,
        artist: str,
        title: str,
        expected_duration_ms: Optional[int],
        candidates: List[Dict[str, Any]],
    ) -> str:
        """Build a prompt for Gemini to select the best matching video."""
        duration_str = ""
        if expected_duration_ms:
            duration_sec = expected_duration_ms / 1000
            duration_str = f"\nExpected duration: {duration_sec:.0f} seconds"

        candidates_str = ""
        for i, cand in enumerate(candidates):
            duration_sec = cand["duration_ms"] / 1000 if cand["duration_ms"] else 0
            verified_marker = "✓" if cand["verified"] else ""
            candidates_str += f"\n{i}. \"{cand['video_title']}\" by \"{cand['channel_name']}\" {verified_marker}| Duration: {duration_sec:.0f}s | Views: {cand['view_count']:,}"

        prompt = f"""You are a music track matching expert. Select the BEST YouTube video that matches the requested track.

Requested Track:
Artist: "{artist}"
Title: "{title}"{duration_str}

Available YouTube Videos:{candidates_str}

Instructions:
1. Find the video that BEST matches the artist and title (1:1 match preferred)
2. Prefer verified channels (marked with ✓) and official uploads
3. Avoid covers, remixes, nightcore, slowed versions, live performances unless specifically requested
4. Match duration if provided
5. Prefer channels matching the artist name or official labels (VEVO, Topic, Records)

Respond with ONLY the index number (0-{len(candidates)-1}) of the best match. No explanation needed."""

        return prompt

    def _parse_gemini_selection(self, response: str, max_index: int) -> Optional[int]:
        """Parse Gemini's response to extract the selected index."""
        if not response:
            return None

        # Try to extract number from response
        response = response.strip()

        # Try direct parsing
        try:
            index = int(response)
            if 0 <= index < max_index:
                return index
        except ValueError:
            pass

        # Try to find first number in response
        import re

        match = re.search(r"\b(\d+)\b", response)
        if match:
            index = int(match.group(1))
            if 0 <= index < max_index:
                return index

        return None

    async def validate_pomice_path(self) -> bool:
        node = await self._get_node()
        return node is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _create_track_obj_from_mapping(
        self, mapping: MappingEntry
    ) -> Optional[Any]:
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
                    LOG.debug(
                        "Pomice build_track failed for identifier %s: %s",
                        identifier,
                        exc,
                    )

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
                tracks = await node.get_tracks(
                    f"https://www.youtube.com/watch?v={mapping.youtube_id}"
                )
            except Exception:
                tracks = []

        if not tracks:
            return None

        for track_obj in tracks:
            metadata = self._extract_track_metadata(track_obj)
            if metadata["youtube_id"] == mapping.youtube_id:
                return track_obj
        return tracks[0]

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_text(value: str) -> str:
        if not value:
            return ""
        lowered = value.lower()
        cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
        compact = re.sub(r"\s+", " ", cleaned).strip()
        return compact

    @staticmethod
    def _fold_text(value: str) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        return normalized.encode("ascii", "ignore").decode("ascii").lower()

    @staticmethod
    def _token_ratio(lhs: str, rhs: str) -> float:
        if not lhs or not rhs:
            return 0.0
        if lhs == rhs:
            return 1.0
        matcher = SequenceMatcher(None, lhs, rhs)
        base_ratio = matcher.ratio()
        if base_ratio >= 0.95:
            return 1.0
        lhs_tokens = set(lhs.split())
        rhs_tokens = set(rhs.split())
        if lhs_tokens and rhs_tokens:
            overlap = len(lhs_tokens & rhs_tokens) / float(len(lhs_tokens | rhs_tokens))
            base_ratio = max(base_ratio, overlap)
        return max(0.0, min(base_ratio, 1.0))

    async def _search_with_pomice(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
    ) -> Optional[Tuple[Any, Dict[str, Any]]]:
        node = await self._get_node()
        if not node:
            return None

        query = f"ytsearch: {artist} {title}"
        try:
            results = await node.get_tracks(query)
        except Exception as exc:
            LOG.warning("Pomice search failed for %s: %s", query, exc)
            return None

        if not results:
            LOG.debug("No Pomice results for %s", query)
            return None

        # Get banned tracks for this artist/title combination
        ban_key = (artist.strip().lower(), title.strip().lower())
        banned_ids = set()
        if ban_key in self._banned_tracks:
            banned_ids = {entry[0] for entry in self._banned_tracks[ban_key]}

        best_track: Optional[Any] = None
        best_score = float("-inf")
        best_features: Optional[Dict[str, Any]] = None

        fallback_track: Optional[Any] = None
        fallback_score = float("-inf")
        fallback_features: Optional[Dict[str, Any]] = None
        features_by_id: Dict[int, Dict[str, Any]] = {}

        duration_target = expected_duration_ms or 0
        artist_for_similarity = artist.strip()
        title_for_similarity = title.strip()

        for index, candidate in enumerate(results):
            metadata = self._extract_track_metadata(candidate)

            # Skip banned tracks
            youtube_id = metadata.get("youtube_id", "")
            if youtube_id in banned_ids:
                if LOG.isEnabledFor(logging.DEBUG):
                    LOG.debug(
                        "🚫 [Search] Skipping banned track: youtube_id=%s, channel=%s",
                        youtube_id,
                        metadata.get("channel_name"),
                    )
                continue

            candidate_title_raw = (
                getattr(candidate, "title", None) or metadata.get("title") or ""
            )
            channel_name = metadata.get("channel_name") or ""
            is_verified = bool(metadata.get("verified", False))

            features = self._build_candidate_features(
                artist=artist_for_similarity,
                title=title_for_similarity,
                candidate_title=candidate_title_raw,
                channel_name=channel_name,
                is_verified=is_verified,
                metadata=metadata,
                expected_duration_ms=duration_target if duration_target else None,
                search_rank=index,
            )
            features_by_id[id(candidate)] = features

            score, should_reject, rejection_reasons = self._compose_candidate_score(
                features
            )
            features["score"] = score
            features["rejection_reasons"] = rejection_reasons

            if score > fallback_score:
                fallback_score = score
                fallback_track = candidate
                fallback_features = features

            if should_reject:
                if LOG.isEnabledFor(logging.DEBUG):
                    LOG.debug(
                        "🚫 [FILTER] Rejected '%s' (score=%.2f, reasons=%s, spam_penalty=%.2f, content_penalty=%.2f)",
                        candidate_title_raw[:80],
                        score,
                        ",".join(rejection_reasons) or "unspecified",
                        float(features.get("spam_penalty", 0.0) or 0.0),
                        float(features.get("content_penalty", 0.0) or 0.0),
                    )
                continue

            if score > best_score:
                best_score = score
                best_track = candidate
                best_features = features

        if best_track and best_features:
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "Track resolved via Pomice heuristics: %s",
                    {
                        "artist": artist,
                        "title": title,
                        "score": round(
                            float(best_features.get("score", 0.0) or 0.0), 3
                        ),
                        "title_similarity": round(
                            float(best_features.get("title_similarity", 0.0) or 0.0), 3
                        ),
                        "artist_similarity": round(
                            float(best_features.get("artist_similarity", 0.0) or 0.0), 3
                        ),
                        "channel_similarity": round(
                            float(best_features.get("channel_similarity", 0.0) or 0.0),
                            3,
                        ),
                        "engagement_score": round(
                            float(best_features.get("engagement_score", 0.0) or 0.0), 3
                        ),
                        "verified": best_features.get("verified", False),
                    },
                )
            return best_track, best_features

        if fallback_track and fallback_features:
            fallback_metadata = self._extract_track_metadata(fallback_track)
            LOG.warning(
                "⚠️ All candidates filtered out for '%s - %s', using best fallback (score=%.2f, views=%d, subs=%d, channel='%s')",
                artist,
                title,
                fallback_score,
                fallback_metadata.get("view_count", 0),
                fallback_metadata.get("subscriber_count", 0),
                fallback_metadata.get("channel_name", "Unknown"),
            )
            return fallback_track, fallback_features

        # Phase 6: Final Failure - Use highest engagement as last resort
        if results:
            # Find track with highest engagement score
            best_engagement_track = None
            best_engagement_score = 0.0
            best_engagement_features = None

            for candidate in results:
                metadata = self._extract_track_metadata(candidate)
                # Calculate simple engagement score based on views and subs
                view_count = metadata.get("view_count", 0)
                subscriber_count = metadata.get("subscriber_count", 0)

                engagement = 0.0
                if view_count > 0:
                    engagement += math.log10(view_count + 1) / 7.0
                if subscriber_count > 0:
                    engagement += math.log10(subscriber_count + 1) / 6.0

                if engagement > best_engagement_score:
                    best_engagement_score = engagement
                    best_engagement_track = candidate
                    best_engagement_features = features_by_id.get(id(candidate), {})

            if best_engagement_track:
                engagement_metadata = self._extract_track_metadata(
                    best_engagement_track
                )
                LOG.warning(
                    "⚠️ No valid candidates found for '%s - %s', using highest engagement as last resort (channel='%s', views=%d, subs=%d, engagement=%.2f)",
                    artist,
                    title,
                    engagement_metadata.get("channel_name", "Unknown"),
                    engagement_metadata.get("view_count", 0),
                    engagement_metadata.get("subscriber_count", 0),
                    best_engagement_score,
                )
                return best_engagement_track, best_engagement_features or {}

        return None

    def _build_candidate_features(
        self,
        *,
        artist: str,
        title: str,
        candidate_title: str,
        channel_name: str,
        is_verified: bool,
        metadata: Dict[str, Any],
        expected_duration_ms: Optional[int],
        search_rank: int,
    ) -> Dict[str, Any]:
        normalized_artist = self._normalize_text(artist)
        normalized_title = self._normalize_text(title)
        candidate_title_norm = self._normalize_text(candidate_title)
        channel_name_norm = self._normalize_text(channel_name)
        title_folded = self._fold_text(candidate_title)

        title_similarity = self._token_ratio(normalized_title, candidate_title_norm)
        if normalized_title and normalized_title in candidate_title_norm:
            title_similarity = max(title_similarity, 0.92)

        # Penalize multi-part titles when looking for simple titles
        # e.g., "IMPULSE!" vs "Impulse - 2 - Ameliorate"
        title_part_penalty = 0.0
        candidate_has_track_number = False

        # Check for track numbers: "- 2 -", "Track 2", "Pt. 2", etc.
        track_number_patterns = [
            r"\s-\s\d+\s-\s",
            r"\strack\s*\d+",
            r"\s#\d+",
            r"\spt\.?\s*\d+",
            r"\s\d+\s*of\s*\d+",
        ]

        for pattern in track_number_patterns:
            if re.search(pattern, candidate_title_norm):
                candidate_has_track_number = True
                title_part_penalty += 0.25
                break

        # Check if candidate title has multiple parts separated by " - " or " | "
        title_parts_candidate = len(re.split(r"\s+[-|]\s+", candidate_title_norm))
        title_parts_expected = len(re.split(r"\s+[-|]\s+", normalized_title))

        if title_parts_candidate > title_parts_expected + 1:
            # Candidate has more parts than expected (e.g., "Artist - Title - Subtitle")
            title_part_penalty += 0.15 * (title_parts_candidate - title_parts_expected)

        # If the expected title is very short and simple, be stricter
        if len(normalized_title.split()) <= 2 and title_parts_candidate > 2:
            title_part_penalty += 0.2

        artist_in_title = (
            1.0
            if normalized_artist and normalized_artist in candidate_title_norm
            else 0.0
        )
        artist_in_channel = (
            1.0 if normalized_artist and normalized_artist in channel_name_norm else 0.0
        )

        artist_similarity_title = self._token_ratio(
            normalized_artist, candidate_title_norm
        )
        artist_similarity_channel = self._token_ratio(
            normalized_artist, channel_name_norm
        )
        artist_similarity = max(
            artist_similarity_title,
            artist_similarity_channel,
            artist_in_title,
            artist_in_channel,
        )

        # Apply stricter artist matching when title has track numbers or extra parts
        if title_part_penalty > 0.0:
            # Require higher artist similarity threshold
            if artist_similarity < 0.75:
                # Strong penalty if artist doesn't match well
                artist_similarity *= 1.0 - min(title_part_penalty * 2.0, 0.9)
            elif artist_similarity < 0.85:
                # Moderate penalty for partial matches
                artist_similarity *= 1.0 - min(title_part_penalty, 0.5)

        channel_similarity = artist_similarity_channel
        if artist_in_channel:
            channel_similarity = max(channel_similarity, 1.0)

        channel_official_hint_score = 0.0
        channel_lower = channel_name.lower()
        for hint in GOOD_CHANNEL_HINTS:
            if hint in channel_lower:
                channel_official_hint_score += 0.3
        channel_official_hint_score = min(channel_official_hint_score, 1.0)

        positive_title_hint_score = 0.0
        title_lower = candidate_title.lower()
        for hint in POSITIVE_MUSIC_HINTS:
            if hint in title_lower or hint in title_folded:
                positive_title_hint_score += 0.2
        positive_title_hint_score = min(positive_title_hint_score, 1.0)

        duration_ms = metadata.get("duration_ms") or 0
        duration_score = None
        duration_within_tolerance = True
        duration_delta_ms: Optional[int] = None
        duration_tolerance_ms = None
        if expected_duration_ms and duration_ms:
            duration_delta_ms = abs(expected_duration_ms - int(duration_ms))
            base_tolerance = max(
                int(expected_duration_ms * DURATION_TOLERANCE_PERCENT),
                DURATION_TOLERANCE_MIN_MS,
            )
            duration_tolerance_ms = base_tolerance
            if is_verified or channel_official_hint_score >= 0.6:
                # Allow slightly wider tolerance for official uploads
                duration_tolerance_ms = int(base_tolerance * 1.4)
            duration_within_tolerance = duration_delta_ms <= duration_tolerance_ms
            ratio = 1.0 - min(duration_delta_ms / max(expected_duration_ms, 1), 1.2)
            duration_score = max(0.0, min(ratio, 1.0))

        view_count = metadata.get("view_count") or 0
        like_count = metadata.get("like_count") or 0
        comment_count = metadata.get("comment_count") or 0
        subscriber_count = metadata.get("subscriber_count") or 0

        engagement_components: List[float] = []
        if view_count > 0:
            engagement_components.append(min(math.log10(view_count + 1) / 7.0, 1.0))
        if subscriber_count > 0:
            engagement_components.append(
                min(math.log10(subscriber_count + 1) / 6.0, 1.0)
            )
        if view_count > 1000:
            engagement_ratio = (like_count + (comment_count * 2)) / view_count
            engagement_components.append(min(engagement_ratio * 12.0, 1.0))
        engagement_score = min(sum(engagement_components), 1.2)

        spam_penalty = 0.0
        spam_flags: List[str] = []
        hashtag_count = candidate_title.count("#")
        if hashtag_count > HASHTAG_LIMIT:
            spam_penalty += 1.0 + 0.1 * (hashtag_count - HASHTAG_LIMIT)
            spam_flags.append("hashtags")

        allowed_keywords = {kw.lower() for kw in ALLOWED_IF_OFFICIAL}
        for keyword in BAD_TITLE_KEYWORDS:
            keyword_lower = keyword.lower()
            if keyword_lower in title_lower or keyword_lower in title_folded:
                if (
                    is_verified or channel_official_hint_score >= 0.6
                ) and keyword_lower in allowed_keywords:
                    continue
                penalty = 0.45
                if keyword_lower in CRITICAL_SPAM_KEYWORDS:
                    penalty = max(penalty, 1.2)
                spam_penalty += penalty
                spam_flags.append(keyword)

        content_penalty = 0.0
        episode_like = False
        for keyword, weight in CONTENT_PENALTY_KEYWORDS.items():
            if keyword in title_lower or keyword in title_folded:
                content_penalty += weight
                spam_flags.append(f"content:{keyword}")
                if keyword.startswith("episode") or keyword in {
                    "pilot",
                    "full episode",
                }:
                    episode_like = True

        if not episode_like:
            if re.search(r"episode\s*\d", title_folded) or re.search(
                r"s\d+e\d+", title_folded
            ):
                episode_like = True
                spam_flags.append("content:episode_pattern")
                content_penalty += 1.1

        if content_penalty:
            if is_verified or channel_official_hint_score >= 0.6:
                content_penalty *= 0.35
            if positive_title_hint_score > 0:
                content_penalty *= max(0.3, 1.0 - (positive_title_hint_score * 0.4))

        short_clip = bool(duration_ms and duration_ms < SHORT_CLIP_THRESHOLD_MS)

        if episode_like and positive_title_hint_score < 0.2:
            content_penalty += 0.9

        # Apply title part penalty to title similarity
        adjusted_title_similarity = title_similarity * (
            1.0 - min(title_part_penalty, 0.7)
        )

        return {
            "artist": artist,
            "title": title,
            "candidate_title": candidate_title,
            "channel_name": channel_name,
            "verified": is_verified,
            "title_similarity": adjusted_title_similarity,
            "artist_similarity": artist_similarity,
            "channel_similarity": channel_similarity,
            "artist_in_title": artist_in_title,
            "artist_in_channel": artist_in_channel,
            "channel_official_hint_score": channel_official_hint_score,
            "positive_title_hint_score": positive_title_hint_score,
            "engagement_score": engagement_score,
            "view_count": view_count,
            "subscriber_count": subscriber_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "duration_ms": duration_ms,
            "duration_score": duration_score,
            "duration_within_tolerance": duration_within_tolerance,
            "duration_delta_ms": duration_delta_ms,
            "duration_tolerance_ms": duration_tolerance_ms,
            "content_penalty": content_penalty,
            "spam_penalty": spam_penalty,
            "spam_flags": spam_flags,
            "search_rank": search_rank,
            "short_clip": short_clip,
            "episode_like": episode_like,
            "title_part_penalty": title_part_penalty,
            "candidate_has_track_number": candidate_has_track_number,
            "heuristic_version": 2,
        }

    def _compose_candidate_score(
        self, features: Dict[str, Any]
    ) -> Tuple[float, bool, List[str]]:
        score = 0.0

        title_similarity = float(features.get("title_similarity") or 0.0)
        artist_similarity = float(features.get("artist_similarity") or 0.0)
        channel_similarity = float(features.get("channel_similarity") or 0.0)
        engagement_score = float(features.get("engagement_score") or 0.0)
        duration_score = features.get("duration_score")
        duration_score_val = (
            float(duration_score) if duration_score is not None else 0.7
        )
        content_penalty = float(features.get("content_penalty") or 0.0)
        spam_penalty = float(features.get("spam_penalty") or 0.0)
        channel_official_hint_score = float(
            features.get("channel_official_hint_score") or 0.0
        )
        positive_hint_score = float(features.get("positive_title_hint_score") or 0.0)
        verified = bool(features.get("verified", False))
        search_rank = int(features.get("search_rank", 0) or 0)
        episode_like = bool(features.get("episode_like", False))

        score += 2.0 * title_similarity
        score += (
            3.0 * artist_similarity
        )  # Increased from 2.2 to prioritize artist matching
        score += 1.2 * channel_similarity
        score += 1.8 * engagement_score
        score += 0.9 * channel_official_hint_score
        score += 0.6 * positive_hint_score
        score += 1.0 * duration_score_val

        if verified:
            score += 2.5

        score -= 0.85 * spam_penalty
        score -= 1.1 * content_penalty
        score -= search_rank * 0.18

        rejection_reasons: List[str] = []
        should_reject = False

        if features.get("short_clip") and not verified:
            should_reject = True
            rejection_reasons.append("short_clip")

        duration_within_tolerance = bool(
            features.get("duration_within_tolerance", True)
        )
        if (
            not duration_within_tolerance
            and (duration_score is not None)
            and duration_score_val < 0.35
        ):
            should_reject = True
            rejection_reasons.append("duration_mismatch")

        if title_similarity < 0.35 and not verified:
            should_reject = True
            rejection_reasons.append("low_title_similarity")

        if artist_similarity < 0.25 and not verified:
            should_reject = True
            rejection_reasons.append("low_artist_similarity")

        # Stricter artist matching when candidate has track numbers/multi-part titles
        title_part_penalty = float(features.get("title_part_penalty") or 0.0)
        if title_part_penalty > 0.3 and artist_similarity < 0.65 and not verified:
            should_reject = True
            rejection_reasons.append("artist_mismatch_with_track_number")

        if spam_penalty >= 1.8 and not verified:
            should_reject = True
            rejection_reasons.append("spam_penalty")

        if content_penalty >= 2.2 and (
            not verified and channel_official_hint_score < 0.4
        ):
            should_reject = True
            rejection_reasons.append("content_penalty")

        if episode_like and positive_hint_score < 0.2:
            should_reject = True
            rejection_reasons.append("episode_content")
        elif episode_like:
            score -= 2.1

        return score, should_reject, rejection_reasons

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
        track_identifier = getattr(track_obj, "track_id", None) or getattr(
            track_obj, "track", None
        )

        # Extract engagement metrics (may not always be available from Lavalink/Pomice)
        view_count = info.get("viewCount") or info.get("views") or 0
        like_count = info.get("likeCount") or info.get("likes") or 0
        comment_count = info.get("commentCount") or info.get("comments") or 0
        subscriber_count = info.get("subscriberCount") or info.get("subscribers") or 0

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
            "view_count": int(view_count) if view_count else 0,
            "like_count": int(like_count) if like_count else 0,
            "comment_count": int(comment_count) if comment_count else 0,
            "subscriber_count": int(subscriber_count) if subscriber_count else 0,
        }

    def _is_spam_mapping(
        self, mapping: MappingEntry, expected_duration_ms: Optional[int]
    ) -> bool:
        """Check if a cached mapping is spam using heuristics."""

        # Check 1: Duration mismatch (if we have expected duration)
        if expected_duration_ms and mapping.duration_ms:
            tolerance_ms = max(
                expected_duration_ms * DURATION_TOLERANCE_PERCENT,
                DURATION_TOLERANCE_MIN_MS,
            )
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

    def _is_spam_title(
        self, title: str, is_verified: bool = False, channel_name: str = ""
    ) -> bool:
        """Check if a YouTube title indicates spam/non-music content.

        Args:
            title: The video title
            is_verified: Whether the channel is verified/official
            channel_name: The channel name (for additional context)
        """

        if not title:
            return False

        title_lower = title.lower()
        channel_lower = (channel_name or "").lower()

        # Check 1: Excessive hashtags
        hashtag_count = title.count("#")
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
                # If it's an allowed keyword and the channel is official/verified, skip this filter
                if keyword in [k.lower() for k in ALLOWED_IF_OFFICIAL]:
                    if is_verified:
                        LOG.debug(
                            "✅ [SPAM BYPASS] Keyword '%s' allowed for verified channel in '%s'",
                            keyword,
                            title[:60],
                        )
                        continue
                    # Also allow if channel name suggests official (has artist name, vevo, topic, etc.)
                    if channel_lower and any(
                        hint in channel_lower
                        for hint in ["vevo", "topic", "records", "official"]
                    ):
                        LOG.debug(
                            "✅ [SPAM BYPASS] Keyword '%s' allowed for official channel '%s' in '%s'",
                            keyword,
                            channel_name,
                            title[:60],
                        )
                        continue

                LOG.debug(
                    "🚫 [SPAM] Spam keyword '%s' found in '%s'",
                    keyword,
                    title[:60],
                )
                return True

        return False


__all__ = ["TrackResolver"]
