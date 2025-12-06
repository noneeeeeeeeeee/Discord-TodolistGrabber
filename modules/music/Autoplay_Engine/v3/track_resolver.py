import asyncio
import json
import logging
import math
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from .cache_manager import CacheManager, MappingEntry, ParsingEntry
from .config import PARSING_SCHEMA_VERSION

if TYPE_CHECKING:
    from .deezer_fetch import DeezerClient

LOG = logging.getLogger(__name__)

# Spam detection thresholds
HASHTAG_LIMIT = 3
DURATION_TOLERANCE_PERCENT = 0.20  # 20%
DURATION_TOLERANCE_MIN_MS = 30000  # 30 seconds minimum tolerance
SHORT_CLIP_THRESHOLD_MS = 30000  # clips shorter than 30s are likely YouTube Shorts/spam

# Confidence threshold for hybrid resolution (0-1 scale for simplified scoring)
LOW_CONFIDENCE_THRESHOLD = 0.55  

# High-quality channel indicators (boost priority)
GOOD_CHANNEL_HINTS = [
    "vevo",
    "official artist channel",
    "topic",
    "records",
    "music",
    "label",
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

    # Class-level semaphore to limit concurrent Gemini resolution calls
    # This prevents throttling by ensuring only 2 Gemini calls happen at once
    _gemini_resolution_semaphore = asyncio.Semaphore(2)

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
                # Check for cache poisoning (artist/title mismatch)
                title_sim = self._token_ratio(title, mapping.title or "")
                artist_sim = self._token_ratio(artist, mapping.channel_name or "")
                
                # If cached title/artist is less than 60% similar, it's poisoned
                if title_sim < 0.6 or artist_sim < 0.6:
                    LOG.warning(
                        "🚫 [Cache Poisoning] Invalidating stale mapping for '%s - %s' "
                        "(cached: '%s' / '%s', title_sim=%.2f, artist_sim=%.2f)",
                        artist, title, mapping.title, mapping.channel_name,
                        title_sim, artist_sim
                    )
                    await self._cache.delete_mapping(artist, title)
                    mapping = None  
                # Validate cached mapping against spam filters
                elif self._is_spam_mapping(mapping, expected_duration_ms):
                    LOG.warning(
                        "🚫 [CACHE SPAM] Invalidating cached mapping for '%s - %s' (youtube_id=%s, reason=spam detected)",
                        artist,
                        title,
                        mapping.youtube_id,
                    )
                    # Delete bad mapping from cache
                    await self._cache.delete_mapping(artist, title)
                    mapping = None 
                
                # Only proceed with cache hit if mapping is still valid
                if mapping:
                    rebuilt = await self._create_track_obj_from_mapping(mapping)
                    if rebuilt:
                        LOG.info(
                            "📁 [Cache Hit: YouTube Mapping] %s - %s -> youtube_id=%s, score=%.2f",
                            artist[:30] + "..." if len(artist) > 30 else artist,
                            title[:40] + "..." if len(title) > 40 else title,
                            mapping.youtube_id,
                            float(mapping.heuristic_score or 0.0),
                        )
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
                                    "content_penalty": mapping.content_penalty,
                                    "spam_penalty": mapping.spam_penalty,
                                    "spam_flags": mapping.spam_flags,
                                    "heuristic_version": mapping.heuristic_version,
                                },
                            )
                        return rebuilt

        search_result = await self._multi_stage_pomice_search(
            artist,
            title,
            expected_duration_ms=expected_duration_ms,
        )

        # Phase 4: Confidence Check & Branching
        if not search_result:
            # No heuristic result at all - try Gemini-assisted YouTube search
            if self._gemini_service:
                LOG.warning(
                    "⚠️ [Hybrid] Heuristic search failed for '%s - %s', using Gemini AI",
                    artist,
                    title,
                )
                return await self.resolve_to_youtube_with_gemini(
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

        # Check confidence threshold - if low, use Gemini for better selection
        if heuristic_score < LOW_CONFIDENCE_THRESHOLD:
            if self._gemini_service:
                LOG.info(
                    "🔄 [Hybrid] Low confidence score %.2f < %.2f for '%s - %s', using Gemini AI",
                    heuristic_score,
                    LOW_CONFIDENCE_THRESHOLD,
                    artist,
                    title,
                )
                gemini_track = await self.resolve_to_youtube_with_gemini(
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
                    "[Hybrid] Low confidence score %.2f but Gemini not available",
                    heuristic_score,
                )

        # High confidence or Gemini unavailable - use heuristic result
        metadata = self._extract_track_metadata(track_obj)
        youtube_id_val = (metadata.get("youtube_id") or "").strip()
        url_val = (metadata.get("url") or "").strip()
        if youtube_id_val and url_val:
            duration_val = self._safe_int(metadata.get("duration_ms"))
            if expected_duration_ms and duration_val is not None:
                duration_val = int(duration_val)

            entry = MappingEntry(
                youtube_id=str(youtube_id_val),
                url=str(url_val),
                timestamp=time.time(),
                channel_name=(metadata.get("channel_name") or None),
                verified=bool(metadata.get("verified", False)),
                duration_ms=duration_val,
                track_identifier=metadata.get("track_identifier"),
                title=str(
                    metadata.get("title") or heuristics.get("candidate_title") or ""
                )
                or None,
                preview_url=(metadata.get("preview_url") or None),
                preview_duration_ms=self._safe_int(
                    metadata.get("preview_duration_ms")
                ),
                preview_fetched_at=self._safe_float(
                    metadata.get("preview_fetched_at")
                ),
                deezer_track_id=(metadata.get("deezer_track_id") or None),
                ingest_source=(metadata.get("ingest_source") or None),
                heuristic_score=float(heuristics.get("score", 0.0) or 0.0),
                title_similarity=self._safe_float(heuristics.get("title_similarity")),
                artist_similarity=self._safe_float(heuristics.get("artist_similarity")),
                channel_similarity=self._safe_float(
                    heuristics.get("channel_similarity")
                ),
                content_penalty=self._safe_float(heuristics.get("content_penalty")),
                spam_penalty=self._safe_float(heuristics.get("spam_penalty")),
                spam_flags=list(heuristics.get("spam_flags", [])),
                search_rank=self._safe_int(heuristics.get("search_rank")) or 0,
                heuristic_version=int(heuristics.get("heuristic_version", 3) or 3),
            )
            await self._cache.set_mapping(artist, title, entry)
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
    def _safe_int(value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(float(value))
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

    async def _multi_stage_pomice_search(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
    ) -> Optional[Tuple[Any, Dict[str, Any]]]:
        node = await self._get_node()
        if not node:
            return None

        attempted_queries: List[str] = []
        best_candidate: Optional[Tuple[Any, Dict[str, Any]]] = None
        best_score = float("-inf")

        primary_queries = self._build_primary_search_queries(artist, title)
        for attempt_index, query in enumerate(primary_queries):
            attempted_queries.append(query)
            result = await self._search_with_pomice(
                artist,
                title,
                expected_duration_ms=expected_duration_ms,
                search_query=query,
                node=node,
            )
            if not result:
                continue

            track_obj, features = result
            if "search_query" not in features:
                features["search_query"] = query

            score = float(features.get("score", 0.0) or 0.0)
            if not best_candidate or score > best_score:
                best_candidate = (track_obj, features)
                best_score = score

            if self._should_accept_candidate(features, attempt_index):
                if LOG.isEnabledFor(logging.DEBUG):
                    LOG.debug(
                        "✅ [Search Pipeline] Accepted candidate via '%s' (score=%.2f, engagement=%.2f)",
                        query,
                        score,
                        float(features.get("engagement_score", 0.0) or 0.0),
                    )
                return track_obj, features

            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "🔁 [Search Pipeline] Candidate via '%s' scored %.2f (engagement=%.2f); trying next strategy",
                    query,
                    score,
                    float(features.get("engagement_score", 0.0) or 0.0),
                )

        if self._gemini_service:
            gemini_queries = await self._generate_gemini_search_queries(
                artist, title, attempted_queries
            )
            for extra_index, query in enumerate(
                gemini_queries, start=len(attempted_queries)
            ):
                attempted_queries.append(query)
                result = await self._search_with_pomice(
                    artist,
                    title,
                    expected_duration_ms=expected_duration_ms,
                    search_query=query,
                    node=node,
                )
                if not result:
                    continue

                track_obj, features = result
                if "search_query" not in features:
                    features["search_query"] = query

                score = float(features.get("score", 0.0) or 0.0)
                if not best_candidate or score > best_score:
                    best_candidate = (track_obj, features)
                    best_score = score

                if self._should_accept_candidate(features, extra_index):
                    LOG.info(
                        "🤖 [Search Pipeline] Gemini-refined query '%s' produced acceptable candidate (score=%.2f)",
                        query,
                        score,
                    )
                    return track_obj, features

                if LOG.isEnabledFor(logging.DEBUG):
                    LOG.debug(
                        "🔍 [Search Pipeline] Gemini query '%s' scored %.2f; continuing",
                        query,
                        score,
                    )

        return best_candidate

    def _build_primary_search_queries(self, artist: str, title: str) -> List[str]:
        """
        Simplified query builder - use flexible query like manual YouTube search.
        Don't use exact quotes - let YouTube's fuzzy matching handle variations.
        """
        artist_clean = artist.replace("\"", "").strip()
        title_clean = title.replace("\"", "").strip()
        
        if not artist_clean or not title_clean:
            return []
        
        return [f'ytsearch:{title_clean} {artist_clean}']

    def _should_accept_candidate(
        self, features: Dict[str, Any], attempt_index: int
    ) -> bool:
        """
        Simplified acceptance criteria - trust similarity scores and verified status.
        No engagement or duration requirements (data unavailable).
        """
        score = float(features.get("score", 0.0) or 0.0)
        verified = bool(features.get("verified", False))
        title_similarity = float(features.get("title_similarity", 0.0) or 0.0)
        artist_similarity = float(features.get("artist_similarity", 0.0) or 0.0)
        search_rank = int(features.get("search_rank", 0) or 0)

        # Accept if high overall score (>0.60 in 0-1 range)
        if score >= 0.60:
            return True
        
        # Accept verified channels with good similarity (trust official uploads)
        if verified and title_similarity >= 0.50:
            return True
        
        # Accept top result with reasonable similarity (trust YouTube ranking)
        if search_rank == 0 and title_similarity >= 0.55 and artist_similarity >= 0.40:
            return True
        
        # Accept good artist+title match
        if title_similarity >= 0.60 and artist_similarity >= 0.50:
            return True
        
        return False

    async def _generate_gemini_search_queries(
        self,
        artist: str,
        title: str,
        attempted_queries: List[str],
        limit: int = 2,
    ) -> List[str]:
        if not self._gemini_service or not self._gemini_service.is_available:
            return []

        try:
            plain_attempts = [
                query.split(":", 1)[1].strip() if ":" in query else query
                for query in attempted_queries
            ]
        except Exception:
            plain_attempts = attempted_queries

        prompt = (
            "You help resolve the best official YouTube upload for a song.\n"
            f"Song: Artist='{artist}', Title='{title}'.\n"
            "Existing search attempts: "
            + ", ".join(f"'{item}'" for item in plain_attempts[:5])
            + "\nProvide up to "
            + str(limit)
            + (
                " refined search queries targeting official or high-quality uploads. "
                "Return STRICT JSON: {\"queries\": [\"query1\", ...]} without explanations."
                " If unsure, return {\"queries\": []}."
            )
        )

        response = await self._gemini_service.query_gemini(
            prompt, allow_grounding=True
        )
        if not response:
            return []

        text = response.strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 2:
                text = parts[1].strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            LOG.warning(
                "⚠️ [Search Pipeline] Gemini returned unparsable payload for '%s - %s'", artist, title
            )
            return []

        raw_queries: List[str] = []
        if isinstance(payload, dict):
            candidate_list = payload.get("queries") or payload.get("search_queries")
            if isinstance(candidate_list, list):
                raw_queries = [str(item).strip() for item in candidate_list]
        elif isinstance(payload, list):
            raw_queries = [str(item).strip() for item in payload]

        deduped: List[str] = []
        seen_lower = {query.lower() for query in attempted_queries}
        for raw in raw_queries:
            if not raw:
                continue
            normalized = raw.strip()
            if not normalized:
                continue

            lowered = normalized.lower()
            if lowered.startswith("ytsearch:") or lowered.startswith("ytmsearch:"):
                full_query = normalized
            else:
                full_query = f"ytsearch: {normalized}"

            if full_query.lower() in seen_lower:
                continue
            deduped.append(full_query)
            seen_lower.add(full_query.lower())
            if len(deduped) >= limit:
                break

        if deduped and LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(
                "🤖 [Search Pipeline] Gemini proposed fallback queries: %s",
                deduped,
            )

        return deduped

    async def _search_with_pomice(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
        search_query: Optional[str] = None,
        node: Optional[Any] = None,
    ) -> Optional[Tuple[Any, Dict[str, Any]]]:
        local_node = node or await self._get_node()
        if not local_node:
            return None

        query = search_query or f"ytsearch: {artist} {title}"
        try:
            results = await local_node.get_tracks(query)
        except Exception as exc:
            LOG.warning("Pomice search failed for %s: %s", query, exc)
            return None

        if not results:
            LOG.debug("No Pomice results for %s", query)
            return None

        ban_key = (artist.strip().lower(), title.strip().lower())
        banned_ids = (
            {entry[0] for entry in self._banned_tracks.get(ban_key, [])}
            if ban_key in self._banned_tracks
            else set()
        )

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
            features["search_query"] = query
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
                        "query": best_features.get("search_query"),
                    },
                )
            return best_track, best_features

        if fallback_track and fallback_features:
            fallback_metadata = self._extract_track_metadata(fallback_track)
            LOG.warning(
                "⚠️ All candidates filtered out for '%s - %s', using best fallback (score=%.2f, views=%d, subs=%d, channel='%s', query='%s')",
                artist,
                title,
                fallback_score,
                fallback_metadata.get("view_count", None),
                fallback_metadata.get("subscriber_count", None),
                fallback_metadata.get("channel_name", "Unknown"),
                fallback_features.get("search_query"),
            )
            return fallback_track, fallback_features

        if results:
            best_engagement_track = None
            best_engagement_score = 0.0
            best_engagement_features = None

            for candidate in results:
                metadata = self._extract_track_metadata(candidate)
                view_count = metadata.get("view_count") or 0
                subscriber_count = metadata.get("subscriber_count") or 0

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
                    "⚠️ No valid candidates found for '%s - %s', using highest engagement as last resort (channel='%s', views=%d, subs=%d, engagement=%.2f, query='%s')",
                    artist,
                    title,
                    engagement_metadata.get("channel_name", "Unknown"),
                    engagement_metadata.get("view_count", 0),
                    engagement_metadata.get("subscriber_count", 0),
                    best_engagement_score,
                    (best_engagement_features or {}).get("search_query"),
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
        title_part_penalty = 0.0
        candidate_has_track_number = False

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
                artist_similarity *= 1.0 - min(title_part_penalty * 2.0, 0.9)
            elif artist_similarity < 0.85:
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

        spam_penalty = 0.0
        spam_flags: List[str] = []
        hashtag_count = candidate_title.count("#")
        if hashtag_count > HASHTAG_LIMIT:
            spam_penalty += 1.0 + 0.1 * (hashtag_count - HASHTAG_LIMIT)
            spam_flags.append("hashtags")

        # Check if this is a collaboration (feat., ft., with, etc.)
        is_collaboration = bool(
            re.search(r'\b(feat\.|ft\.|featuring|with)\b', title_lower)
        )

        allowed_keywords = {kw.lower() for kw in ALLOWED_IF_OFFICIAL}
        for keyword in BAD_TITLE_KEYWORDS:
            keyword_lower = keyword.lower()
            if keyword_lower in title_lower or keyword_lower in title_folded:
                # Allow if verified, official channel, OR legitimate collaboration
                if (is_verified or channel_official_hint_score >= 0.6) and keyword_lower in allowed_keywords:
                    continue
                # Allow "remix" if it's a collaboration crediting original artist
                if keyword_lower in ("remix", "cover") and is_collaboration:
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
            "duration_ms": duration_ms,
            "content_penalty": content_penalty,
            "spam_penalty": spam_penalty,
            "spam_flags": spam_flags,
            "search_rank": search_rank,
            "short_clip": short_clip,
            "episode_like": episode_like,
            "title_part_penalty": title_part_penalty,
            "candidate_has_track_number": candidate_has_track_number,
            "heuristic_version": 3,  # Bumped to v3 for simplified scoring
        }

    def _compose_candidate_score(
        self, features: Dict[str, Any]
    ) -> Tuple[float, bool, List[str]]:
        """
        Simplified scoring that trusts YouTube's ranking (like !p command).
        No engagement data, no duration validation - just similarity + rank + verified.
        """
        # Extract only the features we can actually use
        title_similarity = float(features.get("title_similarity") or 0.0)
        artist_similarity = float(features.get("artist_similarity") or 0.0)
        spam_penalty = float(features.get("spam_penalty") or 0.0)
        content_penalty = float(features.get("content_penalty") or 0.0)
        verified = bool(features.get("verified", False))
        search_rank = int(features.get("search_rank", 0) or 0)
        episode_like = bool(features.get("episode_like", False))

        # Simple scoring formula:
        # - 40% weight on title match
        # - 30% weight on artist match  
        # - 20% bonus for verified channels (official uploads)
        # - 10% weight on search rank (prefer top results)
        score = (title_similarity * 0.4 +
                 artist_similarity * 0.3 +
                 (1.0 if verified else 0.0) * 0.2 +
                 (1.0 - search_rank / 5.0) * 0.1)
        
        # Apply spam/content penalties
        score -= spam_penalty * 0.15
        score -= content_penalty * 0.20

        # Rejection logic - only reject obvious mismatches
        rejection_reasons: List[str] = []
        should_reject = False

        # Reject if title/artist similarity is too low (unless verified)
        if title_similarity < 0.50 and not verified:
            should_reject = True
            rejection_reasons.append("low_title_similarity")

        if artist_similarity < 0.40 and not verified:
            should_reject = True
            rejection_reasons.append("low_artist_similarity")

        # For verified channels, be more lenient (trust official uploads)
        if verified and title_similarity < 0.40:
            should_reject = True
            rejection_reasons.append("low_title_similarity_verified")

        # Reject high spam/episode content
        if spam_penalty >= 2.0 and not verified:
            should_reject = True
            rejection_reasons.append("spam_penalty")

        if episode_like:
            should_reject = True
            rejection_reasons.append("episode_content")

        if content_penalty >= 2.5 and not verified:
            should_reject = True
            rejection_reasons.append("content_penalty")

        return score, should_reject, rejection_reasons

    async def _get_node(self) -> Optional[Any]:
        try:
            import pomice 
        except Exception as exc:  
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
        preview_url = (
            info.get("preview_url")
            or info.get("previewUrl")
            or getattr(track_obj, "preview_url", None)
        )
        preview_duration_ms = (
            info.get("preview_duration_ms")
            or info.get("previewDurationMs")
            or info.get("preview_duration")
            or getattr(track_obj, "preview_duration_ms", None)
        )
        preview_fetched_at = (
            info.get("preview_fetched_at")
            or info.get("previewFetchedAt")
            or getattr(track_obj, "preview_fetched_at", None)
        )
        deezer_track_id = (
            info.get("deezer_track_id")
            or info.get("deezerTrackId")
            or getattr(track_obj, "deezer_track_id", None)
        )
        ingest_source = (
            info.get("ingest_source")
            or info.get("ingestSource")
            or getattr(track_obj, "ingest_source", None)
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

        try:
            preview_duration_int = (
                int(preview_duration_ms) if preview_duration_ms is not None else None
            )
        except (TypeError, ValueError):
            preview_duration_int = None

        try:
            preview_fetched_at_float = (
                float(preview_fetched_at) if preview_fetched_at is not None else None
            )
        except (TypeError, ValueError):
            preview_fetched_at_float = None

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
            "preview_url": preview_url,
            "preview_duration_ms": preview_duration_int,
            "preview_fetched_at": preview_fetched_at_float,
            "deezer_track_id": deezer_track_id,
            "ingest_source": ingest_source,
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

    # =========================================================================
    # BATCH RESOLUTION SYSTEM (Last.fm > Deezer with Gemini Fallback)
    # =========================================================================
    
    async def resolve_batch_to_deezer(
        self,
        tracks: List[Dict[str, str]],
        *,
        source: str = "lastfm",
        max_gemini_batch: int = 50,
    ) -> Dict[str, Any]:
        """
        Resolve a batch of tracks from Last.fm/YouTube to Deezer with multi-phase waterfall.
        
        Phase 1: Waterfall search (3 strategies per track)
        Phase 2: Gemini batch fallback for failures (max 50 tracks)
        
        Args:
            tracks: List of dicts with 'artist' and 'title' keys
            source: Source of tracks ('lastfm', 'youtube')
            max_gemini_batch: Maximum tracks to send to Gemini (default: 50)
            
        Returns:
            {
                'resolved': [{'artist': str, 'title': str, 'deezer_id': int, ...}],
                'failed': [{'artist': str, 'title': str, 'reason': str}],
                'stats': {'phase1_resolved': int, 'phase2_resolved': int, 'total_failed': int}
            }
        """
        from .deezer_fetch import DeezerClient
        
        resolved: List[Dict[str, Any]] = []
        phase1_failed: List[Dict[str, str]] = []
        stats = {
            "input_count": len(tracks),
            "phase1_resolved": 0,
            "phase2_resolved": 0,
            "total_failed": 0,
        }
        
        LOG.info(
            "🔄 [Batch Resolver] Starting resolution for %d tracks (source=%s)",
            len(tracks), source
        )
        
        # Phase 1: Waterfall search with 3 strategies
        async with DeezerClient(max_concurrent=10, timeout=10.0) as client:
            for track in tracks:
                artist = (track.get("artist") or "").strip()
                title = (track.get("title") or "").strip()
                
                if not artist or not title:
                    phase1_failed.append({
                        "artist": artist,
                        "title": title,
                        "reason": "missing_metadata"
                    })
                    continue
                
                result = await self._waterfall_deezer_search(client, artist, title)
                
                if result:
                    resolved.append({
                        "artist": result.artist,
                        "title": result.title,
                        "deezer_id": result.id,
                        "preview_url": result.preview_url,
                        "duration_ms": result.duration_ms,
                        "original_artist": artist,
                        "original_title": title,
                        "phase": 1,
                    })
                    stats["phase1_resolved"] += 1
                else:
                    phase1_failed.append({
                        "artist": artist,
                        "title": title,
                        "reason": "waterfall_exhausted"
                    })
        
        LOG.info(
            "📊 [Batch Resolver] Phase 1 complete: %d resolved, %d failed",
            stats["phase1_resolved"], len(phase1_failed)
        )
        
        # Phase 2: Gemini batch fallback for remaining failures
        if phase1_failed and self._gemini_service:
            # Cap to max_gemini_batch tracks
            gemini_batch = phase1_failed[:max_gemini_batch]
            remaining_failed = phase1_failed[max_gemini_batch:]
            
            LOG.info(
                "🤖 [Batch Resolver] Phase 2: Sending %d tracks to Gemini",
                len(gemini_batch)
            )
            
            gemini_results = await self._gemini_batch_resolve(gemini_batch)
            
            async with DeezerClient(max_concurrent=10, timeout=10.0) as client:
                for item in gemini_results:
                    if item.get("queries"):
                        # Try each Gemini-suggested query
                        found = False
                        for query in item["queries"][:3]:  # Max 3 queries per track
                            results = await client.search_track(query, limit=5)
                            if results:
                                # Verify result matches
                                best = self._find_best_deezer_match(
                                    results,
                                    item["artist"],
                                    item["title"]
                                )
                                if best:
                                    resolved.append({
                                        "artist": best.artist,
                                        "title": best.title,
                                        "deezer_id": best.id,
                                        "preview_url": best.preview_url,
                                        "duration_ms": best.duration_ms,
                                        "original_artist": item["artist"],
                                        "original_title": item["title"],
                                        "phase": 2,
                                        "gemini_query": query,
                                    })
                                    stats["phase2_resolved"] += 1
                                    found = True
                                    break
                        
                        if not found:
                            remaining_failed.append({
                                "artist": item["artist"],
                                "title": item["title"],
                                "reason": "gemini_queries_failed"
                            })
                    else:
                        remaining_failed.append({
                            "artist": item["artist"],
                            "title": item["title"],
                            "reason": item.get("reason", "gemini_no_queries")
                        })
            
            phase1_failed = remaining_failed
        
        stats["total_failed"] = len(phase1_failed)
        
        LOG.info(
            "✅ [Batch Resolver] Complete: %d resolved (P1=%d, P2=%d), %d failed",
            len(resolved), stats["phase1_resolved"], stats["phase2_resolved"],
            stats["total_failed"]
        )
        
        return {
            "resolved": resolved,
            "failed": phase1_failed,
            "stats": stats,
        }
    
    async def _waterfall_deezer_search(
        self,
        client: Any,
        artist: str,
        title: str,
    ) -> Optional[Any]:
        """
        3-tier waterfall search for Deezer resolution.
        
        Tier 1: Exact search "{artist} {title}"
        Tier 2: Fuzzy search (title only, filter by artist)
        Tier 3: Artist top tracks search
        """
        # Tier 1: Exact search
        query = f"{artist} {title}"
        results = await client.search_track(query, limit=10)
        if results:
            best = self._find_best_deezer_match(results, artist, title)
            if best:
                return best
        
        # Tier 2: Fuzzy search - title only
        results = await client.search_track(title, limit=10)
        if results:
            best = self._find_best_deezer_match(results, artist, title, threshold=0.4)
            if best:
                return best
        
        # Tier 3: Artist top tracks
        try:
            artist_results = await client.search_artist(artist, limit=5)
            if artist_results:
                artist_id = artist_results[0].id if hasattr(artist_results[0], 'id') else artist_results[0].get('id')
                if artist_id:
                    top_tracks = await client.get_artist_top_tracks(str(artist_id), limit=50)
                    if top_tracks:
                        best = self._find_best_deezer_match(top_tracks, artist, title, threshold=0.5)
                        if best:
                            return best
        except Exception as e:
            LOG.debug("Tier 3 artist search failed: %s", str(e)[:50])
        
        return None
    
    def _find_best_deezer_match(
        self,
        results: List[Any],
        expected_artist: str,
        expected_title: str,
        threshold: float = 0.3,
    ) -> Optional[Any]:
        """
        Find best matching Deezer track using Jaccard similarity.
        """
        if not results:
            return None
        
        artist_lower = expected_artist.lower().strip()
        title_lower = expected_title.lower().strip()
        artist_words = set(artist_lower.split())
        title_words = set(title_lower.split())
        
        best_match = None
        best_score = 0.0
        
        for result in results:
            result_artist = getattr(result, 'artist', '').lower().strip()
            result_title = getattr(result, 'title', '').lower().strip()
            result_artist_words = set(result_artist.split())
            result_title_words = set(result_title.split())
            
            # Calculate Jaccard similarity for artist
            artist_intersection = len(artist_words & result_artist_words)
            artist_union = len(artist_words | result_artist_words)
            artist_score = artist_intersection / artist_union if artist_union > 0 else 0
            
            # Calculate Jaccard similarity for title
            title_intersection = len(title_words & result_title_words)
            title_union = len(title_words | result_title_words)
            title_score = title_intersection / title_union if title_union > 0 else 0
            
            # Boost if substring match
            if artist_lower in result_artist or result_artist in artist_lower:
                artist_score += 0.3
            if title_lower in result_title or result_title in title_lower:
                title_score += 0.3
            
            # Combined score (weight title higher for music)
            combined_score = (artist_score * 0.4) + (title_score * 0.6)
            
            if combined_score > best_score and combined_score >= threshold:
                best_score = combined_score
                best_match = result
        
        return best_match
    
    async def _gemini_batch_resolve(
        self,
        failed_tracks: List[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """
        Use Gemini to generate refined search queries for failed tracks.
        
        Sends up to 50 tracks in a single batch request with grounding.
        Returns list of dicts with 'artist', 'title', and 'queries' keys.
        """
        if not self._gemini_service or not self._gemini_service.is_available:
            return [{"artist": t["artist"], "title": t["title"], "reason": "gemini_unavailable"} 
                    for t in failed_tracks]
        
        # Format tracks for prompt
        tracks_text = "\n".join([
            f"{i+1}. {t['artist']} - {t['title']}"
            for i, t in enumerate(failed_tracks[:50])
        ])
        
        prompt = f"""These tracks failed to match on Deezer. Generate refined search queries for each.

FAILED TRACKS:
{tracks_text}

For EACH track, provide 3 alternative Deezer search queries using:
1. Canonical artist name (fix typos, expand abbreviations)
2. Clean title (remove suffixes like "Official Audio", "Lyric Video")
3. Album-based search if known

Common fixes:
- "ft." or "feat." → remove or use primary artist only
- Covers → use original artist
- OST → include franchise/game/anime name
- Remasters → search without "remastered"

Return JSON array:
[
  {{"index": 1, "artist": "original", "title": "original", "queries": ["query1", "query2", "query3"]}},
  ...
]

Only include tracks you can help with. If unsure, return empty queries array."""

        try:
            response = await self._gemini_service.query_gemini(
                prompt,
                allow_grounding=True,
                model="gemini-2.5-flash"
            )
            
            if not response:
                return [{"artist": t["artist"], "title": t["title"], "reason": "gemini_no_response"} 
                        for t in failed_tracks]
            
            # Parse JSON response
            text = response.strip()
            if "```" in text:
                text = re.sub(r"```(?:json)?", "", text).strip()
            
            try:
                results = json.loads(text)
                if isinstance(results, list):
                    # Map back to original tracks
                    result_map = {r.get("index", i+1): r for i, r in enumerate(results)}
                    
                    output = []
                    for i, track in enumerate(failed_tracks[:50]):
                        idx = i + 1
                        if idx in result_map:
                            item = result_map[idx]
                            output.append({
                                "artist": track["artist"],
                                "title": track["title"],
                                "queries": item.get("queries", []),
                            })
                        else:
                            output.append({
                                "artist": track["artist"],
                                "title": track["title"],
                                "queries": [],
                                "reason": "not_in_response"
                            })
                    return output
            except json.JSONDecodeError:
                LOG.warning("⚠️ [Batch Resolver] Failed to parse Gemini response as JSON")
            
        except Exception as e:
            LOG.error("❌ [Batch Resolver] Gemini batch request failed: %s", str(e)[:100])
        
        return [{"artist": t["artist"], "title": t["title"], "reason": "gemini_error"} 
                for t in failed_tracks]
    
    # =========================================================================
    # YOUTUBE > DEEZER WATERFALL PARSING (V3 Architecture)
    # =========================================================================
    # Per v3_reimplementation.md: If Deezer can't find it, it's probably not a song.
    # Deezer has ~129 million songs - if we can't find it, the track is likely
    # =========================================================================

    async def parse_youtube_to_deezer(
        self,
        youtube_title: str,
        youtube_id: str,
        channel_name: Optional[str] = None,
        *,
        verbose: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """
        Multi-stage resilient parsing: YouTube → Deezer with Gemini assistance.
        
        Per v3_reimplementation.md: If all stages fail, returns None (likely not a song).
        This signals to the caller that autoplay should be disabled for this content.
        
        Stage 0: Cache check with self-healing TTL
        Stage 1: Flash-Lite → 9 Deezer queries (85% confidence threshold)
        Stage 2: Flash + Grounding → 3 refined queries (75% threshold)
        Stage 3: Direct extraction + strict Deezer verification (70% threshold)
        
        Args:
            youtube_title: Raw YouTube video title
            youtube_id: YouTube video ID for cache key
            channel_name: YouTube channel name (optional artist fallback)
            verbose: Logging verbosity (0=off, 1=summary, 2=detailed)
            
        Returns:
            Dict with artist, title, confidence, track_type, is_canonical, deezer_id
            OR None if track cannot be verified (likely not commercial music)
        """
        from .deezer_fetch import DeezerClient
        
        if DeezerClient is None:
            LOG.warning("[Waterfall] Deezer client unavailable")
            return None
        
        def _log(level: int, msg: str, *args: Any) -> None:
            if verbose >= level:
                LOG.info(msg, *args)
        
        # Stage 0: Cache check with self-healing TTL
        cached = await self._cache.get_parsing(youtube_title, youtube_id)
        if cached:
            schema_version = getattr(cached, "schema_version", 1)
            if schema_version < PARSING_SCHEMA_VERSION:
                _log(2, "🔄 [Stage 0] Schema upgrade needed for '%s...'", youtube_title[:50])
                await self._cache.delete_parsing(youtube_title, youtube_id)
            else:
                cache_type = "CANONICAL" if getattr(cached, "is_canonical", False) else \
                            "BEST_GUESS" if getattr(cached, "is_best_guess", False) else "FALLBACK"
                
                should_refresh = False
                if cache_type == "CANONICAL":
                    should_refresh = False  # Never expires
                elif cache_type == "BEST_GUESS":
                    should_refresh = cached.is_expired(7 * 24 * 3600)  # 7 days
                else:
                    retry_days = getattr(cached, "retry_after_days", 1) or 1
                    should_refresh = cached.is_expired(retry_days * 24 * 3600)
                
                if not should_refresh:
                    _log(2, "📁 [Cache Hit] %s: %s - %s", cache_type, cached.artist, cached.title)
                    return {
                        "artist": cached.artist,
                        "title": cached.title,
                        "confidence": cached.confidence,
                        "track_type": getattr(cached, "track_type", "music"),
                        "primary_entity": getattr(cached, "primary_entity", None),
                        "is_canonical": getattr(cached, "is_canonical", False),
                        "is_best_guess": getattr(cached, "is_best_guess", False),
                        "deezer_id": getattr(cached, "deezer_id", None),
                    }
                else:
                    _log(2, "🔄 [Cache Expired] %s entry for '%s...'", cache_type, youtube_title[:50])
        
        _log(2, "🔍 [Stage 0] Starting waterfall for '%s...'", youtube_title[:60])
        
        if not self._gemini_service or not self._gemini_service.is_available:
            LOG.warning("[Waterfall] Gemini unavailable, cannot parse YouTube title")
            return None
        
        queries_response: Optional[Dict[str, Any]] = None
        grounded_response: Optional[Dict[str, Any]] = None
        
        async with DeezerClient() as deezer_client:
            # Stage 1: Flash-Lite → 9 Deezer queries (85% threshold)
            _log(2, "⚡ [Stage 1] Generating 9 Deezer queries...")
            
            try:
                queries_response = await self._gemini_service.generate_deezer_queries_lite(youtube_title)
                if queries_response and "queries" in queries_response:
                    queries = queries_response["queries"][:9]
                    _log(2, "⚡ [Stage 1] Queries: %s", queries)
                    
                    for idx, query in enumerate(queries, 1):
                        search_results = await deezer_client.search_track(query)
                        match = deezer_client.get_best_match(
                            search_results,
                            threshold=0.85,
                            expected_title=youtube_title,
                        )
                        if match:
                            _log(1, "✅ [Stage 1] Match: %s - %s (conf=%.2f)", 
                                 match.track.artist, match.track.title, match.confidence)
                            
                            entry = ParsingEntry(
                                artist=match.track.artist,
                                title=match.track.title,
                                confidence=match.confidence,
                                parsed_at=time.time(),
                                track_type="music",
                                is_canonical=True,
                                deezer_id=match.track.id,
                                schema_version=PARSING_SCHEMA_VERSION,
                            )
                            await self._cache.set_parsing(youtube_title, youtube_id, entry)
                            
                            return {
                                "artist": match.track.artist,
                                "title": match.track.title,
                                "confidence": match.confidence,
                                "track_type": "music",
                                "is_canonical": True,
                                "deezer_id": match.track.id,
                            }
                    
                    _log(2, "⚠️ [Stage 1] All 9 queries failed 85%% threshold")
            except Exception as exc:
                LOG.warning("⚠️ [Stage 1] Error: %s", exc)
            
            # Stage 2: Flash + Grounding → 3 refined queries (75% threshold)
            if self._gemini_service.can_use_grounding():
                _log(2, "🧠 [Stage 2] Generating 3 grounded queries...")
                
                try:
                    context = queries_response.get("queries", []) if queries_response else []
                    grounded_response = await self._gemini_service.generate_deezer_queries_grounded(
                        youtube_title, failed_queries=context[:3]
                    )
                    
                    if grounded_response and "queries" in grounded_response:
                        queries = grounded_response["queries"][:3]
                        _log(2, "🧠 [Stage 2] Queries: %s", queries)
                        
                        for idx, query in enumerate(queries, 1):
                            search_results = await deezer_client.search_track(query)
                            match = deezer_client.get_best_match(
                                search_results,
                                threshold=0.75,
                                expected_title=youtube_title,
                            )
                            if match:
                                _log(1, "✅ [Stage 2] Match: %s - %s (conf=%.2f)",
                                     match.track.artist, match.track.title, match.confidence)
                                
                                entry = ParsingEntry(
                                    artist=match.track.artist,
                                    title=match.track.title,
                                    confidence=match.confidence,
                                    parsed_at=time.time(),
                                    track_type="music",
                                    is_canonical=True,
                                    deezer_id=match.track.id,
                                    schema_version=PARSING_SCHEMA_VERSION,
                                )
                                await self._cache.set_parsing(youtube_title, youtube_id, entry)
                                
                                return {
                                    "artist": match.track.artist,
                                    "title": match.track.title,
                                    "confidence": match.confidence,
                                    "track_type": "music",
                                    "is_canonical": True,
                                    "deezer_id": match.track.id,
                                }
                        
                        _log(2, "⚠️ [Stage 2] All 3 queries failed 75%% threshold")
                except Exception as exc:
                    LOG.warning("⚠️ [Stage 2] Error: %s", exc)
            else:
                _log(2, "⏭️ [Stage 2] Skipped (grounding unavailable)")
            
            # Stage 3: Direct extraction + strict Deezer verification (70% threshold)
            if self._gemini_service.can_use_grounding():
                _log(2, "🎯 [Stage 3] Extracting metadata...")
                
                try:
                    failed_context = []
                    if queries_response and "queries" in queries_response:
                        failed_context.extend(queries_response["queries"][:9])
                    if grounded_response and "queries" in grounded_response:
                        failed_context.extend(grounded_response["queries"][:3])
                    
                    fallback_response = await self._gemini_service.generate_fallback_metadata(
                        youtube_title, failed_queries=failed_context[:5]
                    )
                    
                    if fallback_response:
                        artist = fallback_response.get("artist", "").strip()
                        title = fallback_response.get("title", "").strip()
                        track_type = fallback_response.get("track_type", "music")
                        primary_entity = fallback_response.get("primary_entity")
                        
                        if artist and title:
                            _log(2, "🎯 [Stage 3] Extracted: %s - %s, verifying...", artist, title)
                            
                            query = f'artist:"{artist}" track:"{title}"'
                            search_results = await deezer_client.search_track(query)
                            match = deezer_client.get_best_match(
                                search_results,
                                threshold=0.70,
                                expected_title=youtube_title,
                            )
                            
                            if match:
                                _log(1, "✅ [Stage 3] Verified: %s - %s (conf=%.2f)",
                                     match.track.artist, match.track.title, match.confidence)
                                
                                entry = ParsingEntry(
                                    artist=match.track.artist,
                                    title=match.track.title,
                                    confidence=match.confidence,
                                    parsed_at=time.time(),
                                    track_type=track_type,
                                    primary_entity=primary_entity,
                                    is_canonical=True,
                                    deezer_id=match.track.id,
                                    schema_version=PARSING_SCHEMA_VERSION,
                                )
                                await self._cache.set_parsing(youtube_title, youtube_id, entry)
                                
                                return {
                                    "artist": match.track.artist,
                                    "title": match.track.title,
                                    "confidence": match.confidence,
                                    "track_type": track_type,
                                    "primary_entity": primary_entity,
                                    "is_canonical": True,
                                    "deezer_id": match.track.id,
                                }
                            else:
                                _log(2, "❌ [Stage 3] Extracted '%s - %s' failed Deezer verification", artist, title)
                except Exception as exc:
                    LOG.warning("⚠️ [Stage 3] Error: %s", exc)
            else:
                _log(2, "⏭️ [Stage 3] Skipped (grounding unavailable)")
        
        # Total failure - per v3_reimplementation.md, this is likely NOT a song
        LOG.warning("❌ [Waterfall] Could not verify '%s...' on Deezer - likely not commercial music", youtube_title[:60])
        return None

    # =========================================================================
    # ARTIST/TITLE > YOUTUBE RESOLUTION (For Low Quality Deezer/Last.fm Matches)
    # =========================================================================
    # When we have artist + title from Deezer/Last.fm but need a YouTube video,
    # use Gemini to intelligently select the best match from YouTube search.
    # =========================================================================

    async def resolve_to_youtube_with_gemini(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
    ) -> Optional[Any]:
        """
        Resolve a Deezer/Last.fm track to YouTube using Gemini-assisted selection.
        
        This is used when standard heuristics produce low confidence results.
        Gemini analyzes YouTube search results and selects the best match.
        
        Args:
            artist: Track artist
            title: Track title
            expected_duration_ms: Expected track duration for validation
            
        Returns:
            Pomice track object or None
        """
        artist = artist.strip()
        title = title.strip()
        if not artist or not title:
            return None
        
        if not self._gemini_service:
            LOG.debug("[Gemini Resolution] Gemini unavailable, using heuristics")
            return await self.resolve_track(artist, title, expected_duration_ms=expected_duration_ms, prefer_cache=False)
        
        LOG.info("🤖 [Gemini Resolution] AI-assisted search for '%s' by '%s'", title, artist)
        
        async with self._gemini_resolution_semaphore:
            node = await self._get_node()
            if not node:
                return None
            
            search_query = f"{artist} {title}"
            try:
                search_results = await node.get_tracks(query=f"ytsearch:{search_query}", ctx=None)
            except Exception as e:
                LOG.error("Pomice search failed: %s", e)
                return None
            
            if not search_results:
                return None
            
            # Filter banned tracks
            ban_key = (artist.lower(), title.lower())
            banned_ids = {entry[0] for entry in self._banned_tracks.get(ban_key, [])} if ban_key in self._banned_tracks else set()
            
            # Build candidates for Gemini (top 10)
            candidates = []
            for idx, track in enumerate(search_results[:10]):
                metadata = self._extract_track_metadata(track)
                youtube_id = metadata.get("youtube_id", "")
                
                if youtube_id in banned_ids:
                    continue
                
                candidates.append({
                    "index": idx,
                    "video_title": metadata.get("title", ""),
                    "channel_name": metadata.get("channel_name", ""),
                    "duration_ms": metadata.get("duration_ms", 0),
                    "verified": metadata.get("verified", False),
                    "track_obj": track,
                })
            
            if not candidates:
                LOG.warning("[Gemini Resolution] No valid candidates after filtering")
                return None
            
            # Single Gemini call to select best match
            prompt = self._build_youtube_selection_prompt(artist, title, expected_duration_ms, candidates)
            
            try:
                response = await self._gemini_service.query_gemini(prompt)
                selected_index = self._parse_selection_response(response, len(candidates))
                
                if selected_index is None:
                    LOG.warning("[Gemini Resolution] Failed to parse response, using first candidate")
                    selected_index = 0
                
                selected = candidates[selected_index]
                track_obj = selected["track_obj"]
                metadata = self._extract_track_metadata(track_obj)
                
                LOG.info("✅ [Gemini Resolution] Selected: '%s' by '%s' (verified=%s)",
                         selected["video_title"][:60], selected["channel_name"], selected["verified"])
                
                # Cache the result with high confidence
                entry = MappingEntry(
                    youtube_id=str(metadata.get("youtube_id") or ""),
                    url=str(metadata.get("url") or ""),
                    timestamp=time.time(),
                    track_identifier=getattr(track_obj, "identifier", None),
                    title=str(metadata.get("title") or ""),
                    channel_name=metadata.get("channel_name"),
                    duration_ms=self._safe_int(metadata.get("duration_ms")),
                    verified=bool(metadata.get("verified", False)),
                    heuristic_score=0.95,  # High score for Gemini-selected
                    title_similarity=1.0,
                    artist_similarity=1.0,
                    search_rank=selected_index,
                    heuristic_version=3,
                )
                await self._cache.set_mapping(artist, title, entry)
                
                return track_obj
                
            except Exception as e:
                LOG.error("[Gemini Resolution] Error: %s", e, exc_info=True)
                # Fallback to heuristic
                return await self.resolve_track(artist, title, expected_duration_ms=expected_duration_ms, prefer_cache=False)
    
    def _build_youtube_selection_prompt(
        self,
        artist: str,
        title: str,
        expected_duration_ms: Optional[int],
        candidates: List[Dict[str, Any]],
    ) -> str:
        """Build prompt for Gemini to select best YouTube match."""
        duration_str = f"\nExpected duration: {expected_duration_ms / 1000:.0f}s" if expected_duration_ms else ""
        
        candidates_str = ""
        for cand in candidates:
            dur_sec = cand["duration_ms"] / 1000 if cand["duration_ms"] else 0
            verified = "✓" if cand["verified"] else ""
            candidates_str += f"\n{cand['index']}. \"{cand['video_title']}\" by \"{cand['channel_name']}\" {verified}| {dur_sec:.0f}s"
        
        return f"""Select the BEST YouTube video matching this track:

Artist: "{artist}"
Title: "{title}"{duration_str}

Videos:{candidates_str}

Rules:
1. Prefer exact artist + title match
2. Prefer verified channels (✓) and official uploads
3. Avoid covers, remixes, nightcore, slowed versions, live performances
4. Match duration if provided

Reply with ONLY the index number (0-{len(candidates)-1})."""
    
    def _parse_selection_response(self, response: str, max_index: int) -> Optional[int]:
        """Parse Gemini's index selection."""
        if not response:
            return None
        response = response.strip()
        
        try:
            idx = int(response)
            if 0 <= idx < max_index:
                return idx
        except ValueError:
            pass
        
        # Try to extract first number
        match = re.search(r"\b(\d+)\b", response)
        if match:
            idx = int(match.group(1))
            if 0 <= idx < max_index:
                return idx
        
        return None


__all__ = ["TrackResolver"]
