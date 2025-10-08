import os
import asyncio
import time
import logging
import json
import random
import re
from collections import Counter, deque
from datetime import datetime
from typing import Deque, Dict, Any, List, Optional, Tuple
from pathlib import Path

import aiohttp
from google import genai
from google.genai import types

LOG = logging.getLogger(__name__)


LOG_LEVEL = 2  # 0=ERROR, 1=INFO, 2=DEBUG (Higher number more verbose)
SESSION_HISTORY_LIMIT = 50
RECENT_TRACK_WINDOW_SECONDS = 90 * 60  # 90 minutes
FAILED_TRACK_COOLDOWN_SECONDS = 30 * 60  # 30 minutes
GROUNDING_DAILY_LIMIT = 500  # 500 requests per day
CACHE_FLUSH_INTERVAL_SECONDS = 120  # Flush caches at most every 2 minutes
METRICS_HISTORY_LIMIT = 200  # Retain recent recommendation metrics
ESCAPE_REASON_HISTORY_LIMIT = 16

# Recommendation tuning
CACHE_MAX_AGE = 60 * 60 * 24 * 15  # 15 days TTL for Last.fm->YouTube cache
METADATA_CACHE_MAX_AGE = 60 * 60 * 24 * 90  # 90 days TTL for metadata
DURATION_TOLERANCE_SECONDS = 5  # +/- 5 seconds tolerance
DURATION_TOLERANCE_PERCENT = 0.12  # +/- 12% duration tolerance
CANDIDATE_POOL_TARGET = 60  # Aim for ~60 candidates before filtering
ENRICH_TOP_N = 16  # How many top Last.fm tags to fetch and use for Gemini enrichment
RESOLVE_TOP_K = 6  # How many top YouTube search results to consider for matching
ARTIST_REPEAT_LIMIT = 3  # Max times the same artist can appear in session history
ARTIST_COOLDOWN_CYCLES = 3  # How many tracks before an artist can reappear
SKIP_THRESHOLD = 0.8  # Similarity threshold to skip a candidate
FINISH_THRESHOLD = 0.9  # Similarity threshold to finish a candidate
EPSILON_EXPLORE = 0.08  # Exploration factor for candidate selection

# Gemini configuration
GEMINI_MODEL = "gemini-2.5-flash-lite"


def _install_print_logger(logger: logging.Logger) -> None:
    """Override logger methods to honour LOG_LEVEL with stdout prints."""

    level_thresholds = {
        "debug": 2,
        "info": 1,
        "warning": 1,
        "error": 0,
        "critical": 0,
    }

    def _make(method_name: str):
        threshold = level_thresholds.get(method_name, 1)

        def _m(*args, **kwargs):
            if LOG_LEVEL < threshold:
                return
            try:
                msg = args[0] if args else ""
                print(f"[AUTO][{method_name.upper()}] {msg}")
            except Exception:
                pass

        return _m

    for _n in ("debug", "info", "warning", "error", "critical"):
        setattr(logger, _n, _make(_n))


_install_print_logger(LOG)

GOOD_TITLE_KEYWORDS = (
    # explicit "official" phrases first (phrase match)
    "official audio",
    "official video",
    "official mv",
    "official music video",
    "official visualizer",
    "official lyric video",
    "official lyric",
    "official",
    # quality markers / release signals
    "studio",
    "studio version",
    "album version",
    "single version",
    "original",
    "original mix",
    # Third party uploads, but still original content
    "vevo",
    "hq",
    "hd",
    "4k",
    "lossless",
    "remastered",  # sometimes official remaster is desired
    "explicit",  # indicates official release (may be part of title)
)

EMOJI_PATTERNS = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f1e0-\U0001f1ff"  # flags (iOS)
    "\U00002700-\U000027bf"  # dingbats
    "\U0001f926-\U0001f937"  # gestures
    "\U00010000-\U0010ffff"  # other unicode
    "\u2640-\u2642"  # gender symbols
    "\u2600-\u2b55"  # misc symbols
    "\u200d"  # zero width joiner
    "\u23cf"  # eject symbol
    "\u23e9"  # fast forward
    "\u231a"  # watch
    "\ufe0f"  # variation selector
    "\u3030"  # wavy dash
    "]+",
    flags=re.UNICODE,
)  # Soon to be implemented


BAD_TITLE_KEYWORDS = (
    # pitch/speed/time manipulations
    "nightcore",
    "night core",
    "slowed",
    "slowed + reverb",
    "slowed+reverb",
    "sped up",
    "speed up",
    "speedup",
    "pitch",
    # binaural / effect processing
    "8d",
    "8d audio",
    "3d audio",
    "binaural",
    "spatial audio",
    "ambisonic",
    "sound spatial",
    "stereo widened",
    # fan uploads / non-canonical
    "cover",
    "karaoke",
    "karaoke version",
    "karaoke instrumental",
    "instrumental",
    "backing track",
    "minus one",
    "tutorial",
    "lesson",
    "practice",
    # remix / edits / unofficial versions
    "remix",
    "remixed",
    "edit",
    "rework",
    "bootleg",
    "mashup",
    "reimagined",
    "redux",
    "acapella",
    "a cappella",
    "instrumental",
    "midi",
    "black midi",
    "audio spectrum",
    "remake",
    "music project",
    "fan edit",
    "fanmix",
    "fan mix",
    "audio",
    "covered by",
    # performance / live
    "live",
    "live at",
    "live from",
    "session",
    "concert",
    "performance",
    "tour",
    # long-play / loop / compilation
    "hour",
    "hours",
    "loop",
    "mix",
    "mixes",
    "dj set",
    "set",
    # low-quality / user-added modifiers
    "lyric",
    "lyrics",
    "lyric video",
    "visualizer",  # sometimes official but often not; treat lower priority
    "reupload",
    "fanmade",
    # languages / non-english karaoke markers
    "伴唱",  # chinese karaoke
    "カラオケ",  # japanese karaoke
    "노래방",  # korean karaoke
    # Teaser Music
    "teaser",
    "trailer",
    "preview",
    "snippet",
    "sample",
    "promo",
    "promotional",
    "promotion",
    "promos",
    # Music Mix (Multiple Music from the same artist in one video)
    "music mix",
    "top hits",
    "best of",
    "2018",
    "2019",
    "2020",
    "2021",
    "2022",
    "2023",
    "2024",
    "2025",
    "2016",
    "2017",
    "amazing",
    "greatest",
    "hits",
    "collection",
    "compilation",
    "playlist",
    # Subscriber Specials & Spam Entries
    "subscriber special",
    "plz",
    "subscriber special mix",
    "subscriber special edition",
    "hz",
    # Studio Remixes (First-party remix not suggested)
    "Studio Vocals",
)

# Last.fm API Configuration
LASTFM_API_BASE = "http://ws.audioscrobbler.com/2.0/"
LASTFM_API_KEY_ENV = "LASTFM_API_KEY"

# Gemini API Configuration
GEMINI_API_KEYS_ENV = "GeminiApiKeys"


class LastFMAutoplay:
    """Manages Last.fm-based autoplay recommendations with Gemini AI parsing."""

    def __init__(self, bot):
        self.bot = bot
        self.api_key: Optional[str] = None
        self._cache_dir = Path("cache/music")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "lastfm_mappings.json"
        self._cache: Dict[str, Tuple[str, float]] = {}
        self._recent_autoplayed: Dict[int, List[Dict[str, Any]]] = (
            {}
        )  # In-memory only, per-session
        self._artist_cooldowns: Dict[int, Dict[str, int]] = (
            {}
        )  # Guild -> Artist -> Cooldown counter
        self._genre_history: Dict[int, List[str]] = (
            {}
        )  # Guild -> List of recent genres (for diversity)
        self._metadata_cache: Dict[str, Dict[str, Any]] = {}
        self._metadata_cache_file = self._cache_dir / "track_metadata.json"
        self._metadata_lock = asyncio.Lock()
        self._initialized = False
        self._gemini_client: Optional[genai.Client] = None
        self._gemini_search_tool: Optional[types.Tool] = None
        self._gemini_base_config: Optional[types.GenerateContentConfig] = None
        self._gemini_available = False
        self._gemini_api_keys: List[str] = []
        self._gemini_key_index: int = 0
        self._active_gemini_key: Optional[str] = None
        self._gemini_key_cooldowns: Dict[str, float] = {}
        self._session_history: Dict[int, Deque[Dict[str, Any]]] = {}
        self._recent_track_set: Dict[int, Dict[str, float]] = {}
        self._failed_resolve_map: Dict[int, Dict[str, float]] = {}
        self._grounding_state: Dict[str, Any] = {
            "date": self._grounding_date_key(),
            "count": 0,
        }
        self._rng = random.Random()
        self._last_metadata_flush = 0.0
        self._last_mapping_flush = 0.0
        self._metrics: Dict[int, Dict[str, Any]] = {}
        self._metrics_global: Dict[str, Any] = {
            "date": self._grounding_date_key(),
            "grounding_calls_today": 0,
        }
        self._gemini_retry_after = 0.0

        # Initialize Last.fm API
        self._init_lastfm_client()
        self._init_gemini()
        self._load_cache()
        self._load_metadata_cache()

    def _init_lastfm_client(self):
        """Initialize Last.fm API with API key."""
        self.api_key = os.getenv(LASTFM_API_KEY_ENV)

        if not self.api_key:
            LOG.warning(
                f"{LASTFM_API_KEY_ENV} not found in .env. "
                "Get a free API key from https://www.last.fm/api/account/create"
            )
            LOG.warning("Last.fm autoplay will NOT be available")
            return

        LOG.info("✅ Last.fm autoplay initialized successfully!")
        self._initialized = True

    def _init_gemini(self):
        """Initialize Gemini AI for intelligent track parsing."""
        try:
            keys = self._load_gemini_keys()
            if not keys:
                LOG.warning(
                    "⚠️ [Last.fm AutoPlay] No Gemini API keys configured. "
                    "Gemini parsing disabled; falling back to heuristic parsing."
                )
                LOG.warning('Set GeminiApiKeys=["KEY1",...] in .env to enable Gemini.')
                self._disable_gemini()
                return

            self._gemini_api_keys = keys
            self._gemini_key_cooldowns = {key: 0.0 for key in keys}

            for idx in range(len(keys)):
                if self._activate_gemini_key(idx):
                    if len(keys) > 1:
                        LOG.info(
                            f"✅ [Last.fm AutoPlay] Gemini initialized with {len(keys)} API keys."
                        )
                    else:
                        LOG.info(
                            "✅ [Last.fm AutoPlay] Gemini initialized successfully!"
                        )
                    return

            LOG.error(
                "❌ [Last.fm AutoPlay] Failed to initialize Gemini with any provided API key."
            )
            self._disable_gemini()
        except Exception as e:
            LOG.error(
                f"❌ [Last.fm AutoPlay] Failed to initialize Gemini: {e}. Heuristic parsing will be used instead."
            )
            self._disable_gemini()

    def _disable_gemini(self) -> None:
        self._gemini_client = None
        self._gemini_search_tool = None
        self._gemini_base_config = None
        self._gemini_available = False
        self._active_gemini_key = None
        self._gemini_api_keys = []
        self._gemini_key_cooldowns = {}

    def _load_gemini_keys(self) -> List[str]:
        raw_keys = os.getenv(GEMINI_API_KEYS_ENV)
        keys: List[str] = []
        if raw_keys:
            parsed: Any = None
            try:
                parsed = json.loads(raw_keys)
            except json.JSONDecodeError:
                parsed = None

            if isinstance(parsed, list):
                keys = [str(k).strip() for k in parsed if str(k).strip()]
            elif isinstance(parsed, str):
                keys = [parsed.strip()]
            else:
                keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

        # Deduplicate while preserving order
        seen = set()
        unique_keys = []
        for key in keys:
            if key and key not in seen:
                unique_keys.append(key)
                seen.add(key)
        return unique_keys

    def _activate_gemini_key(self, index: int) -> bool:
        if not self._gemini_api_keys:
            return False
        index = index % len(self._gemini_api_keys)
        candidate_key = self._gemini_api_keys[index]

        try:
            self._gemini_client = genai.Client(api_key=candidate_key)
            self._gemini_search_tool = types.Tool(google_search=types.GoogleSearch())
            self._gemini_base_config = types.GenerateContentConfig(
                tools=[self._gemini_search_tool],
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            )
            self._gemini_key_index = index
            self._active_gemini_key = candidate_key
            self._gemini_key_cooldowns[candidate_key] = 0.0
            self._gemini_available = True
            return True
        except Exception as exc:
            LOG.error(
                f"❌ [Last.fm AutoPlay] Gemini API key #{index + 1} failed to initialize: {exc}"
            )
            self._gemini_key_cooldowns[candidate_key] = time.time() + 600
            return False

    def _ensure_gemini_ready(self) -> bool:
        if not self._gemini_api_keys:
            return False

        now = time.time()

        active_key = self._active_gemini_key
        if active_key:
            if now >= self._gemini_key_cooldowns.get(active_key, 0.0):
                if self._gemini_client is not None:
                    self._gemini_available = True
                    self._gemini_retry_after = 0.0
                    return True

        total_keys = len(self._gemini_api_keys)
        for offset in range(total_keys):
            idx = (self._gemini_key_index + offset) % total_keys
            key = self._gemini_api_keys[idx]
            cooldown_until = self._gemini_key_cooldowns.get(key, 0.0)
            if now < cooldown_until:
                continue
            if idx == self._gemini_key_index and self._gemini_client is not None:
                self._active_gemini_key = key
                self._gemini_available = True
                self._gemini_retry_after = 0.0
                return True
            if self._activate_gemini_key(idx):
                if offset != 0:
                    LOG.info(
                        f"✅ [Last.fm AutoPlay] Switched to Gemini API key #{idx + 1}."
                    )
                self._gemini_retry_after = 0.0
                return True

        if self._gemini_key_cooldowns:
            soonest = min(self._gemini_key_cooldowns.values())
            if soonest > now:
                self._gemini_retry_after = max(self._gemini_retry_after, soonest)

        self._active_gemini_key = None
        self._gemini_available = False
        return False

    @staticmethod
    def _grounding_date_key() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def _reset_grounding_counter_if_needed(self) -> None:
        current = self._grounding_date_key()
        if self._grounding_state.get("date") != current:
            self._grounding_state["date"] = current
            self._grounding_state["count"] = 0
            self._metrics_global["date"] = current
            self._metrics_global["grounding_calls_today"] = 0

    def _can_use_grounded_call(self) -> bool:
        self._reset_grounding_counter_if_needed()
        return self._grounding_state.get("count", 0) < GROUNDING_DAILY_LIMIT

    def _register_grounded_call(self) -> None:
        self._reset_grounding_counter_if_needed()
        self._grounding_state["count"] = self._grounding_state.get("count", 0) + 1
        self._record_grounding_metrics()

    def _maybe_flush_metadata_cache(self, force: bool = False) -> None:
        now = time.time()
        if force or (now - self._last_metadata_flush) >= CACHE_FLUSH_INTERVAL_SECONDS:
            self._save_metadata_cache()
            self._last_metadata_flush = now

    def _maybe_flush_mapping_cache(self, force: bool = False) -> None:
        now = time.time()
        if force or (now - self._last_mapping_flush) >= CACHE_FLUSH_INTERVAL_SECONDS:
            self._save_cache()
            self._last_mapping_flush = now

    def _metrics_bucket(self, guild_id: int) -> Dict[str, Any]:
        bucket = self._metrics.setdefault(
            guild_id,
            {
                "recommendations": deque(maxlen=METRICS_HISTORY_LIMIT),
                "failed_resolves": 0,
                "escape_hatch_count": 0,
                "escape_hatch_reasons": deque(maxlen=ESCAPE_REASON_HISTORY_LIMIT),
                "last_updated": time.time(),
            },
        )
        bucket["last_updated"] = time.time()
        return bucket

    def _record_recommendation_metric(
        self, guild_id: int, candidate: Dict[str, Any]
    ) -> None:
        bucket = self._metrics_bucket(guild_id)
        bucket["recommendations"].append(
            {
                "timestamp": time.time(),
                "artist": candidate.get("artist"),
                "track": candidate.get("track"),
                "source": candidate.get("source"),
                "quality_score": candidate.get("quality_score"),
                "score": candidate.get("score"),
            }
        )

    def _record_escape_hatch_metric(self, guild_id: int, reason: str) -> None:
        bucket = self._metrics_bucket(guild_id)
        bucket["escape_hatch_count"] += 1
        bucket["escape_hatch_reasons"].append(
            {"timestamp": time.time(), "reason": reason}
        )

    def _increment_failed_resolve_metric(self, guild_id: int) -> None:
        bucket = self._metrics_bucket(guild_id)
        bucket["failed_resolves"] += 1

    def _record_grounding_metrics(self) -> None:
        today = self._grounding_date_key()
        if self._metrics_global.get("date") != today:
            self._metrics_global["date"] = today
            self._metrics_global["grounding_calls_today"] = 0
        self._metrics_global["grounding_calls_today"] = self._grounding_state.get(
            "count", 0
        )

    def _enter_gemini_cooldown(
        self, retry_seconds: float, reason: str, key: Optional[str] = None
    ) -> None:
        cooldown = max(retry_seconds, 1.0)
        target = time.time() + cooldown
        key = key or self._active_gemini_key

        if key:
            self._gemini_key_cooldowns[key] = target

        if target > self._gemini_retry_after:
            self._gemini_retry_after = target

        LOG.warning(
            f"⚠️ [Gemini] Metadata enrichment paused for {cooldown:.1f}s ({reason})."
        )

        if not self._ensure_gemini_ready():
            self._gemini_available = False

    @staticmethod
    def _is_rate_limited_message(message: str) -> bool:
        lower = message.lower()
        return "429" in lower or "quota" in lower or "resource_exhausted" in lower

    @staticmethod
    def _extract_retry_delay_seconds_from_text(message: str) -> Optional[float]:
        lowered = message.lower()
        match = re.search(r"retry(?: in)?\s*(\d+(?:\.\d+)?)s", lowered)
        if match:
            return float(match.group(1))
        match = re.search(r"retrydelay['\"]:\s*['\"]?(\d+)(?:s)?", lowered)
        if match:
            return float(match.group(1))
        return None

    @staticmethod
    def _strip_code_fence(payload: str) -> str:
        """Remove Markdown code fences from Gemini responses."""

        if not isinstance(payload, str):
            return ""

        stripped = payload.strip()
        fence_match = re.match(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()
        return stripped

    @staticmethod
    def _extract_json_dict(text: str) -> Optional[Dict[str, Any]]:
        """Try to parse a JSON object from a Gemini text fragment."""

        if not isinstance(text, str):
            return None

        cleaned = LastFMAutoplay._strip_code_fence(text)
        if not cleaned:
            return None

        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = cleaned[start : end + 1]
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                return None
        return None

    def _collect_gemini_texts(self, response: Any) -> List[str]:
        """Gather all textual payloads from a Gemini response."""

        texts: List[str] = []
        if response is None:
            return texts

        primary_text = getattr(response, "text", None)
        if isinstance(primary_text, str) and primary_text.strip():
            texts.append(primary_text)

        parsed_payload = getattr(response, "parsed", None)
        if isinstance(parsed_payload, dict):
            try:
                texts.append(json.dumps(parsed_payload))
            except (TypeError, ValueError):
                pass
        elif isinstance(parsed_payload, list):
            for item in parsed_payload:
                if isinstance(item, (dict, list)):
                    try:
                        texts.append(json.dumps(item))
                    except (TypeError, ValueError):
                        continue

        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str) and part_text.strip():
                    texts.append(part_text)

        return texts

    def _parse_gemini_json(self, response: Any) -> Optional[Dict[str, Any]]:
        """Extract the first valid JSON object from a Gemini response."""

        for candidate_text in self._collect_gemini_texts(response):
            data = self._extract_json_dict(candidate_text)
            if isinstance(data, dict):
                return data
        return None

    def _load_cache(self):
        """Load Last.fm->YouTube mapping cache from disk."""
        if not self._cache_file.exists():
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            return

        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for track_key, entry in data.items():
                    if isinstance(entry, dict):
                        self._cache[track_key] = (
                            entry.get("playable", ""),
                            entry.get("timestamp", 0),
                        )
                self._last_mapping_flush = time.time()
        except Exception as e:
            LOG.warning(f"Failed to load Last.fm cache: {e}")

    def _save_cache(self):
        """Save Last.fm->YouTube mapping cache to disk."""
        try:
            data = {
                track_key: {"playable": playable, "timestamp": timestamp}
                for track_key, (playable, timestamp) in self._cache.items()
            }

            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self._last_mapping_flush = time.time()

        except Exception as e:
            LOG.warning(f"Failed to save Last.fm cache: {e}")

    def _load_metadata_cache(self) -> None:
        """Load cached enriched track metadata (Gemini + Last.fm)."""
        if not self._metadata_cache_file.exists():
            return

        try:
            with open(self._metadata_cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._metadata_cache = data
                    self._last_metadata_flush = time.time()
        except Exception as e:
            LOG.warning(f"Failed to load track metadata cache: {e}")

    def _save_metadata_cache(self) -> None:
        """Persist enriched track metadata to disk."""
        try:
            with open(self._metadata_cache_file, "w", encoding="utf-8") as f:
                json.dump(self._metadata_cache, f, indent=2)
            self._last_metadata_flush = time.time()
        except Exception as e:
            LOG.warning(f"Failed to save track metadata cache: {e}")

    @staticmethod
    def _make_track_key(artist: str, track: str) -> str:
        return f"{artist.lower().strip()}|||{track.lower().strip()}"

    @staticmethod
    def _normalize_tag(tag: str) -> str:
        return tag.strip().lower()

    def _should_refresh_metadata(self, entry: Dict[str, Any]) -> bool:
        timestamp = entry.get("timestamp", 0)
        return (time.time() - timestamp) > METADATA_CACHE_MAX_AGE

    async def _fetch_track_tags_from_lastfm(self, artist: str, track: str) -> List[str]:
        tags: List[str] = []
        async with aiohttp.ClientSession() as session:
            data = await self._lastfm_get(
                {"method": "track.getTopTags", "artist": artist, "track": track},
                session,
            )
            if data:
                tag_list = data.get("toptags", {}).get("tag", [])
                if isinstance(tag_list, dict):
                    tag_list = [tag_list]
                for tag_obj in tag_list[:10]:
                    tag_name = tag_obj.get("name")
                    if tag_name:
                        tags.append(self._normalize_tag(tag_name))
        return tags

    async def _classify_track_with_gemini(
        self,
        artist: str,
        track: str,
        existing_tags: List[str],
        allow_grounding: bool = False,
    ) -> Dict[str, Any]:
        """Use Gemini with Google Search grounding to enrich track metadata."""
        if not self._ensure_gemini_ready():
            LOG.debug(
                "⚠️ [Gemini] No available API keys for metadata enrichment; skipping."
            )
            return {}

        if not self._gemini_available or not self._gemini_client:
            return {}

        instructions = (
            "You are a music metadata enrichment assistant. Given an artist and track, "
            "return concise JSON with keys: tags (list of canonical genres), moods "
            "(list of 1-3 mood descriptors), and energy (one word describing energy level). "
            "Normalize genres to standard Spotify-like labels. Use Google Search grounding "
            "to verify ambiguous cases."
        )
        prompt = (
            "Artist: {artist}\n"
            "Track: {track}\n"
            "Existing tags: {tags}\n"
            "Respond with JSON only."
        ).format(artist=artist, track=track, tags=existing_tags or "[]")

        use_grounding = allow_grounding and self._can_use_grounded_call()

        error_state: Dict[str, Any] = {
            "message": "",
            "retry": None,
            "rate_limited": False,
        }

        def _call_gemini() -> Optional[Any]:
            try:
                config = types.GenerateContentConfig(
                    system_instruction=instructions,
                    tools=(
                        [self._gemini_search_tool]
                        if use_grounding and self._gemini_search_tool
                        else None
                    ),
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                )
                return self._gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=config,
                )
            except Exception as exc:  # Rate limits or network errors
                message = str(exc)
                error_state["message"] = message
                error_state["rate_limited"] = self._is_rate_limited_message(message)
                retry_attr = getattr(exc, "retry_delay", None) or getattr(
                    exc, "retry_after", None
                )
                if retry_attr is not None:
                    try:
                        error_state["retry"] = float(retry_attr.total_seconds())
                    except AttributeError:
                        try:
                            error_state["retry"] = float(retry_attr)
                        except (TypeError, ValueError):
                            error_state["retry"] = None
                if error_state["retry"] is None:
                    error_state["retry"] = self._extract_retry_delay_seconds_from_text(
                        message
                    )
                LOG.debug(f"⚠️ [Gemini] Metadata enrichment failed: {message}")
                return None

        response = await asyncio.to_thread(_call_gemini)
        if response is None:
            if error_state["message"]:
                default_retry = 60.0 if error_state["rate_limited"] else 20.0
                retry_seconds = error_state.get("retry") or default_retry
                reason = (
                    "rate-limit"
                    if error_state["rate_limited"]
                    else "metadata enrichment failure"
                )
                self._enter_gemini_cooldown(
                    retry_seconds, reason, key=self._active_gemini_key
                )
            return {}

        response_error = getattr(response, "error", None)
        if not response_error and isinstance(response, dict):
            response_error = response.get("error")
        if response_error:
            error_message = (
                response_error.get("message")
                if isinstance(response_error, dict)
                else str(response_error)
            )
            retry_seconds = (
                self._extract_retry_delay_seconds_from_text(error_message) or 60.0
            )
            reason = (
                "rate-limit"
                if self._is_rate_limited_message(error_message)
                else "metadata enrichment failure"
            )
            self._enter_gemini_cooldown(
                retry_seconds, reason, key=self._active_gemini_key
            )
            return {}

        if use_grounding:
            self._register_grounded_call()

        structured = self._parse_gemini_json(response)
        if structured:
            return structured

        texts = self._collect_gemini_texts(response)
        if texts:
            preview = self._strip_code_fence(texts[0])[:200]
            LOG.debug(f"⚠️ [Gemini] Failed to parse JSON response. Raw: {preview}")
        return {}

    async def _get_track_profile(
        self, artist: str, track: str, require_enrichment: bool = False
    ) -> Dict[str, Any]:
        """Return enriched track metadata, combining Last.fm and Gemini."""

        metadata_key = self._make_track_key(artist, track)

        async with self._metadata_lock:
            cache_entry = self._metadata_cache.get(metadata_key, {})

            if cache_entry and not self._should_refresh_metadata(cache_entry):
                return cache_entry

        # Fetch Last.fm tags first (primary source)
        tags = await self._fetch_track_tags_from_lastfm(artist, track)

        metadata: Dict[str, Any] = {
            "artist": artist,
            "track": track,
            "tags": tags,
            "moods": [],
            "energy": None,
            "sources": ["lastfm"] if tags else [],
            "timestamp": time.time(),
        }

        need_gemini = require_enrichment or len(tags) < 3

        if need_gemini:
            enriched = await self._classify_track_with_gemini(
                artist,
                track,
                tags,
                allow_grounding=require_enrichment,
            )
            if enriched:
                gemini_tags = [
                    self._normalize_tag(tag)
                    for tag in enriched.get("tags", [])
                    if isinstance(tag, str)
                ]
                # Merge tags prioritizing Last.fm first, then Gemini unique ones
                merged_tags = (
                    list(dict.fromkeys(tags + gemini_tags)) if tags else gemini_tags
                )
                if merged_tags:
                    metadata["tags"] = merged_tags
                metadata["moods"] = [
                    mood.strip()
                    for mood in enriched.get("moods", [])
                    if isinstance(mood, str)
                ][:5]
                energy = enriched.get("energy")
                if isinstance(energy, str):
                    metadata["energy"] = energy.strip()
                metadata.setdefault("sources", []).append("gemini")

        async with self._metadata_lock:
            metadata["timestamp"] = time.time()
            self._metadata_cache[metadata_key] = metadata
            self._maybe_flush_metadata_cache()

        return metadata

    def _load_guild_history(self, guild_id: int) -> None:
        """Load recommendation history for a specific guild from disk."""
        history_file = self._cache_dir / f"{guild_id}_history.json"

        if not history_file.exists():
            return

        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_history = data.get("history", []) if isinstance(data, dict) else []

                cleaned: List[Dict[str, Any]] = []
                history_buffer: Deque[Dict[str, Any]] = deque(
                    maxlen=SESSION_HISTORY_LIMIT
                )
                now_ts = time.time()
                recent_tracks: Dict[str, float] = {}

                for entry in raw_history:
                    if not isinstance(entry, dict):
                        continue
                    artist = entry.get("artist", "")
                    track = entry.get("track", "")
                    if not artist or not track:
                        continue

                    timestamp = float(entry.get("timestamp", now_ts))
                    resolved_source = entry.get("resolved_source")
                    quality_score = entry.get("quality_score")

                    record = {
                        "artist": artist,
                        "track": track,
                        "timestamp": timestamp,
                        "resolved_source": resolved_source,
                        "quality_score": quality_score,
                    }
                    cleaned.append(record)
                    history_buffer.append(record)

                    track_key = self._make_track_key(artist, track)
                    if timestamp and (now_ts - timestamp) < RECENT_TRACK_WINDOW_SECONDS:
                        recent_tracks[track_key] = timestamp

                self._recent_autoplayed[guild_id] = cleaned
                self._session_history[guild_id] = history_buffer
                if recent_tracks:
                    self._recent_track_set[guild_id] = recent_tracks
                else:
                    self._recent_track_set.setdefault(guild_id, {})
                self._failed_resolve_map.setdefault(guild_id, {})
        except Exception as e:
            LOG.warning(f"Failed to load history for guild {guild_id}: {e}")

    def _save_guild_history(self, guild_id: int) -> None:
        """Save recommendation history for a specific guild to disk."""
        if guild_id not in self._recent_autoplayed:
            return

        history_file = self._cache_dir / f"{guild_id}_history.json"

        try:
            history_records = list(self._session_history.get(guild_id, []))
            if not history_records:
                history_records = self._recent_autoplayed[guild_id][
                    -SESSION_HISTORY_LIMIT:
                ]

            self._recent_autoplayed[guild_id] = history_records

            data = {
                "guild_id": guild_id,
                "history": history_records,
                "last_updated": time.time(),
                "grounding_count": self._grounding_state.get("count", 0),
            }

            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            LOG.warning(f"Failed to save history for guild {guild_id}: {e}")

    def is_available(self) -> bool:
        """Check if Last.fm autoplay is available."""
        return self._initialized and self.api_key is not None

    def _normalize_artist_for_diversity(self, artist: str) -> str:
        """Normalize artist name for diversity scoring to catch variations."""
        normalized = artist.lower().strip()
        for sep in [", ", " & ", " ft. ", " feat. ", " featuring ", " x "]:
            if sep in normalized:
                normalized = normalized.split(sep)[0].strip()
                break
        return normalized

    def clear_history(self, guild_id: Optional[int] = None) -> None:
        """
        Clear recommendation history (session end).

        Args:
            guild_id: If provided, clear history for this guild only.
                      If None, clear all history (full reset).
        """
        if guild_id is not None:
            # Remove from memory
            if guild_id in self._recent_autoplayed:
                self._recent_autoplayed.pop(guild_id)
            if guild_id in self._artist_cooldowns:
                self._artist_cooldowns.pop(guild_id)
            if guild_id in self._genre_history:
                self._genre_history.pop(guild_id)
            if guild_id in self._session_history:
                self._session_history.pop(guild_id)
            if guild_id in self._recent_track_set:
                self._recent_track_set.pop(guild_id)
            if guild_id in self._failed_resolve_map:
                self._failed_resolve_map.pop(guild_id)

            # Delete the file
            history_file = self._cache_dir / f"{guild_id}_history.json"
            if history_file.exists():
                try:
                    history_file.unlink()
                    LOG.info(
                        f"🗑️ [Last.fm] Cleared session history for guild {guild_id}"
                    )
                except Exception as e:
                    LOG.warning(
                        f"Failed to delete history file for guild {guild_id}: {e}"
                    )
        else:
            # Clear all guilds
            self._recent_autoplayed.clear()
            self._artist_cooldowns.clear()
            self._genre_history.clear()
            self._session_history.clear()
            self._recent_track_set.clear()
            self._failed_resolve_map.clear()
            self._grounding_state = {"date": self._grounding_date_key(), "count": 0}

            # Delete all history files
            try:
                for history_file in self._cache_dir.glob("*_history.json"):
                    history_file.unlink()
                LOG.info("🗑️ [Last.fm] Cleared all session histories")
            except Exception as e:
                LOG.warning(f"Failed to delete all history files: {e}")

    @staticmethod
    def _sanitize_artist_name(value: str) -> str:
        cleaned = value or ""
        cleaned = re.sub(r"(?i)\b(feat|ft|featuring|x)\b.*", "", cleaned)
        cleaned = re.sub(r"[|•/]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        primary_parts = re.split(r"\s*(?:,|;| with )\s*", cleaned)
        if primary_parts:
            cleaned = primary_parts[0]
        return cleaned.strip(" -|•")

    @staticmethod
    def _sanitize_title_text(value: str) -> str:
        cleaned = value or ""
        cleaned = re.sub(r"\[[^\]]*\]", " ", cleaned)
        cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
        cleaned = re.sub(
            r"(?i)\b(official(?:\s+(music|lyric|audio|video))?|official|lyrics?|audio|video|mv|visualizer|hd|4k|8k|live|performance|karaoke|teaser|trailer)\b",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"[|•/]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip(" -|•")

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, value))

    def _estimate_quality_score(self, title: str) -> float:
        if not title:
            return 0.5
        lower = title.lower()
        penalty = sum(1 for kw in BAD_TITLE_KEYWORDS if kw in lower)
        bonus = sum(1 for kw in GOOD_TITLE_KEYWORDS if kw in lower)
        base = 0.65 + 0.08 * bonus - 0.12 * penalty
        return self._clamp01(base)

    def _track_recently_played(self, guild_id: int, artist: str, track: str) -> bool:
        track_key = self._make_track_key(artist, track)
        now_ts = time.time()
        recent_map = self._recent_track_set.setdefault(guild_id, {})
        timestamp = recent_map.get(track_key)
        if timestamp and (now_ts - timestamp) < RECENT_TRACK_WINDOW_SECONDS:
            return True
        if timestamp and (now_ts - timestamp) >= RECENT_TRACK_WINDOW_SECONDS:
            recent_map.pop(track_key, None)
        failed_map = self._failed_resolve_map.setdefault(guild_id, {})
        failed_ts = failed_map.get(track_key)
        if failed_ts and (now_ts - failed_ts) < FAILED_TRACK_COOLDOWN_SECONDS:
            return True
        if failed_ts and (now_ts - failed_ts) >= FAILED_TRACK_COOLDOWN_SECONDS:
            failed_map.pop(track_key, None)
        return False

    def _mark_track_played(
        self,
        guild_id: int,
        artist: str,
        track: str,
        resolved_source: Optional[str] = None,
        quality_score: Optional[float] = None,
    ) -> None:
        entry = {
            "artist": artist,
            "track": track,
            "timestamp": time.time(),
            "resolved_source": resolved_source,
            "quality_score": quality_score,
        }
        history_buffer = self._session_history.setdefault(
            guild_id, deque(maxlen=SESSION_HISTORY_LIMIT)
        )
        history_buffer.append(entry)
        self._recent_autoplayed[guild_id] = list(history_buffer)
        track_key = self._make_track_key(artist, track)
        self._recent_track_set.setdefault(guild_id, {})[track_key] = entry["timestamp"]

    def _mark_resolve_failure(self, guild_id: int, artist: str, track: str) -> None:
        track_key = self._make_track_key(artist, track)
        self._failed_resolve_map.setdefault(guild_id, {})[track_key] = time.time()
        self._increment_failed_resolve_metric(guild_id)

    def _recent_artist_counts(self, guild_id: int) -> Counter:
        buffer = self._session_history.get(guild_id)
        if not buffer:
            return Counter()
        normalized = [
            self._normalize_artist_for_diversity(entry.get("artist", ""))
            for entry in buffer
        ]
        return Counter(a for a in normalized if a)

    async def _trigger_escape_hatch(
        self,
        guild_id: int,
        reason: str,
        seed_artist: str,
        seed_tags: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        self._record_escape_hatch_metric(guild_id, reason)
        LOG.warning(f"Escape hatch triggered: {reason}")
        candidates = await self._get_fallback_recommendations(seed_artist, limit=20)
        if candidates:
            return candidates
        tags = seed_tags or []
        if tags:
            for tag in tags[:3]:
                tag_tracks = await self._get_tag_top_tracks(tag, limit=10)
                if tag_tracks:
                    return tag_tracks
        default_tag = "pop"
        return await self._get_tag_top_tracks(default_tag, limit=10)

    def _make_candidate_entry(
        self,
        guild_id: int,
        artist: str,
        track: str,
        source: str,
    ) -> Optional[Dict[str, Any]]:
        if not artist or not track:
            return None
        key = self._make_track_key(artist, track)
        if self._track_recently_played(guild_id, artist, track):
            return None
        return {
            "artist": artist,
            "track": track,
            "source": source,
            "quality_score": self._estimate_quality_score(track),
            "track_key": key,
        }

    async def _build_candidate_pool(
        self,
        guild_id: int,
        artist: str,
        title: str,
        seed_tags: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        pool: List[Dict[str, Any]] = []
        seen: set[str] = set()

        similar_tracks = await self._get_similar_tracks(
            artist, title, limit=CANDIDATE_POOL_TARGET
        )
        for track in similar_tracks:
            candidate_artist = track.get("artist", {})
            if isinstance(candidate_artist, dict):
                candidate_artist = candidate_artist.get("name", "")
            candidate_title = track.get("name") or track.get("title") or ""
            candidate = self._make_candidate_entry(
                guild_id,
                candidate_artist,
                candidate_title,
                track.get("source", "similar"),
            )
            if candidate and candidate["track_key"] not in seen:
                seen.add(candidate["track_key"])
                pool.append(candidate)

        if len(pool) < CANDIDATE_POOL_TARGET:
            top_tracks = await self._get_artist_top_tracks(
                artist, exclude_titles=[title], limit=10
            )
            for track in top_tracks:
                candidate_artist = track.get("artist", {})
                if isinstance(candidate_artist, dict):
                    candidate_artist = candidate_artist.get("name", "")
                candidate_title = track.get("name") or track.get("title") or ""
                candidate = self._make_candidate_entry(
                    guild_id,
                    candidate_artist,
                    candidate_title,
                    track.get("source", "artist-top"),
                )
                if candidate and candidate["track_key"] not in seen:
                    seen.add(candidate["track_key"])
                    pool.append(candidate)

        if len(pool) < CANDIDATE_POOL_TARGET:
            fallback_tracks = await self._get_fallback_recommendations(
                artist, limit=CANDIDATE_POOL_TARGET
            )
            for track in fallback_tracks:
                candidate_artist = track.get("artist", {})
                if isinstance(candidate_artist, dict):
                    candidate_artist = candidate_artist.get("name", "")
                candidate_title = track.get("name") or track.get("title") or ""
                candidate = self._make_candidate_entry(
                    guild_id,
                    candidate_artist,
                    candidate_title,
                    track.get("source", "fallback"),
                )
                if candidate and candidate["track_key"] not in seen:
                    seen.add(candidate["track_key"])
                    pool.append(candidate)

        if len(pool) < CANDIDATE_POOL_TARGET and seed_tags:
            for tag in seed_tags[:3]:
                tag_tracks = await self._get_tag_top_tracks(tag, limit=20)
                for track in tag_tracks:
                    candidate_artist = track.get("artist") or track.get(
                        "artist", {}
                    ).get("name", "")
                    candidate_title = (
                        track.get("track")
                        or track.get("name")
                        or track.get("title")
                        or ""
                    )
                    candidate = self._make_candidate_entry(
                        guild_id,
                        candidate_artist,
                        candidate_title,
                        track.get("source", "tag"),
                    )
                    if candidate and candidate["track_key"] not in seen:
                        seen.add(candidate["track_key"])
                        pool.append(candidate)
                    if len(pool) >= CANDIDATE_POOL_TARGET:
                        break
                if len(pool) >= CANDIDATE_POOL_TARGET:
                    break

        LOG.info(
            f"Built candidate pool: {len(pool)} tracks (seed artist '{artist}', seed tags: {seed_tags or []})"
        )
        return pool

    async def _score_and_resolve_candidates(
        self,
        guild_id: int,
        seed_artist: str,
        seed_title: str,
        seed_tags: List[str],
        candidate_pool: List[Dict[str, Any]],
        expected_duration_ms: Optional[int],
        limit: int,
    ) -> List[Tuple[str, Any]]:
        if not candidate_pool:
            return []

        session_counts = self._recent_artist_counts(guild_id)
        session_tags_raw = self._genre_history.get(guild_id, [])
        session_tag_counter = (
            Counter(session_tags_raw) if session_tags_raw else Counter()
        )
        session_tag_set = {tag for tag, _ in session_tag_counter.most_common(5)}
        metadata_map: Dict[str, Dict[str, Any]] = {}

        top_for_enrichment = candidate_pool[:ENRICH_TOP_N]
        for idx, candidate in enumerate(top_for_enrichment):
            metadata_map[candidate["track_key"]] = await self._get_track_profile(
                candidate["artist"],
                candidate["track"],
                require_enrichment=idx < 3,
            )

        scored_candidates: List[Dict[str, Any]] = []
        now_ts = time.time()

        for candidate in candidate_pool:
            artist = candidate["artist"]
            track = candidate["track"]
            if (
                artist.lower() == seed_artist.lower()
                and track.lower() == seed_title.lower()
            ):
                continue

            track_key = candidate["track_key"]
            metadata = metadata_map.get(track_key)
            if metadata is None:
                metadata = await self._get_track_profile(artist, track)
                metadata_map[track_key] = metadata

            candidate_tags = metadata.get("tags", [])
            quality_score = candidate.get("quality_score", 0.5)

            seed_overlap = 0.0
            if seed_tags and candidate_tags:
                seed_overlap = len(set(seed_tags) & set(candidate_tags)) / max(
                    len(seed_tags), 1
                )

            session_overlap = 0.0
            if session_tag_set and candidate_tags:
                session_overlap = len(set(candidate_tags) & session_tag_set) / max(
                    len(session_tag_set), 1
                )

            artist_norm = self._normalize_artist_for_diversity(artist)
            artist_count = session_counts.get(artist_norm, 0)
            artist_affinity = self._clamp01(
                1.0 - (artist_count / max(ARTIST_REPEAT_LIMIT, 1))
            )
            novelty = self._clamp01(
                (1.0 - max(seed_overlap, session_overlap))
                + self._rng.uniform(0.0, EPSILON_EXPLORE)
            )
            content_sim = seed_overlap
            session_coherence = session_overlap

            base_score = (
                content_sim * 0.45
                + artist_affinity * 0.18
                + session_coherence * 0.12
                + quality_score * 0.15
                + novelty * 0.10
            )

            cooldown_remaining = self._artist_cooldowns.setdefault(guild_id, {}).get(
                artist_norm, 0
            )
            if artist_count >= ARTIST_REPEAT_LIMIT:
                base_score -= 0.5
            if cooldown_remaining > 0:
                base_score -= 0.2 * cooldown_remaining

            failed_ts = self._failed_resolve_map.setdefault(guild_id, {}).get(track_key)
            if failed_ts and (now_ts - failed_ts) < FAILED_TRACK_COOLDOWN_SECONDS:
                base_score = -1.0

            if base_score <= 0:
                continue

            scored_candidates.append(
                {
                    **candidate,
                    "score": base_score,
                    "tags": candidate_tags,
                    "artist_norm": artist_norm,
                }
            )

        if LOG_LEVEL >= 2 and scored_candidates:
            debug_preview = [
                (c["artist"], c["track"], round(c["score"], 3))
                for c in scored_candidates[:10]
            ]
            LOG.debug(f"Candidate scores preview: {debug_preview}")

        scored_candidates.sort(key=lambda x: x["score"], reverse=True)

        resolved: List[Tuple[str, Any]] = []
        for candidate in scored_candidates:
            if len(resolved) >= min(limit, RESOLVE_TOP_K):
                break

            playable_track = await self._resolve_lastfm_to_youtube(
                candidate["artist"],
                candidate["track"],
                duration_ms=expected_duration_ms,
            )

            if playable_track:
                resolved.append((candidate["track_key"], playable_track))
                self._record_recommendation_metric(guild_id, candidate)
                self._mark_track_played(
                    guild_id,
                    candidate["artist"],
                    candidate["track"],
                    candidate.get("source"),
                    candidate.get("quality_score"),
                )
                session_counts[candidate["artist_norm"]] = (
                    session_counts.get(candidate["artist_norm"], 0) + 1
                )

                cooldowns = self._artist_cooldowns.setdefault(guild_id, {})
                cooldowns[candidate["artist_norm"]] = ARTIST_COOLDOWN_CYCLES
                for other in list(cooldowns.keys()):
                    if other == candidate["artist_norm"]:
                        continue
                    cooldowns[other] = max(0, cooldowns[other] - 1)
                    if cooldowns[other] == 0:
                        cooldowns.pop(other, None)

                tags = candidate.get("tags") or []
                if tags:
                    genre_track = self._genre_history.setdefault(guild_id, [])
                    genre_track.append(tags[0])
                    if len(genre_track) > 60:
                        del genre_track[:-60]
            else:
                self._mark_resolve_failure(
                    guild_id, candidate["artist"], candidate["track"]
                )

        if resolved:
            self._maybe_flush_mapping_cache(force=True)
            self._save_guild_history(guild_id)

        return resolved

    def _fallback_parse_track(
        self, raw_title: str, channel_name: str
    ) -> Optional[Dict[str, str]]:
        """Simple heuristic parsing when Gemini is unavailable."""

        base_title = raw_title or ""
        if not base_title.strip():
            return None

        normalized = self._sanitize_title_text(base_title)

        candidates: List[Tuple[str, str]] = []
        separators = [" - ", " – ", " — ", " ~ ", " : "]
        for sep in separators:
            if sep in normalized:
                left, right = normalized.split(sep, 1)
                candidates.append((left.strip(), right.strip()))

        if not candidates and "-" in normalized:
            left, right = normalized.split("-", 1)
            candidates.append((left.strip(), right.strip()))

        for artist_raw, title_raw in candidates:
            artist = self._sanitize_artist_name(artist_raw)
            title = self._sanitize_title_text(title_raw)
            if artist and title:
                return {"artist": artist, "title": title}

        channel_artist = self._sanitize_artist_name(channel_name or "")
        if channel_artist:
            title = self._sanitize_title_text(normalized)
            if channel_artist and title:
                return {"artist": channel_artist, "title": title}

        return None

    async def _parse_track_with_gemini(
        self, raw_title: str, channel_name: str
    ) -> Optional[Dict[str, str]]:
        """
        Use Gemini AI to parse artist and title from YouTube metadata.
        This replaces regex-based parsing for much better accuracy.

        Args:
            raw_title: Raw YouTube video title
            channel_name: YouTube channel name

        Returns:
            Dict with 'artist' and 'title' keys, or None if parsing failed
        """
        if not self._gemini_available or not self._gemini_client:
            fallback = self._fallback_parse_track(raw_title, channel_name)
            if fallback:
                LOG.warning(
                    "⚠️ [Last.fm] Gemini unavailable, using heuristic track parsing"
                )
                return fallback
            LOG.warning("⚠️ [Last.fm] Gemini not available, cannot parse track")
            return None

        prompt = f"""You are a music metadata parser for Last.fm integration. Extract the artist name and song title from this YouTube video information.

YouTube Title: "{raw_title}"
Channel Name: "{channel_name}"

Rules:
1. Return ONLY a JSON object with "artist" and "title" fields
2. Remove ANY extra text like [Official Video], (Lyrics), feat., ft., (Official Audio), etc.
3. If the title contains "Artist - Song Title" format, split it correctly
4. If artist is not clearly in the title, intelligently infer from the channel name
5. Clean up promotional text, quality markers (HD, 4K, 8K), version info, remixes markers
6. Keep ONLY the core song title and primary artist name
7. Be accurate - this will be used to search Last.fm API
Note: Sometimes the channel name is a lyrics channel or fan channel, so use your best judgment to find the real artist, not just the channel name.

Example inputs/outputs:
Input: "Adele - Hello (Official Video)"
Output: {{"artist": "Adele", "title": "Hello"}}

Input: "The Weeknd - Blinding Lights (Official Audio)"
Output: {{"artist": "The Weeknd", "title": "Blinding Lights"}}

Input: "AMNESIA (from Garten of Banban 0) - Black Gryph0n & Baasik"
Output: {{"artist": "Black Gryph0n", "title": "Amnesia"}}

Input: "Taylor Swift - Anti-Hero (Official Music Video)"
Output: {{"artist": "Taylor Swift", "title": "Anti-Hero"}}

Respond with ONLY the JSON object, no other text."""

        config = self._gemini_base_config
        if config is None and self._gemini_search_tool:
            config = types.GenerateContentConfig(tools=[self._gemini_search_tool])

        try:
            response = await asyncio.to_thread(
                self._gemini_client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
        except Exception as e:
            LOG.error(f"❌ [Gemini] Error parsing track: {e}")
            return None

        structured = self._parse_gemini_json(response)
        if structured:
            artist = str(structured.get("artist", "")).strip()
            title = str(structured.get("title", "")).strip()
            if artist and title:
                LOG.debug(
                    f"🎧 [Gemini] Parsed track metadata → artist='{artist}', title='{title}'"
                )
                return {"artist": artist, "title": title}
            LOG.warning(
                f"⚠️ [Gemini] Parsed result missing required fields: {structured}"
            )
        else:
            texts = self._collect_gemini_texts(response)
            if texts:
                preview = self._strip_code_fence(texts[0])[:200]
                LOG.debug(f"⚠️ [Gemini] Failed to parse JSON response. Raw: {preview}")
            else:
                LOG.debug("⚠️ [Gemini] Empty response payload while parsing track")

        fallback = self._fallback_parse_track(raw_title, channel_name)
        if fallback:
            LOG.warning(
                "⚠️ [Last.fm] Falling back to heuristic track parsing due to Gemini failure"
            )
            LOG.debug(
                f"🎧 [Heuristic] Parsed track metadata → artist='{fallback['artist']}', title='{fallback['title']}'"
            )
            return fallback

        LOG.error("❌ [Gemini] Unable to derive artist/title from provided metadata")
        return None

    async def _lastfm_get(
        self, params: Dict[str, Any], session: aiohttp.ClientSession
    ) -> Optional[Dict[str, Any]]:
        """
        Robust Last.fm API request with timeout and error handling.

        Args:
            params: API parameters
            session: aiohttp session

        Returns:
            JSON response dict or None on error
        """
        params.update({"api_key": self.api_key, "format": "json"})
        try:
            async with session.get(
                LASTFM_API_BASE, params=params, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    LOG.debug(f"Last.fm HTTP {resp.status} for {params.get('method')}")
                    return None
                return await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            LOG.debug(f"Last.fm request error for {params.get('method')}: {e}")
            return None

    async def _get_fallback_recommendations(
        self, artist: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get fallback recommendations using artist.getSimilar and tag-based search.

        This is used when direct track matching fails. It finds similar artists
        and their popular tracks, or uses genre tags to find related music.

        Args:
            artist: Artist name to base recommendations on
            limit: Maximum recommendations to return

        Returns:
            List of dicts with keys: artist, title
        """
        out = []

        async with aiohttp.ClientSession() as session:
            # Strategy 1: Similar artists -> their top tracks (EXPANDED POOL)
            j = await self._lastfm_get(
                {"method": "artist.getSimilar", "artist": artist, "limit": 20},
                session,  # Increased from 8 to 20
            )
            similar_artists = []
            if j:
                sim = j.get("similarartists", {}).get("artist", [])
                if isinstance(sim, dict):
                    sim = [sim]
                for a in sim:
                    name = a.get("name")
                    if name:
                        similar_artists.append(name)

            # Get top tracks from similar artists (parallel) - more tracks per artist
            tasks = [
                self._lastfm_get(
                    {"method": "artist.getTopTracks", "artist": a, "limit": 5},
                    session,  # Increased from 3 to 5
                )
                for a in similar_artists
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for r in results:
                if not r or isinstance(r, Exception):
                    continue
                tracks = r.get("toptracks", {}).get("track", [])
                if isinstance(tracks, dict):
                    tracks = [tracks]
                for t in tracks[:5]:  # Increased from 3 to 5
                    name = t.get("name")
                    aobj = t.get("artist", {})
                    aname = aobj.get("name") if isinstance(aobj, dict) else None
                    if name and aname:
                        out.append(
                            {
                                "artist": {"name": aname},
                                "name": name,
                                "source": "similar-artist-top",
                            }
                        )

            # Strategy 2: If still empty, use artist tags -> tag top tracks
            if not out:
                jtags = await self._lastfm_get(
                    {"method": "artist.getTopTags", "artist": artist}, session
                )
                tags = []
                if jtags:
                    t = jtags.get("toptags", {}).get("tag", [])
                    if isinstance(t, dict):
                        t = [t]
                    for tg in t[:3]:
                        tags.append(tg.get("name"))

                for tag in tags:
                    jtag = await self._lastfm_get(
                        {"method": "tag.getTopTracks", "tag": tag, "limit": 5}, session
                    )
                    if not jtag:
                        continue
                    tracks = jtag.get("tracks", {}).get("track", [])
                    if isinstance(tracks, dict):
                        tracks = [tracks]
                    for tr in tracks[:5]:
                        a = (
                            tr.get("artist", {}).get("name")
                            if isinstance(tr.get("artist"), dict)
                            else None
                        )
                        n = tr.get("name")
                        if a and n:
                            out.append(
                                {
                                    "artist": {"name": a},
                                    "name": n,
                                    "source": "tag-top-track",
                                }
                            )

                # Strategy 3: Spotify-lite genre exploration - use similar tags
                if len(out) < limit and tags:
                    # Get similar genres to explore multi-dimensional taste space
                    primary_tag = tags[0] if tags else None
                    if primary_tag:
                        similar_tags = await self._get_similar_tags(
                            primary_tag, limit=3
                        )
                        for similar_tag in similar_tags:
                            jtag = await self._lastfm_get(
                                {
                                    "method": "tag.getTopTracks",
                                    "tag": similar_tag,
                                    "limit": 5,
                                },
                                session,
                            )
                            if not jtag:
                                continue
                            tracks = jtag.get("tracks", {}).get("track", [])
                            if isinstance(tracks, dict):
                                tracks = [tracks]
                            for tr in tracks[:5]:
                                a = (
                                    tr.get("artist", {}).get("name")
                                    if isinstance(tr.get("artist"), dict)
                                    else None
                                )
                                n = tr.get("name")
                                if a and n:
                                    out.append(
                                        {
                                            "artist": {"name": a},
                                            "name": n,
                                            "source": "similar-genre",
                                        }
                                    )

        # Dedupe and limit
        seen = set()
        final = []
        for item in out:
            artist_name = (
                item.get("artist", {}).get("name")
                if isinstance(item.get("artist"), dict)
                else item.get("artist", "")
            )
            track_name = item.get("name", "")

            if not artist_name or not track_name:
                continue

            key = (artist_name.lower(), track_name.lower())
            if key in seen:
                continue
            seen.add(key)
            final.append(item)
            if len(final) >= limit:
                break

        return final

    async def _get_artist_top_tracks(
        self,
        artist: str,
        exclude_titles: Optional[List[str]] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Fetch popular tracks from the same artist for variety."""
        if not artist:
            return []

        excluded = {t.lower() for t in (exclude_titles or []) if t}
        results: List[Dict[str, Any]] = []

        async with aiohttp.ClientSession() as session:
            data = await self._lastfm_get(
                {"method": "artist.getTopTracks", "artist": artist, "limit": limit},
                session,
            )

        if not data:
            return results

        tracks = data.get("toptracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]

        for track in tracks[: limit * 2]:  # grab a few extra before filtering
            name = track.get("name")
            if not name or name.lower() in excluded:
                continue

            results.append(
                {
                    "artist": {"name": artist},
                    "name": name,
                    "source": "artist-top-track",
                }
            )

            if len(results) >= limit:
                break

            LOG.debug(
                f"🎧 [Last.fm] Added {len(results)} top tracks for artist '{artist}'"
            )
        return results

    async def _get_similar_tracks(
        self, artist: str, track: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Get similar tracks from Last.fm API.

        Args:
            artist: Artist name
            track: Track name
            limit: Maximum number of results

        Returns:
            List of similar tracks with artist and name
        """
        if not self.is_available():
            return []

        try:
            params = {
                "method": "track.getSimilar",
                "artist": artist,
                "track": track,
                "api_key": self.api_key,
                "format": "json",
                "limit": limit,
                "autocorrect": 1,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(LASTFM_API_BASE, params=params) as response:
                    if response.status != 200:
                        LOG.error(
                            f"Last.fm API error: HTTP {response.status} for track '{artist} - {track}'"
                        )
                        return []

                    data = await response.json()

                    # Check for API errors
                    if "error" in data:
                        error_code = data.get("error", "unknown")
                        error_msg = data.get("message", "Unknown error")
                        LOG.error(
                            f"Last.fm API error {error_code}: {error_msg} for track '{artist} - {track}'"
                        )
                        return []

                    # Extract similar tracks
                    similar_tracks = data.get("similartracks", {}).get("track", [])

                    # Handle case where only 1 track returned (not a list)
                    if isinstance(similar_tracks, dict):
                        similar_tracks = [similar_tracks]

                    if not similar_tracks:
                        LOG.warning(
                            f"⚠️ [Last.fm] No similar tracks found for '{artist}' - '{track}'. "
                            f"This might mean Last.fm doesn't have data for this track."
                        )

                    return similar_tracks

        except asyncio.TimeoutError:
            LOG.error(f"Last.fm API timeout for track '{artist} - {track}'")
            return []
        except Exception as e:
            LOG.error(f"Last.fm API error for track '{artist} - {track}': {e}")
            return []

    async def _resolve_lastfm_to_youtube(
        self, artist: str, track: str, duration_ms: Optional[int] = None
    ) -> Optional[Any]:
        """
        Resolve Last.fm track to a playable Lavalink/YouTube track.

        Args:
            artist: Artist name
            track: Track name
            duration_ms: Expected duration in milliseconds (for matching)

        Returns:
            Pomice Track object or None
        """
        # Generate cache key
        cache_key = f"{artist.lower()}:{track.lower()}"

        # Check cache first (if not expired)
        if cache_key in self._cache:
            playable_id, timestamp = self._cache[cache_key]
            if time.time() - timestamp < CACHE_MAX_AGE:
                pass

        try:
            query = f"{artist} {track} official audio"

            # Use Pomice NodePool to get node
            import pomice

            node_pool_cls = getattr(pomice, "NodePool", None)
            if node_pool_cls is None:
                LOG.error("Pomice NodePool not available")
                return None

            try:
                node = node_pool_cls.get_node()
            except Exception as e:
                LOG.error(f"Failed to get Pomice node: {e}")
                return None

            if not node:
                LOG.error("No Pomice node available")
                return None

            # Search using the node
            results = await node.get_tracks(f"ytsearch:{query}")

            if not results:
                return None

            # Filter and find best match
            best_match = None
            best_score = 0

            for track_obj in results:
                score = self._calculate_match_score(
                    track_obj, artist, track, duration_ms
                )
                if score > best_score:
                    best_score = score
                    best_match = track_obj

            if best_match and best_score > 0.3:  # Minimum match threshold
                # Cache this mapping
                self._cache[cache_key] = (query, time.time())
                self._maybe_flush_mapping_cache()

                return best_match

            return None

        except Exception as e:
            LOG.error(f"Error resolving Last.fm track to YouTube: {e}")
            return None

    def _calculate_match_score(
        self,
        track_obj: Any,
        expected_artist: str,
        expected_title: str,
        expected_duration_ms: Optional[int] = None,
    ) -> float:
        """
        Calculate how well a YouTube track matches the expected Last.fm track.

        Returns:
            Score from 0.0 to 1.0 (higher is better)
        """
        score = 0.0
        title_lower = track_obj.title.lower()
        expected_title_lower = expected_title.lower()
        expected_artist_lower = expected_artist.lower()

        # Title similarity (0.4 weight)
        if expected_title_lower in title_lower or title_lower in expected_title_lower:
            score += 0.4
        else:
            # Partial match
            title_words = set(expected_title_lower.split())
            found_words = sum(1 for word in title_words if word in title_lower)
            score += 0.4 * (found_words / max(len(title_words), 1))

        # Artist similarity (0.3 weight)
        if expected_artist_lower in title_lower:
            score += 0.3

        # Duration similarity (0.2 weight)
        if expected_duration_ms:
            duration_diff = abs(track_obj.length - expected_duration_ms)
            if duration_diff < DURATION_TOLERANCE_SECONDS * 1000:
                score += 0.2
            elif duration_diff < expected_duration_ms * DURATION_TOLERANCE_PERCENT:
                score += 0.1

        # Quality indicators (0.1 weight)
        good_keywords = sum(1 for kw in GOOD_TITLE_KEYWORDS if kw in title_lower)
        bad_keywords = sum(1 for kw in BAD_TITLE_KEYWORDS if kw in title_lower)

        if good_keywords > 0:
            score += 0.05
        if bad_keywords > 0:
            score -= 0.2  # Penalize bad matches

        return max(0.0, min(1.0, score))

    async def get_recommendations_for_track(
        self, track_info: Dict[str, Any], limit: int = 10
    ) -> List[Tuple[str, Any]]:
        """Return autoplay recommendations with diversity, quality, and loop guards."""

        if not self.is_available():
            LOG.debug(
                "Last.fm autoplay not available (Gemini or Last.fm not configured)"
            )
            return []

        raw_title = track_info.get("title", "").strip()
        channel_name = track_info.get("author", "").strip()
        expected_duration_ms = track_info.get("length")
        guild_id = int(track_info.get("guild_id", 0) or 0)

        if not raw_title:
            LOG.warning("Missing track title for autoplay seed")
            return []

        LOG.info(f"Seed track → {channel_name} :: {raw_title}")

        parsed = await self._parse_track_with_gemini(raw_title, channel_name)
        if not parsed or not parsed.get("artist") or not parsed.get("title"):
            LOG.error(
                "Unable to parse artist/title from seed track; aborting autoplay round"
            )
            return []

        seed_artist = parsed["artist"]
        seed_title = parsed["title"]

        if guild_id not in self._recent_autoplayed:
            self._load_guild_history(guild_id)
        self._recent_autoplayed.setdefault(guild_id, [])
        self._session_history.setdefault(guild_id, deque(maxlen=SESSION_HISTORY_LIMIT))
        self._artist_cooldowns.setdefault(guild_id, {})
        self._genre_history.setdefault(guild_id, [])
        self._recent_track_set.setdefault(guild_id, {})
        self._failed_resolve_map.setdefault(guild_id, {})

        seed_profile = await self._get_track_profile(seed_artist, seed_title)
        seed_tags = seed_profile.get("tags") or []

        candidate_pool = await self._build_candidate_pool(
            guild_id, seed_artist, seed_title, seed_tags
        )
        candidate_pool = [c for c in candidate_pool if c["quality_score"] >= 0.15]

        if LOG_LEVEL >= 2 and candidate_pool:
            preview = [
                (c["artist"], c["track"], round(c["quality_score"], 2))
                for c in candidate_pool[:5]
            ]
            LOG.debug(f"Candidate pool preview: {preview}")

        recommendations = await self._score_and_resolve_candidates(
            guild_id,
            seed_artist,
            seed_title,
            seed_tags,
            candidate_pool,
            expected_duration_ms,
            limit,
        )

        if recommendations:
            return recommendations

        fallback_raw = await self._trigger_escape_hatch(
            guild_id, "primary candidate pool empty", seed_artist, seed_tags
        )
        fallback_candidates: List[Dict[str, Any]] = []
        for track in fallback_raw:
            candidate_artist = track.get("artist")
            if isinstance(candidate_artist, dict):
                candidate_artist = candidate_artist.get("name", "")
            candidate_title = (
                track.get("track") or track.get("name") or track.get("title") or ""
            )
            candidate = self._make_candidate_entry(
                guild_id,
                candidate_artist or "",
                candidate_title,
                track.get("source", "fallback"),
            )
            if candidate:
                fallback_candidates.append(candidate)

        fallback_candidates = [
            c for c in fallback_candidates if c["quality_score"] >= 0.15
        ]

        return await self._score_and_resolve_candidates(
            guild_id,
            seed_artist,
            seed_title,
            seed_tags,
            fallback_candidates,
            expected_duration_ms,
            limit,
        )

    async def _get_similar_tags(self, tag: str, limit: int = 5) -> List[str]:
        """Get similar genres/tags from Last.fm API (Spotify-lite: genre exploration)."""
        similar_tags = []
        async with aiohttp.ClientSession() as session:
            resp = await self._lastfm_get(
                {"method": "tag.getSimilar", "tag": tag, "limit": limit}, session
            )
            if not resp:
                return []

            similartags = resp.get("similartags", {}).get("tag", [])
            if isinstance(similartags, dict):
                similartags = [similartags]

            for t in similartags[:limit]:
                tag_name = t.get("name")
                if tag_name:
                    similar_tags.append(tag_name.lower())

        return similar_tags

    async def _get_tag_top_tracks(self, tag: str, limit: int = 10) -> list:
        """Get top tracks for a given genre/tag from Last.fm."""
        out = []
        async with aiohttp.ClientSession() as session:
            jtag = await self._lastfm_get(
                {"method": "tag.getTopTracks", "tag": tag, "limit": limit}, session
            )
            if not jtag:
                return []
            tracks = jtag.get("tracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            for tr in tracks[:limit]:
                a = (
                    tr.get("artist", {}).get("name")
                    if isinstance(tr.get("artist"), dict)
                    else None
                )
                n = tr.get("name")
                if a and n:
                    out.append(
                        {
                            "artist": a,
                            "track": n,
                            "track_key": f"{a.lower()}|||{n.lower()}",
                            "source": "genre-fallback",
                        }
                    )
        return out


_lastfm_autoplay_instance: Optional[LastFMAutoplay] = None


def get_lastfm_autoplay(bot) -> LastFMAutoplay:
    """Get or create the global Last.fm autoplay instance."""
    global _lastfm_autoplay_instance
    if _lastfm_autoplay_instance is None:
        _lastfm_autoplay_instance = LastFMAutoplay(bot)
    return _lastfm_autoplay_instance
