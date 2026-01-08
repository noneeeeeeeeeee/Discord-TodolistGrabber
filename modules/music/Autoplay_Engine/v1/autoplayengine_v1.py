import os
import asyncio
import time
import logging
import json
import random
import re
import math
from collections import Counter, deque
from datetime import datetime
from typing import Deque, Dict, Any, List, Optional, Tuple
from pathlib import Path

import aiohttp
from google import genai
from google.genai import types

LOG = logging.getLogger(__name__)

# _____     _       _____ _
# |  _  |_ _| |_ ___|  _  | |___ _ _
# |     | | |  _| . |   __| | .'| | |
# |__|__|___|_| |___|__|  |_|__,|_  |
#                              |___|
# This module implements Last.fm-based autoplay recommendations with Gemini AI parsing.
# This module has been revived for use as a fallback engine instead. Once v3 is finished, this will be improved further.

LOG_LEVEL = 1  # 0=ERROR, 1=INFO, 2=DEBUG, 3=All (Higher number more verbose)
SESSION_HISTORY_LIMIT = 50  # How many recent tracks to remember in a single session (if you change this value, you may need to change the other numbers to fit with it)
RECENT_TRACK_WINDOW_SECONDS = 90 * 60  # 90 minutes
FAILED_TRACK_COOLDOWN_SECONDS = 30 * 60  # 30 minutes
GROUNDING_DAILY_LIMIT = 500  # 500 requests per day
CACHE_FLUSH_INTERVAL_SECONDS = 120  # Flush caches at most every 2 minutes
METRICS_HISTORY_LIMIT = 200  # Retain recent recommendation metrics
ESCAPE_REASON_HISTORY_LIMIT = 16
DJ_LISTENER_BIAS = (
    False  # If True, DJs have more influence on autoplay than listeners (1.25x weight)
)

# Recommendation tuning
CACHE_MAX_AGE = 60 * 60 * 24 * 15  # 15 days TTL for Last.fm->YouTube cache mappings
METADATA_CACHE_MAX_AGE = 60 * 60 * 24 * 90  # 90 days TTL for metadata
DURATION_TOLERANCE_SECONDS = 5  # +/- 5 seconds tolerance
DURATION_TOLERANCE_PERCENT = 0.12  # +/- 12% duration tolerance
CANDIDATE_POOL_TARGET = 35  # Aim for ~35 candidates before filtering
ENRICH_TOP_N = 16  # How many top Last.fm tags to fetch and use for Gemini enrichment
RESOLVE_TOP_K = 6  # How many top YouTube search results to consider for matching
ARTIST_REPEAT_LIMIT = 5  # Max times the same artist can appear in session history
ARTIST_COOLDOWN_CYCLES = 3  # How many tracks before an artist can reappear
SKIP_EARLY_THRESHOLD = 0.2  # Listen progress considered an immediate skip
SKIP_THRESHOLD = 0.8  # Progress threshold treated as a skip event
FINISH_THRESHOLD = 0.9  # Progress threshold considered a full completion

# ═══════════════════════════════════════════════════════════════════════════════
# DIVERSITY & EXPLORATION SYSTEMS
# ═══════════════════════════════════════════════════════════════════════════════
# Multiple complementary systems control recommendation diversity. Each serves
# a distinct purpose and operates on different timescales:

# ───────────────────────────────────────────────────────────────────────────────
# 1. EPSILON (Adaptive Real-Time Exploration)
# ───────────────────────────────────────────────────────────────────────────────
# Purpose: Fast-reacting exploration rate that adapts to user feedback signals
# Mechanism: Increases on skips (user wants something different), decreases on
#            finishes (user is satisfied). Acts as random noise in scoring.
# Scope: Per-guild, updates every track based on listen progress
# Timescale: Immediate (adjusts within 1-2 tracks)
EPSILON_BASE = 0.08  # Default exploration factor (8% random variance in scoring)
EPSILON_MIN = 0.02  # Floor to maintain minimum discovery even when satisfied
EPSILON_MAX = 0.28  # Ceiling to prevent chaos even when frustrated
EPSILON_DELTA_SKIP = 0.02  # Increment per skip (accelerates exploration)
EPSILON_DELTA_FINISH = 0.012  # Decrement per finish (rewards good picks)

# ───────────────────────────────────────────────────────────────────────────────
# 2. PROGRESSIVE DIVERSITY INJECTION (Spotify-Style Session Arc)
# ───────────────────────────────────────────────────────────────────────────────
# Purpose: Gradually introduce variety over session lifetime to prevent monotony
# Mechanism: Multiplies epsilon by increasing factor as autoplay count grows
# Scope: Per-guild session (resets when autoplay restarts)
# Timescale: Medium (ramps over 10-18 tracks)
# Note: Independent from epsilon adjustments—multiplies final epsilon_rate
DIVERSITY_INJECT_START_TRACK = 10  # Keep first N tracks highly similar to seed
DIVERSITY_INJECT_RAMP_TRACKS = 8  # Fully ramp exploration over next N tracks
DIVERSITY_INJECT_MAX_MULTIPLIER = 2.5  # Peak multiplier (e.g., 0.08 → 0.20 epsilon)

# ───────────────────────────────────────────────────────────────────────────────
# 3. GENRE DOMINANCE PRESSURE (Anti-Repetition Safeguard)
# ───────────────────────────────────────────────────────────────────────────────
# Purpose: Emergency diversity boost when single genre dominates recent history
# Mechanism: Adds fixed pressure when one genre exceeds threshold in rolling window
# Scope: Per-guild, examines last N autoplay picks
# Timescale: Short-term reactive (evaluates every recommendation)
# Note: Adds to epsilon (not multiplies), capped at EPSILON_DIVERSITY_BOOST_MAX
GENRE_DIVERSITY_WINDOW = 12  # Rolling window size for dominance detection
GENRE_DOMINANCE_MIN_COUNT = (
    4  # Minimum occurrences to trigger (prevents false positives)
)
GENRE_DOMINANCE_THRESHOLD = 0.55  # Ratio threshold (55% of window)
EPSILON_DIVERSITY_BOOST_MAX = 0.12  # Max additive boost from genre pressure + sentiment
GENRE_HISTORY_LIMIT = 60  # Total genre history retained (for decay/analysis)

# ───────────────────────────────────────────────────────────────────────────────
# 4. SESSION GENRE SENTIMENT (User Preference Learning)
# ───────────────────────────────────────────────────────────────────────────────
# Purpose: Track which genres user likes/dislikes within current session
# Mechanism: Accumulates pos/neg signals per genre, decays over time, feeds into
#            scoring multipliers and can add to diversity_pressure if strongly negative
# Scope: Per-guild session with time-based decay
# Timescale: Long-term accumulative (45min half-life)
# Note: Shares EPSILON_DIVERSITY_BOOST_MAX cap with genre dominance pressure
SESSION_GENRE_SENTIMENT_HALFLIFE = 45 * 60  # Decay rate (seconds)
SESSION_GENRE_SENTIMENT_MIN = 0.02  # Prune threshold for memory efficiency
SESSION_GENRE_SENTIMENT_MAX_BONUS = 0.55  # Max scoring multiplier for liked genres
SESSION_GENRE_SENTIMENT_MAX_PENALTY = 0.65  # Max scoring penalty for disliked genres
SESSION_GENRE_SENTIMENT_PENALTY_THRESHOLD = (
    -0.35
)  # Sentiment threshold to add diversity pressure
SESSION_GENRE_SKIP_ESCALATION_THRESHOLD = (
    3  # Consecutive skips trigger harsh escape penalty
)

# ═══════════════════════════════════════════════════════════════════════════════

CANDIDATE_MULTIPLIERS = {
    "hard_skip": 0.20,
    "medium_skip": 0.50,
    "late_skip": 0.85,
    "finish": 1.20,
}

SESSION_TAG_MULTIPLIERS = {
    "hard_skip": (0.25, 24 * 3600, 0.92),
    "medium_skip": (0.60, 12 * 3600, 0.94),
    "finish": (1.20, 12 * 3600, 0.94),
}

SESSION_ARTIST_MULTIPLIERS = {
    "hard_skip": (0.35, 24 * 3600, 0.90, 0.0),
    "medium_skip": (0.60, 12 * 3600, 0.93, 0.0),
    "finish": (1.10, 12 * 3600, 0.94, 0.10),
}

GUILD_PREFERENCE_DECAY = 0.96  # decay per day for guild-level preferences

# Gemini configuration
GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_BATCH_MAX = 25
GEMINI_BATCH_DELAY_SECONDS = 1
GEMINI_BATCH_TIMEOUT_SECONDS = 15.0


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
                message = ""
                if args:
                    fmt = args[0]
                    if len(args) > 1:
                        try:
                            message = str(fmt) % args[1:]
                        except Exception:
                            message = " ".join(str(a) for a in args)
                    else:
                        message = str(fmt)
                else:
                    message = str(kwargs.get("msg", ""))

                if kwargs.get("exc_info"):
                    exc = kwargs.get("exc_info")
                    if isinstance(exc, tuple):
                        import traceback

                        message = (
                            f"{message}\n{''.join(traceback.format_exception(*exc))}"
                        )

                print(f"[AUTO][{method_name.upper()}] {message}")
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
    "extended version",
    "extended",
    "version",
    # performance / live
    "live",
    "live at",
    "live from",
    "session",
    "concert",
    "performance",
    "tour",
    "interview,"
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
    "version",
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
    # Tutorial Videos
    "how to",
    "tutorial",
    "lesson",
    "practice",
    "learn",
    "teach",
)

CANONICAL_CHANNEL_HINTS = (
    "official artist channel",
    "vevo",
    "topic",
    "official",
    "records",
    "music",
    "label",
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
        # V1 uses separate cache file to avoid conflicts with V2
        self._cache_file = self._cache_dir / "lastfm_mappings_v1.json"
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
        # V1 uses separate metadata cache to avoid conflicts with V2
        self._metadata_cache_file = self._cache_dir / "track_metadata_v1.json"
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
        self._gemini_batch_queue: List[Dict[str, Any]] = []
        self._gemini_batch_lock = asyncio.Lock()
        self._gemini_batch_task: Optional[asyncio.Task] = None
        self._candidate_feedback: Dict[int, Dict[str, Dict[str, Any]]] = {}
        self._tag_feedback: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
        self._artist_feedback: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
        self._explore_state: Dict[int, Dict[str, Any]] = {}
        self._guild_skip_stats: Dict[int, Dict[str, float]] = {}
        self._guild_preferences: Dict[int, Dict[str, Any]] = {}
        self._session_genre_sentiment: Dict[int, Dict[str, Any]] = {}

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

    # --- Feedback helpers -------------------------------------------------

    def _now(self) -> float:
        return time.time()

    def _get_exploration_state(self, guild_id: int) -> Dict[str, Any]:
        state = self._explore_state.setdefault(
            guild_id,
            {
                "epsilon": EPSILON_BASE,
                "events": deque(maxlen=METRICS_HISTORY_LIMIT),
                "last_updated": self._now(),
                "last_diversity_notice": 0.0,
                "autoplay_count": 0,
                "consecutive_genre_skips": {},
            },
        )
        state["last_updated"] = self._now()
        return state

    def _get_exploration_rate(self, guild_id: int) -> float:
        state = self._get_exploration_state(guild_id)
        return float(state.get("epsilon", EPSILON_BASE))

    def _compute_progressive_diversity_factor(self, guild_id: int) -> float:
        """Compute progressive diversity multiplier based on session autoplay count."""
        state = self._get_exploration_state(guild_id)
        count = int(state.get("autoplay_count", 0))
        if count < DIVERSITY_INJECT_START_TRACK:
            return 1.0
        overage = count - DIVERSITY_INJECT_START_TRACK
        ramp_progress = min(1.0, overage / max(1.0, DIVERSITY_INJECT_RAMP_TRACKS))
        multiplier = 1.0 + ramp_progress * (DIVERSITY_INJECT_MAX_MULTIPLIER - 1.0)
        return multiplier

    def _increment_autoplay_count(self, guild_id: int) -> None:
        state = self._get_exploration_state(guild_id)
        state["autoplay_count"] = int(state.get("autoplay_count", 0)) + 1

    def _note_genre_skip(self, guild_id: int, genre: str) -> int:
        """Track consecutive skips of same genre; return count."""
        state = self._get_exploration_state(guild_id)
        skip_map: Dict[str, int] = state.setdefault("consecutive_genre_skips", {})
        skip_map[genre] = skip_map.get(genre, 0) + 1
        return skip_map[genre]

    def _reset_genre_skip_count(self, guild_id: int, genre: str) -> None:
        state = self._get_exploration_state(guild_id)
        skip_map: Dict[str, int] = state.get("consecutive_genre_skips", {})
        skip_map.pop(genre, None)

    def _adjust_exploration_rate(
        self, guild_id: int, outcome: str, magnitude: float
    ) -> None:
        state = self._get_exploration_state(guild_id)
        previous_epsilon = float(state.get("epsilon", EPSILON_BASE))
        epsilon = previous_epsilon
        if outcome in ("hard_skip", "medium_skip", "late_skip"):
            epsilon = min(EPSILON_MAX, epsilon + EPSILON_DELTA_SKIP * magnitude)
        elif outcome == "finish":
            epsilon = max(EPSILON_MIN, epsilon - EPSILON_DELTA_FINISH * magnitude)
        state["epsilon"] = round(epsilon, 5)
        state["events"].append((self._now(), outcome, magnitude))
        if LOG_LEVEL >= 2 and abs(epsilon - previous_epsilon) >= 1e-4:
            LOG.debug(
                "[Explore] Epsilon updated via %s (guild=%s, magnitude=%.2f, %.3f → %.3f)",
                outcome,
                guild_id,
                magnitude,
                previous_epsilon,
                epsilon,
            )

    @staticmethod
    def _decayed_multiplier(entry: Dict[str, Any], now: float) -> float:
        multiplier = float(entry.get("multiplier", 1.0))
        applied_at = float(entry.get("applied_at", now))
        duration = float(entry.get("duration", 0.0))
        decay_rate = float(entry.get("decay_rate", 1.0))
        if multiplier == 1.0:
            return 1.0
        elapsed = max(0.0, now - applied_at)
        if duration and elapsed >= duration:
            return 1.0
        # convert elapsed seconds into pseudo steps (~per recommendation)
        steps = elapsed / 60.0
        influence = multiplier - 1.0
        decayed = 1.0 + influence * (decay_rate**steps)
        return max(0.1, min(2.5, decayed))

    def _register_candidate_feedback(
        self, guild_id: int, track_key: str, multiplier: float, ttl: float = 3600.0
    ) -> None:
        bucket = self._candidate_feedback.setdefault(guild_id, {})
        bucket[track_key] = {
            "multiplier": float(multiplier),
            "expires_at": self._now() + ttl,
        }

    def _get_candidate_feedback_multiplier(
        self, guild_id: int, track_key: str
    ) -> float:
        bucket = self._candidate_feedback.get(guild_id)
        if not bucket:
            return 1.0
        now = self._now()
        entry = bucket.get(track_key)
        if not entry:
            return 1.0
        if entry.get("expires_at", 0.0) < now:
            bucket.pop(track_key, None)
            return 1.0
        return float(entry.get("multiplier", 1.0))

    def _register_tag_feedback(
        self,
        guild_id: int,
        tags: List[str],
        multiplier: float,
        duration: float,
        decay: float,
    ) -> None:
        if not tags:
            return
        bucket = self._tag_feedback.setdefault(guild_id, {})
        now = self._now()
        for raw_tag in tags:
            tag = self._normalize_tag(raw_tag)
            entries = bucket.setdefault(tag, [])
            entries.append(
                {
                    "multiplier": float(multiplier),
                    "applied_at": now,
                    "duration": duration,
                    "decay_rate": decay,
                }
            )

    def _get_tag_feedback_multiplier(self, guild_id: int, tags: List[str]) -> float:
        if not tags:
            return 1.0
        bucket = self._tag_feedback.get(guild_id)
        if not bucket:
            return 1.0
        now = self._now()
        result = 1.0
        for raw_tag in tags:
            tag = self._normalize_tag(raw_tag)
            entries = bucket.get(tag)
            if not entries:
                continue
            keep: List[Dict[str, Any]] = []
            tag_multiplier = 1.0
            for entry in entries:
                effective = self._decayed_multiplier(entry, now)
                if (
                    entry.get("duration")
                    and (now - entry.get("applied_at", now)) >= entry["duration"]
                ):
                    continue
                if abs(effective - 1.0) > 0.02:
                    keep.append(entry)
                tag_multiplier *= effective
            if keep:
                bucket[tag] = keep
            else:
                bucket.pop(tag, None)
            result *= tag_multiplier
        return max(0.1, min(4.0, result))

    def _register_artist_feedback(
        self,
        guild_id: int,
        artist_norm: str,
        multiplier: float,
        duration: float,
        decay: float,
        additive: float = 0.0,
    ) -> None:
        if not artist_norm:
            return
        bucket = self._artist_feedback.setdefault(guild_id, {})
        entries = bucket.setdefault(artist_norm, [])
        entries.append(
            {
                "multiplier": float(multiplier),
                "applied_at": self._now(),
                "duration": duration,
                "decay_rate": decay,
                "additive": float(additive),
            }
        )

    def _get_artist_feedback_adjustments(
        self, guild_id: int, artist_norm: str
    ) -> Tuple[float, float]:
        bucket = self._artist_feedback.get(guild_id)
        if not bucket or not artist_norm:
            return 1.0, 0.0
        entries = bucket.get(artist_norm)
        if not entries:
            return 1.0, 0.0
        now = self._now()
        multiplier = 1.0
        additive = 0.0
        keep: List[Dict[str, Any]] = []
        for entry in entries:
            effective = self._decayed_multiplier(entry, now)
            additive_component = float(entry.get("additive", 0.0))
            elapsed = now - entry.get("applied_at", now)
            if entry.get("duration") and elapsed >= entry["duration"]:
                continue
            if abs(effective - 1.0) > 0.02 or abs(additive_component) > 1e-3:
                keep.append(entry)
                multiplier *= effective
                additive += additive_component * (
                    entry.get("decay_rate", 1.0) ** (elapsed / 60.0)
                )
        if keep:
            bucket[artist_norm] = keep
        else:
            bucket.pop(artist_norm, None)
        return max(0.1, min(3.0, multiplier)), max(-0.5, min(0.5, additive))

    def _decay_guild_preferences(self, prefs: Dict[str, Any]) -> None:
        now = self._now()
        last = float(prefs.get("last_updated", now))
        if now <= last:
            return
        elapsed_days = (now - last) / 86400.0
        decay_factor = GUILD_PREFERENCE_DECAY ** max(0.0, elapsed_days)
        for key in ("tags", "artists"):
            store = prefs.setdefault(key, {})
            for item in list(store.keys()):
                store[item] *= decay_factor
                if abs(store[item]) < 0.05:
                    store.pop(item, None)
        prefs["last_updated"] = now

    def _update_guild_preferences(
        self,
        guild_id: int,
        tags: List[str],
        artist_norm: str,
        signal: float,
        *,
        primary_listener_bias: bool = DJ_LISTENER_BIAS,
    ) -> None:
        if not guild_id or not tags and not artist_norm:
            return
        prefs = self._guild_preferences.setdefault(
            guild_id,
            {"tags": {}, "artists": {}, "last_updated": self._now()},
        )
        self._decay_guild_preferences(prefs)
        weight = 1.25 if primary_listener_bias else 1.0
        adjusted_signal = signal * weight
        for raw_tag in tags[:5]:
            tag = self._normalize_tag(raw_tag)
            prefs.setdefault("tags", {})[tag] = (
                prefs["tags"].get(tag, 0.0) + adjusted_signal
            )
        if artist_norm:
            prefs.setdefault("artists", {})[artist_norm] = (
                prefs["artists"].get(artist_norm, 0.0) + adjusted_signal
            )
        prefs["last_updated"] = self._now()

    def _get_guild_preference_multiplier(
        self, guild_id: int, tags: List[str], artist_norm: str
    ) -> float:
        prefs = self._guild_preferences.get(guild_id)
        if not prefs:
            return 1.0
        now = self._now()
        total = 0.0
        count = 0
        self._decay_guild_preferences(prefs)
        for raw_tag in tags[:5]:
            tag = self._normalize_tag(raw_tag)
            if tag in prefs.get("tags", {}):
                total += prefs["tags"][tag]
                count += 1
        if artist_norm and artist_norm in prefs.get("artists", {}):
            total += prefs["artists"][artist_norm]
            count += 1
        if count == 0:
            return 1.0
        avg = total / count
        multiplier = 1.0 + max(-0.4, min(0.4, avg * 0.03))
        return max(0.5, min(1.6, multiplier))

    @staticmethod
    def _session_profile_decay(delta_seconds: float) -> float:
        if delta_seconds <= 0.0:
            return 1.0
        exponent = -math.log(2.0) * (delta_seconds / SESSION_GENRE_SENTIMENT_HALFLIFE)
        return math.exp(exponent)

    def _decay_session_genre_sentiment(
        self, guild_id: int, now: Optional[float] = None
    ) -> None:
        profile = self._session_genre_sentiment.get(guild_id)
        if not profile:
            return
        tags_map: Dict[str, Dict[str, float]] = profile.get("tags", {})
        if not tags_map:
            return
        now = now or self._now()
        last_decay = float(profile.get("last_decay", profile.get("updated", now)))
        elapsed = now - last_decay
        if elapsed < 15.0:
            return
        decay_factor = self._session_profile_decay(elapsed)
        if decay_factor >= 0.999:
            profile["last_decay"] = now
            return
        to_remove: List[str] = []
        for tag, entry in tags_map.items():
            entry["pos"] = float(entry.get("pos", 0.0)) * decay_factor
            entry["neg"] = float(entry.get("neg", 0.0)) * decay_factor
            if (
                entry["pos"] < SESSION_GENRE_SENTIMENT_MIN
                and entry["neg"] < SESSION_GENRE_SENTIMENT_MIN
            ):
                to_remove.append(tag)
        for tag in to_remove:
            tags_map.pop(tag, None)
        profile["last_decay"] = now

    def _update_session_genre_sentiment(
        self,
        guild_id: int,
        tags: List[str],
        outcome: str,
        ratio: float,
        primary_listener_bias: bool,
    ) -> None:
        if not tags:
            return
        now = self._now()
        profile = self._session_genre_sentiment.setdefault(
            guild_id, {"tags": {}, "updated": now, "last_decay": now}
        )
        self._decay_session_genre_sentiment(guild_id, now)
        tags_map: Dict[str, Dict[str, float]] = profile.setdefault("tags", {})
        bias_multiplier = 1.2 if primary_listener_bias else 1.0

        outcome_weights = {
            "hard_skip": (0.0, 1.35),
            "medium_skip": (0.0, 1.0),
            "late_skip": (0.0, 0.55),
            "finish": (1.0 + ratio * 0.6, 0.0),
        }
        pos_delta, neg_delta = outcome_weights.get(outcome, (0.0, 0.0))
        if outcome == "finish" and ratio > 0.99:
            pos_delta *= 1.15
        pos_delta *= bias_multiplier
        neg_delta *= bias_multiplier * (1.0 + max(0.0, 0.6 - ratio))

        for raw_tag in tags[:5]:
            tag = self._normalize_tag(raw_tag)
            entry = tags_map.setdefault(tag, {"pos": 0.0, "neg": 0.0})
            entry["pos"] = float(entry.get("pos", 0.0)) + pos_delta
            entry["neg"] = float(entry.get("neg", 0.0)) + neg_delta
        profile["updated"] = now

    def _get_session_genre_multiplier(self, guild_id: int, tags: List[str]) -> float:
        if not tags:
            return 1.0
        profile = self._session_genre_sentiment.get(guild_id)
        if not profile:
            return 1.0
        self._decay_session_genre_sentiment(guild_id)
        tags_map: Dict[str, Dict[str, float]] = profile.get("tags", {})
        if not tags_map:
            return 1.0
        total_sentiment = 0.0
        considered = 0
        for raw_tag in tags[:5]:
            tag = self._normalize_tag(raw_tag)
            entry = tags_map.get(tag)
            if not entry:
                continue
            pos = float(entry.get("pos", 0.0))
            neg = float(entry.get("neg", 0.0))
            total = pos + neg
            if total <= 0.0:
                continue
            sentiment = (pos - neg) / total
            total_sentiment += sentiment
            considered += 1
        if considered == 0:
            return 1.0
        avg_sentiment = total_sentiment / considered
        multiplier = 1.0 + max(
            -SESSION_GENRE_SENTIMENT_MAX_PENALTY,
            min(SESSION_GENRE_SENTIMENT_MAX_BONUS, avg_sentiment * 0.5),
        )
        return max(0.25, min(1.7, multiplier))

    def _get_session_disliked_tag(self, guild_id: int) -> Tuple[Optional[str], float]:
        profile = self._session_genre_sentiment.get(guild_id)
        if not profile:
            return None, 0.0
        self._decay_session_genre_sentiment(guild_id)
        tags_map: Dict[str, Dict[str, float]] = profile.get("tags", {})
        if not tags_map:
            return None, 0.0
        worst_tag = None
        worst_sentiment = 0.0
        for tag, entry in tags_map.items():
            pos = float(entry.get("pos", 0.0))
            neg = float(entry.get("neg", 0.0))
            total = pos + neg
            if total <= 0.0:
                continue
            sentiment = (pos - neg) / total
            if sentiment < worst_sentiment:
                worst_sentiment = sentiment
                worst_tag = tag
        return worst_tag, worst_sentiment

    def _record_feedback_metric(
        self,
        guild_id: int,
        track_key: str,
        outcome: str,
        progress_ratio: float,
        user_id: Optional[int] = None,
    ) -> None:
        bucket = self._metrics_bucket(guild_id)
        feedback_history: Deque[Dict[str, Any]] = bucket.setdefault(
            "feedback_events", deque(maxlen=METRICS_HISTORY_LIMIT)
        )
        feedback_history.append(
            {
                "timestamp": self._now(),
                "track_key": track_key,
                "outcome": outcome,
                "progress": progress_ratio,
                "user_id": user_id,
            }
        )

    async def record_playback_feedback(
        self,
        guild_id: int,
        artist: str,
        track: str,
        progress_ratio: float,
        *,
        duration_ms: Optional[int] = None,
        primary_listener_bias: bool = DJ_LISTENER_BIAS,
    ) -> None:
        if not guild_id or not artist or not track:
            return

        ratio = max(0.0, min(1.0, progress_ratio))
        if ratio < SKIP_EARLY_THRESHOLD:
            outcome = "hard_skip"
        elif ratio < SKIP_THRESHOLD:
            outcome = "medium_skip"
        elif ratio < FINISH_THRESHOLD:
            outcome = "late_skip"
        else:
            outcome = "finish"

        track_key = self._make_track_key(artist, track)
        artist_norm = self._normalize_artist_for_diversity(artist)

        metadata: Optional[Dict[str, Any]] = None
        async with self._metadata_lock:
            metadata = self._metadata_cache.get(track_key)
        if not metadata:
            try:
                metadata = await self._get_track_profile(
                    artist, track, require_enrichment=False
                )
            except Exception as exc:
                LOG.debug(
                    f"⚠️ [Autoplay] Unable to refresh metadata for feedback ({artist} - {track}): {exc}"
                )
                metadata = None

        tags: List[str] = []
        if metadata:
            raw_tags = metadata.get("tags", []) or []
            tags = [
                self._normalize_tag(str(tag))
                for tag in raw_tags
                if isinstance(tag, str)
            ]

        self._update_session_genre_sentiment(
            guild_id, tags, outcome, ratio, primary_listener_bias
        )

        if outcome in ("hard_skip", "medium_skip") and tags:
            primary_genre = tags[0]
            skip_count = self._note_genre_skip(guild_id, primary_genre)
            if LOG_LEVEL >= 2 and skip_count >= 2:
                LOG.debug(
                    "[Feedback] Consecutive skip #%s for genre '%s' (guild=%s)",
                    skip_count,
                    primary_genre,
                    guild_id,
                )
        elif outcome == "finish" and tags:
            self._reset_genre_skip_count(guild_id, tags[0])

        candidate_multiplier = CANDIDATE_MULTIPLIERS.get(outcome, 1.0)
        if outcome == "finish":
            extra = max(0.0, ratio - FINISH_THRESHOLD) / max(
                0.001, 1.0 - FINISH_THRESHOLD
            )
            candidate_multiplier *= 1.0 + (extra * 0.25)

        self._register_candidate_feedback(guild_id, track_key, candidate_multiplier)

        if outcome in ("hard_skip", "medium_skip", "finish") and tags:
            tag_multiplier, duration, decay = SESSION_TAG_MULTIPLIERS[outcome]
            self._register_tag_feedback(guild_id, tags, tag_multiplier, duration, decay)

        if outcome in ("hard_skip", "medium_skip", "finish"):
            multiplier, duration, decay, additive = SESSION_ARTIST_MULTIPLIERS[outcome]
            self._register_artist_feedback(
                guild_id, artist_norm, multiplier, duration, decay, additive
            )

        signal_map = {
            "hard_skip": -1.25,
            "medium_skip": -0.9,
            "late_skip": -0.4,
            "finish": 1.0 + ratio * 0.5,
        }
        self._update_guild_preferences(
            guild_id,
            tags,
            artist_norm,
            signal_map.get(outcome, 0.0),
            primary_listener_bias=primary_listener_bias,
        )

        stats = self._guild_skip_stats.setdefault(
            guild_id, {"events": deque(maxlen=200)}
        )
        stats["events"].append((self._now(), outcome, ratio))
        self._adjust_exploration_rate(guild_id, outcome, max(0.3, ratio))

        self._record_feedback_metric(guild_id, track_key, outcome, ratio)

        if LOG_LEVEL >= 2:
            LOG.debug(
                f"🎯 [Feedback] {outcome} → {artist} - {track} (ratio={ratio:.2f}, multiplier={candidate_multiplier:.2f})"
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

    def _parse_gemini_json_any(self, response: Any) -> Optional[Any]:
        """Extract the first valid JSON payload (dict or list) from a Gemini response."""

        for candidate_text in self._collect_gemini_texts(response):
            if not isinstance(candidate_text, str):
                continue
            cleaned = self._strip_code_fence(candidate_text)
            if not cleaned:
                continue
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                object_start = cleaned.find("{")
                object_end = cleaned.rfind("}")
                array_start = cleaned.find("[")
                array_end = cleaned.rfind("]")
                candidates: List[str] = []
                if (
                    object_start != -1
                    and object_end != -1
                    and object_end > object_start
                ):
                    candidates.append(cleaned[object_start : object_end + 1])
                if array_start != -1 and array_end != -1 and array_end > array_start:
                    candidates.append(cleaned[array_start : array_end + 1])
                for snippet in candidates:
                    try:
                        return json.loads(snippet)
                    except json.JSONDecodeError:
                        continue
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
            if isinstance(data, dict):
                tag_source = data.get("toptags") or {}
                tag_list = tag_source.get("tag") if isinstance(tag_source, dict) else []
                if isinstance(tag_list, dict):
                    tag_list = [tag_list]
                if isinstance(tag_list, list):
                    for tag_obj in tag_list[:10]:
                        if isinstance(tag_obj, dict):
                            tag_name = tag_obj.get("name")
                            if tag_name:
                                tags.append(self._normalize_tag(tag_name))
        return tags

    async def _execute_gemini_prompt(
        self,
        instructions: str,
        prompt: str,
        allow_grounding: bool,
    ) -> Optional[Any]:
        if not self._ensure_gemini_ready():
            LOG.debug("⚠️ [Gemini] All API keys exhausted or on cooldown; skipping.")
            return None

        if not self._gemini_available or not self._gemini_client:
            return None

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
            except Exception as exc:
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
                LOG.debug(f"⚠️ [Gemini] Request failed: {message}")
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
            return None

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
            return None

        if use_grounding:
            self._register_grounded_call()

        return response

    async def _schedule_gemini_batch_flush(self) -> None:
        try:
            await asyncio.sleep(GEMINI_BATCH_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        await self._flush_gemini_batch()

    async def _flush_gemini_batch(self) -> None:
        async with self._gemini_batch_lock:
            batch = self._gemini_batch_queue
            self._gemini_batch_queue = []
            self._gemini_batch_task = None

        if not batch:
            return

        if LOG_LEVEL >= 3:
            keys_preview = [entry.get("key") for entry in batch]
            LOG.debug(
                f"🧠 [Gemini] Flushing batch of {len(batch)} request(s): {keys_preview}"
            )

        try:
            result_map = await self._process_gemini_batch(batch)
        except Exception as exc:
            LOG.error(f"❌ [Gemini] Batch processing failure: {exc}")
            result_map = {}

        for entry in batch:
            future: asyncio.Future = entry["future"]
            track_key = entry["key"]
            payload = result_map.get(track_key, {})
            if not future.done():
                future.set_result(payload)

    async def _process_gemini_batch(
        self, batch_entries: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        if not batch_entries:
            return {}

        if (
            not self._ensure_gemini_ready()
            or not self._gemini_available
            or not self._gemini_client
        ):
            return {entry["key"]: {} for entry in batch_entries}

        allow_grounding = any(entry.get("allow_grounding") for entry in batch_entries)

        tracks_payload = [
            {
                "id": entry["key"],
                "artist": entry["artist"],
                "track": entry["track"],
                "existing_tags": entry.get("tags", []),
            }
            for entry in batch_entries
        ]

        instructions = (
            "You are a music metadata enrichment assistant. Given a batch of tracks, "
            "return JSON with a 'results' array. Each entry must include: id (copy "
            "the provided id), tags (list of canonical genres), moods (1-3 mood descriptors), "
            "and energy (single word). Normalize genres to common labels. Use existing tags "
            "as hints when helpful."
        )

        prompt = (
            "Input Tracks:\n"
            f"{json.dumps({'tracks': tracks_payload}, ensure_ascii=False, indent=2)}\n"
            "Respond with JSON only."
        )

        if LOG_LEVEL >= 3:
            track_debug = [track.get("id") for track in tracks_payload]
            LOG.debug(
                f"🧠 [Gemini] Batch prompt built for tracks: {track_debug}",
            )
            LOG.debug(
                f"🧠 [Gemini] Instructions:\n{instructions}",
            )
            LOG.debug(
                f"🧠 [Gemini] Prompt body (truncated):\n{prompt[:1500]}",
            )

        response = await self._execute_gemini_prompt(
            instructions, prompt, allow_grounding
        )

        if response is None:
            return {entry["key"]: {} for entry in batch_entries}

        if LOG_LEVEL >= 3:
            raw_texts = self._collect_gemini_texts(response)
            LOG.debug(
                f"🧠 [Gemini] Raw response snippet(s): {[self._strip_code_fence(text)[:400] for text in raw_texts[:3]]}",
            )

        structured = self._parse_gemini_json(response)
        parsed_payload: Any
        if structured is not None:
            parsed_payload = structured
        else:
            parsed_payload = self._parse_gemini_json_any(response)

        if parsed_payload is None:
            texts = self._collect_gemini_texts(response)
            if texts:
                preview = self._strip_code_fence(texts[0])[:200]
                LOG.debug(
                    f"⚠️ [Gemini] Failed to parse batch JSON response. Raw: {preview}"
                )
            return {entry["key"]: {} for entry in batch_entries}

        records: List[Dict[str, Any]] = []
        if isinstance(parsed_payload, dict):
            candidate = parsed_payload.get("results")
            if isinstance(candidate, list):
                records = [record for record in candidate if isinstance(record, dict)]
            elif isinstance(candidate, dict):
                records = [
                    value for value in candidate.values() if isinstance(value, dict)
                ]
            else:
                records = [parsed_payload]
        elif isinstance(parsed_payload, list):
            records = [record for record in parsed_payload if isinstance(record, dict)]

        result_map: Dict[str, Dict[str, Any]] = {}
        unmatched_keys = [entry["key"] for entry in batch_entries]
        fallback_records: List[Dict[str, Any]] = []

        for record in records:
            track_id = (
                record.get("id")
                or record.get("track_id")
                or record.get("key")
                or record.get("trackKey")
            )
            payload = {
                "tags": record.get("tags") or [],
                "moods": record.get("moods") or [],
                "energy": record.get("energy"),
            }
            if track_id and track_id in unmatched_keys:
                result_map[track_id] = payload
                unmatched_keys.remove(track_id)
            else:
                fallback_records.append(payload)

        for key, record in zip(unmatched_keys, fallback_records):
            result_map[key] = record

        for key in unmatched_keys[len(fallback_records) :]:
            result_map.setdefault(key, {})

        LOG.debug(
            f"✅ [Gemini] Batch metadata response received for {len(batch_entries)} track(s)."
        )

        if LOG_LEVEL >= 3:
            LOG.debug(f"🧠 [Gemini] Parsed metadata map: {result_map}")

        return result_map

    async def _enqueue_gemini_metadata_future(
        self,
        artist: str,
        track: str,
        existing_tags: List[str],
        allow_grounding: bool,
    ) -> asyncio.Future:
        """Enqueue a Gemini metadata request and return the future (for batch accumulation)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        entry = {
            "artist": artist,
            "track": track,
            "tags": existing_tags,
            "allow_grounding": allow_grounding,
            "key": self._make_track_key(artist, track),
            "future": future,
        }

        async with self._gemini_batch_lock:
            self._gemini_batch_queue.append(entry)
            should_flush_immediately = len(self._gemini_batch_queue) >= GEMINI_BATCH_MAX
            if LOG_LEVEL >= 3:
                LOG.debug(
                    f"🧠 [Gemini] Enqueued metadata request for {entry['key']} | queue={len(self._gemini_batch_queue)}"
                )
            if should_flush_immediately:
                if self._gemini_batch_task and not self._gemini_batch_task.done():
                    self._gemini_batch_task.cancel()
                self._gemini_batch_task = asyncio.create_task(
                    self._flush_gemini_batch()
                )
            elif not self._gemini_batch_task or self._gemini_batch_task.done():
                self._gemini_batch_task = asyncio.create_task(
                    self._schedule_gemini_batch_flush()
                )

        return future

    async def _enqueue_gemini_metadata_request(
        self,
        artist: str,
        track: str,
        existing_tags: List[str],
        allow_grounding: bool,
    ) -> Dict[str, Any]:
        """Enqueue a Gemini metadata request and await the result (single track convenience)."""
        future = await self._enqueue_gemini_metadata_future(
            artist, track, existing_tags, allow_grounding
        )
        try:
            return await asyncio.wait_for(future, timeout=GEMINI_BATCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if not future.done():
                future.set_result({})
            return {}

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

        enriched = await self._enqueue_gemini_metadata_request(
            artist,
            track,
            existing_tags or [],
            allow_grounding,
        )

        if not enriched:
            return {}

        tags = [
            self._normalize_tag(tag)
            for tag in enriched.get("tags", [])
            if isinstance(tag, str) and tag.strip()
        ]
        moods = [
            mood.strip()
            for mood in enriched.get("moods", [])
            if isinstance(mood, str) and mood.strip()
        ][:5]
        energy_value = enriched.get("energy")
        if isinstance(energy_value, str):
            energy_value = energy_value.strip()
        else:
            energy_value = None

        return {
            "tags": tags,
            "moods": moods,
            "energy": energy_value,
        }

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

    def can_recommend(self) -> bool:
        """Check if recommendations are available (same as is_available for V1)."""
        return self.is_available()

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
        source_counts: Counter[str] = Counter()

        similar_tracks = await self._get_similar_tracks(
            artist, title, limit=CANDIDATE_POOL_TARGET
        )
        for track in similar_tracks:
            candidate_artist = track.get("artist", {})
            if isinstance(candidate_artist, dict):
                candidate_artist = candidate_artist.get("name", "")
            candidate_title = track.get("name") or track.get("title") or ""
            source = track.get("source", "similar")
            candidate = self._make_candidate_entry(
                guild_id,
                candidate_artist,
                candidate_title,
                source,
            )
            if candidate and candidate["track_key"] not in seen:
                seen.add(candidate["track_key"])
                pool.append(candidate)
                source_counts[source] += 1

        if len(pool) < CANDIDATE_POOL_TARGET:
            top_tracks = await self._get_artist_top_tracks(
                artist, exclude_titles=[title], limit=10
            )
            for track in top_tracks:
                candidate_artist = track.get("artist", {})
                if isinstance(candidate_artist, dict):
                    candidate_artist = candidate_artist.get("name", "")
                candidate_title = track.get("name") or track.get("title") or ""
                source = track.get("source", "artist-top")
                candidate = self._make_candidate_entry(
                    guild_id,
                    candidate_artist,
                    candidate_title,
                    source,
                )
                if candidate and candidate["track_key"] not in seen:
                    seen.add(candidate["track_key"])
                    pool.append(candidate)
                    source_counts[source] += 1

        if len(pool) < CANDIDATE_POOL_TARGET:
            fallback_tracks = await self._get_fallback_recommendations(
                artist, limit=CANDIDATE_POOL_TARGET
            )
            for track in fallback_tracks:
                candidate_artist = track.get("artist", {})
                if isinstance(candidate_artist, dict):
                    candidate_artist = candidate_artist.get("name", "")
                candidate_title = track.get("name") or track.get("title") or ""
                source = track.get("source", "fallback")
                candidate = self._make_candidate_entry(
                    guild_id,
                    candidate_artist,
                    candidate_title,
                    source,
                )
                if candidate and candidate["track_key"] not in seen:
                    seen.add(candidate["track_key"])
                    pool.append(candidate)
                    source_counts[source] += 1

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
                    source = track.get("source", "tag")
                    candidate = self._make_candidate_entry(
                        guild_id,
                        candidate_artist,
                        candidate_title,
                        source,
                    )
                    if candidate and candidate["track_key"] not in seen:
                        seen.add(candidate["track_key"])
                        pool.append(candidate)
                        source_counts[source] += 1
                    if len(pool) >= CANDIDATE_POOL_TARGET:
                        break
                if len(pool) >= CANDIDATE_POOL_TARGET:
                    break

        label_map = {
            "similar": "Similar",
            "artist-top": "ArtistTop",
            "fallback": "Fallback",
            "tag": "Tag",
        }
        source_summary = ", ".join(
            f"{label_map.get(source, source)}: {count}"
            for source, count in source_counts.most_common()
        )
        tag_summary = "none"
        if seed_tags:
            tag_summary = f"{len(seed_tags)} → {', '.join(seed_tags)}"

        LOG.info(
            "Built candidate pool: %s tracks (Seed Factors → Artist Seed: %s | Tag Seeds: %s | Source breakdown: %s)",
            len(pool),
            artist or "unknown",
            tag_summary,
            source_summary or "none",
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
        recent_tags = (
            session_tags_raw[-GENRE_DIVERSITY_WINDOW:] if session_tags_raw else []
        )
        dominant_tag: Optional[str] = None
        dominant_ratio = 0.0
        dominant_count = 0
        diversity_pressure = 0.0
        if recent_tags:
            recent_counter = Counter(recent_tags)
            dominant_tag, dominant_count = recent_counter.most_common(1)[0]
            dominant_ratio = dominant_count / max(len(recent_tags), 1)
            if (
                dominant_count >= GENRE_DOMINANCE_MIN_COUNT
                and dominant_ratio >= GENRE_DOMINANCE_THRESHOLD
            ):
                diversity_pressure = min(
                    EPSILON_DIVERSITY_BOOST_MAX,
                    (dominant_ratio - GENRE_DOMINANCE_THRESHOLD) * 0.6
                    + 0.02 * (dominant_count / max(len(recent_tags), 1)),
                )
        disliked_tag: Optional[str] = None
        disliked_sentiment = 0.0
        disliked_tag, disliked_sentiment = self._get_session_disliked_tag(guild_id)
        if (
            disliked_tag
            and disliked_sentiment <= SESSION_GENRE_SENTIMENT_PENALTY_THRESHOLD
        ):
            additional = max(0.0, abs(disliked_sentiment) * 0.35 + 0.04)
            remaining = max(0.0, EPSILON_DIVERSITY_BOOST_MAX - diversity_pressure)
            diversity_increment = min(additional, remaining)
            if diversity_increment > 0:
                diversity_pressure += diversity_increment

        explore_state = self._get_exploration_state(guild_id)
        epsilon_baseline = float(explore_state.get("epsilon", EPSILON_BASE))
        epsilon_rate = epsilon_baseline
        if diversity_pressure > 0.0:
            epsilon_rate = min(EPSILON_MAX, epsilon_rate + diversity_pressure)

        diversity_factor = self._compute_progressive_diversity_factor(guild_id)
        epsilon_rate *= diversity_factor

        state = self._get_exploration_state(guild_id)
        skip_map: Dict[str, int] = state.get("consecutive_genre_skips", {})
        escape_trigger_genre: Optional[str] = None
        for genre, count in skip_map.items():
            if count >= SESSION_GENRE_SKIP_ESCALATION_THRESHOLD:
                escape_trigger_genre = genre
                break

        if disliked_tag:
            explore_state["last_disliked_tag"] = disliked_tag
            explore_state["last_disliked_strength"] = disliked_sentiment

        metadata_map: Dict[str, Dict[str, Any]] = {}
        cached_hit_count = 0
        cache_snapshot: Dict[str, Dict[str, Any]] = {}
        async with self._metadata_lock:
            if self._metadata_cache:
                cache_snapshot = self._metadata_cache.copy()
        if cache_snapshot:
            for candidate in candidate_pool:
                track_key = candidate["track_key"]
                entry = cache_snapshot.get(track_key)
                if entry and not self._should_refresh_metadata(entry):
                    metadata_map[track_key] = entry
                    cached_hit_count += 1

        if LOG_LEVEL >= 2 and cached_hit_count:
            LOG.debug(
                "[Metadata] Reused %s cached profiles prior to enrichment (pool=%s)",
                cached_hit_count,
                len(candidate_pool),
            )

        enrichment_cutoff = min(len(candidate_pool), ENRICH_TOP_N)
        if LOG_LEVEL >= 2:
            autoplay_count = int(explore_state.get("autoplay_count", 0))
            LOG.debug(
                "[Ranking] Reducing candidate size: start=%s → enrichment_top=%s (epsilon=%.3f → %.3f, diversity_boost=%.3f, diversity_factor=%.2f, autoplay_count=%s, dominant_tag=%s, disliked_tag=%s/%.2f, escape_trigger=%s)",
                len(candidate_pool),
                enrichment_cutoff,
                epsilon_baseline,
                epsilon_rate,
                diversity_pressure,
                diversity_factor,
                autoplay_count,
                dominant_tag or "none",
                disliked_tag or "none",
                disliked_sentiment,
                escape_trigger_genre or "none",
            )

        # Phase 1: Collect all tracks that need Gemini enrichment
        top_for_enrichment = candidate_pool[:ENRICH_TOP_N]
        tracks_needing_enrichment: List[Tuple[str, str, str, bool]] = (
            []
        )  # (track_key, artist, track, require_enrichment)

        for idx, candidate in enumerate(top_for_enrichment):
            track_key = candidate["track_key"]
            artist = candidate["artist"]
            track = candidate["track"]
            require_enrichment = idx < 3

            if track_key in metadata_map:
                continue

            # Check cache (fall back to disk snapshot if needed)
            cache_entry: Optional[Dict[str, Any]] = None
            if cache_snapshot:
                cache_entry = cache_snapshot.get(track_key)
            if cache_entry is None:
                async with self._metadata_lock:
                    cache_entry = self._metadata_cache.get(track_key)
            if cache_entry and not self._should_refresh_metadata(cache_entry):
                metadata_map[track_key] = cache_entry
                continue

            # Need enrichment - add to batch list
            tracks_needing_enrichment.append(
                (track_key, artist, track, require_enrichment)
            )

        # Phase 2: Fetch ALL Last.fm tags in parallel (fast)
        lastfm_fetch_tasks = {}
        for track_key, artist, track, require_enrichment in tracks_needing_enrichment:
            lastfm_fetch_tasks[track_key] = (
                asyncio.create_task(self._fetch_track_tags_from_lastfm(artist, track)),
                artist,
                track,
                require_enrichment,
            )

        # Await all Last.fm fetches together
        lastfm_results: Dict[str, Tuple[List[str], str, str, bool]] = {}
        for track_key, (
            task,
            artist,
            track,
            require_enrichment,
        ) in lastfm_fetch_tasks.items():
            try:
                tags = await task
                lastfm_results[track_key] = (tags, artist, track, require_enrichment)
            except Exception as e:
                LOG.warning(f"⚠️ [Last.fm] Failed to fetch tags for {track_key}: {e}")
                lastfm_results[track_key] = ([], artist, track, require_enrichment)

        # Phase 3: Enqueue all Gemini requests at once (batch accumulation)
        enrichment_futures: Dict[str, Tuple[asyncio.Future, List[str], str, str]] = {}
        enqueue_tasks: List[Tuple[str, str, str, List[str], bool]] = []
        enqueued_track_keys = set()

        cache_updates: List[Tuple[str, Dict[str, Any]]] = []
        for track_key, (
            tags,
            artist,
            track,
            require_enrichment,
        ) in lastfm_results.items():
            base_metadata: Dict[str, Any] = {
                "artist": artist,
                "track": track,
                "tags": tags,
                "moods": [],
                "energy": None,
                "sources": ["lastfm"] if tags else [],
                "timestamp": time.time(),
            }
            metadata_map[track_key] = base_metadata
            cache_updates.append((track_key, base_metadata))

            need_gemini = require_enrichment or len(tags) < 3
            if need_gemini:
                enqueue_tasks.append(
                    (track_key, artist, track, tags, require_enrichment)
                )
                enqueued_track_keys.add(track_key)

        if cache_updates:
            async with self._metadata_lock:
                now = time.time()
                for track_key, metadata in cache_updates:
                    metadata["timestamp"] = now
                    self._metadata_cache[track_key] = metadata
                self._maybe_flush_metadata_cache()

        # Phase 3b: ensure remaining candidates have baseline metadata without blocking batches
        additional_candidates: List[Tuple[str, str, str]] = []
        for candidate in candidate_pool:
            track_key = candidate["track_key"]
            if track_key in metadata_map:
                continue

            cache_entry: Optional[Dict[str, Any]] = None
            if cache_snapshot:
                cache_entry = cache_snapshot.get(track_key)
            if cache_entry is None:
                async with self._metadata_lock:
                    cache_entry = self._metadata_cache.get(track_key)
            if cache_entry and not self._should_refresh_metadata(cache_entry):
                metadata_map[track_key] = cache_entry
                continue

            additional_candidates.append(
                (track_key, candidate["artist"], candidate["track"])
            )

        if additional_candidates:
            fetch_tasks: Dict[str, Tuple[asyncio.Task, str, str]] = {}
            for track_key, artist, track in additional_candidates:
                fetch_tasks[track_key] = (
                    asyncio.create_task(
                        self._fetch_track_tags_from_lastfm(artist, track)
                    ),
                    artist,
                    track,
                )

            supplemental_cache_updates: List[Tuple[str, Dict[str, Any]]] = []
            for track_key, (task, artist, track) in fetch_tasks.items():
                try:
                    tags = await task
                except Exception as e:
                    LOG.warning(
                        f"⚠️ [Last.fm] Failed to fetch tags for {track_key} (secondary batch): {e}"
                    )
                    tags = []

                base_metadata = {
                    "artist": artist,
                    "track": track,
                    "tags": tags,
                    "moods": [],
                    "energy": None,
                    "sources": ["lastfm"] if tags else [],
                    "timestamp": time.time(),
                }
                metadata_map[track_key] = base_metadata
                supplemental_cache_updates.append((track_key, base_metadata))

                need_gemini = len(tags) < 3
                if need_gemini and track_key not in enqueued_track_keys:
                    enqueue_tasks.append((track_key, artist, track, tags, False))
                    enqueued_track_keys.add(track_key)

            if supplemental_cache_updates:
                async with self._metadata_lock:
                    now = time.time()
                    for track_key, metadata in supplemental_cache_updates:
                        metadata["timestamp"] = now
                        self._metadata_cache[track_key] = metadata
                    self._maybe_flush_metadata_cache()

        # Now enqueue all at once using gather to minimize delays
        if enqueue_tasks:
            futures_list = await asyncio.gather(
                *[
                    self._enqueue_gemini_metadata_future(
                        artist, track, tags or [], allow_grounding=require_enrichment
                    )
                    for track_key, artist, track, tags, require_enrichment in enqueue_tasks
                ]
            )

            for idx, (track_key, artist, track, tags, _) in enumerate(enqueue_tasks):
                enrichment_futures[track_key] = (futures_list[idx], tags, artist, track)

        # Phase 4: Wait for batch to accumulate and flush
        if enrichment_futures:
            await asyncio.sleep(GEMINI_BATCH_DELAY_SECONDS + 0.1)

            # Now await all futures (batch should have flushed by now)
            for track_key, (future, tags, artist, track) in enrichment_futures.items():
                try:
                    enriched = await asyncio.wait_for(
                        future, timeout=GEMINI_BATCH_TIMEOUT_SECONDS
                    )

                    # Build metadata from Last.fm + Gemini
                    metadata: Dict[str, Any] = {
                        "artist": artist,
                        "track": track,
                        "tags": tags,
                        "moods": [],
                        "energy": None,
                        "sources": ["lastfm"] if tags else [],
                        "timestamp": time.time(),
                    }

                    if enriched:
                        gemini_tags = [
                            self._normalize_tag(tag)
                            for tag in enriched.get("tags", [])
                            if isinstance(tag, str)
                        ]
                        merged_tags = (
                            list(dict.fromkeys(tags + gemini_tags))
                            if tags
                            else gemini_tags
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

                    # Cache and store
                    async with self._metadata_lock:
                        metadata["timestamp"] = time.time()
                        self._metadata_cache[track_key] = metadata
                    metadata_map[track_key] = metadata

                except asyncio.TimeoutError:
                    LOG.warning(
                        f"⚠️ [Gemini] Metadata enrichment timed out for {track_key}"
                    )
                except Exception as e:
                    LOG.warning(
                        f"⚠️ [Gemini] Metadata enrichment failed for {track_key}: {e}"
                    )

        # Phase 5: Score all candidates using enriched metadata
        scored_candidates: List[Dict[str, Any]] = []
        now_ts = time.time()

        if diversity_pressure > 0.0:
            now_mark = self._now()
            last_notice = float(explore_state.get("last_diversity_notice", 0.0))
            if LOG_LEVEL >= 2 and (now_mark - last_notice) > 20.0:
                LOG.debug(
                    "[Diversity] Dominant tag '%s' seen %d/%d recent autoplay picks (ratio=%.2f). Boosting exploration by %.3f.",
                    dominant_tag,
                    dominant_count,
                    len(recent_tags),
                    dominant_ratio,
                    diversity_pressure,
                )
                explore_state["last_diversity_notice"] = now_mark
            else:
                explore_state["last_diversity_notice"] = max(
                    explore_state.get("last_diversity_notice", 0.0), now_mark
                )
            if dominant_tag:
                explore_state["last_dominant_tag"] = dominant_tag

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
                # Get from cache only (no new Gemini enrichment)
                metadata = await self._get_track_profile(
                    artist, track, require_enrichment=False
                )
                metadata_map[track_key] = metadata

            candidate_tags = metadata.get("tags", [])
            quality_score = candidate.get("quality_score", 0.5)

            artist_norm = self._normalize_artist_for_diversity(artist)
            artist_count = session_counts.get(artist_norm, 0)
            base_artist_affinity = self._clamp01(
                1.0 - (artist_count / max(ARTIST_REPEAT_LIMIT, 1))
            )

            tag_feedback_multiplier = self._get_tag_feedback_multiplier(
                guild_id, candidate_tags
            )

            seed_overlap = 0.0
            if seed_tags and candidate_tags:
                seed_overlap = len(set(seed_tags) & set(candidate_tags)) / max(
                    len(seed_tags), 1
                )
                seed_overlap = self._clamp01(seed_overlap * tag_feedback_multiplier)

            session_overlap = 0.0
            if session_tag_set and candidate_tags:
                session_overlap = len(set(candidate_tags) & session_tag_set) / max(
                    len(session_tag_set), 1
                )
                session_overlap = self._clamp01(
                    session_overlap * tag_feedback_multiplier
                )

            artist_multiplier, artist_additive = self._get_artist_feedback_adjustments(
                guild_id, artist_norm
            )
            artist_affinity = self._clamp01(
                base_artist_affinity * artist_multiplier + artist_additive
            )
            novelty_base = 1.0 - max(seed_overlap, session_overlap)
            if diversity_pressure > 0.0 and dominant_tag:
                if dominant_tag in candidate_tags:
                    novelty_base -= diversity_pressure * 0.6
                else:
                    novelty_base += diversity_pressure * 0.85
            novelty = self._clamp01(novelty_base + self._rng.uniform(0.0, epsilon_rate))
            content_sim = seed_overlap
            session_coherence = session_overlap

            base_score = (
                content_sim * 0.45
                + artist_affinity * 0.18
                + session_coherence * 0.12
                + quality_score * 0.15
                + novelty * 0.10
            )

            if diversity_pressure > 0.0 and dominant_tag:
                if dominant_tag in candidate_tags:
                    base_score -= diversity_pressure * 0.35
                else:
                    base_score += diversity_pressure * 0.25

            if escape_trigger_genre and escape_trigger_genre in candidate_tags:
                base_score -= 0.75
                if LOG_LEVEL >= 2:
                    LOG.debug(
                        "[Ranking] Escape penalty applied to %s - %s (genre=%s, consecutive_skips≥%s)",
                        artist,
                        track,
                        escape_trigger_genre,
                        SESSION_GENRE_SKIP_ESCALATION_THRESHOLD,
                    )

            if (
                disliked_tag
                and disliked_sentiment <= SESSION_GENRE_SENTIMENT_PENALTY_THRESHOLD
                and disliked_tag in candidate_tags
            ):
                base_score -= abs(disliked_sentiment) * 0.35

            candidate_multiplier = self._get_candidate_feedback_multiplier(
                guild_id, track_key
            )
            guild_pref_multiplier = self._get_guild_preference_multiplier(
                guild_id, candidate_tags, artist_norm
            )
            session_genre_multiplier = self._get_session_genre_multiplier(
                guild_id, candidate_tags
            )

            base_score *= (
                candidate_multiplier * guild_pref_multiplier * session_genre_multiplier
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

        scored_candidates.sort(key=lambda x: x["score"], reverse=True)

        if LOG_LEVEL >= 2 and scored_candidates:
            preview_limit = min(10, len(scored_candidates))
            top_preview = [
                (c["artist"], c["track"], round(c["score"], 3))
                for c in scored_candidates[:preview_limit]
            ]
            LOG.debug(
                "[Ranking] Finding Top K candidates: target=%s (resolve_k=%s, epsilon=%.3f, diversity_boost=%.3f) | preview=%s",
                min(limit, RESOLVE_TOP_K),
                RESOLVE_TOP_K,
                epsilon_rate,
                diversity_pressure,
                top_preview,
            )

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
                    if len(genre_track) > GENRE_HISTORY_LIMIT:
                        del genre_track[:-GENRE_HISTORY_LIMIT]

                self._increment_autoplay_count(guild_id)
            else:
                self._mark_resolve_failure(
                    guild_id, candidate["artist"], candidate["track"]
                )
                if LOG_LEVEL >= 1:
                    LOG.warning(
                        "⚠️ [AutoPlay] Failed to resolve candidate '%s - %s' to playable track (guild=%s, reason=youtube_search_empty)",
                        candidate["artist"],
                        candidate["track"],
                        guild_id,
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
            if isinstance(j, dict):
                similar_section = (
                    j.get("similarartists")
                    if isinstance(j.get("similarartists"), dict)
                    else {}
                )
                sim = (
                    similar_section.get("artist", [])
                    if isinstance(similar_section, dict)
                    else []
                )
                if isinstance(sim, dict):
                    sim = [sim]
                if isinstance(sim, list):
                    for a in sim:
                        if isinstance(a, dict):
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
                if not isinstance(r, dict) or isinstance(r, Exception):
                    continue
                toptracks_section = (
                    r.get("toptracks") if isinstance(r.get("toptracks"), dict) else {}
                )
                tracks = (
                    toptracks_section.get("track", [])
                    if isinstance(toptracks_section, dict)
                    else []
                )
                if isinstance(tracks, dict):
                    tracks = [tracks]
                if isinstance(tracks, list):
                    for t in tracks[:5]:  # Increased from 3 to 5
                        if not isinstance(t, dict):
                            continue
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
                if isinstance(jtags, dict):
                    tag_section = (
                        jtags.get("toptags")
                        if isinstance(jtags.get("toptags"), dict)
                        else {}
                    )
                    t = (
                        tag_section.get("tag", [])
                        if isinstance(tag_section, dict)
                        else []
                    )
                    if isinstance(t, dict):
                        t = [t]
                    if isinstance(t, list):
                        for tg in t[:3]:
                            if isinstance(tg, dict):
                                name = tg.get("name")
                                if name:
                                    tags.append(name)

                for tag in tags:
                    jtag = await self._lastfm_get(
                        {"method": "tag.getTopTracks", "tag": tag, "limit": 5}, session
                    )
                    if not isinstance(jtag, dict):
                        continue
                    tag_tracks_section = (
                        jtag.get("tracks")
                        if isinstance(jtag.get("tracks"), dict)
                        else {}
                    )
                    tracks = (
                        tag_tracks_section.get("track", [])
                        if isinstance(tag_tracks_section, dict)
                        else []
                    )
                    if isinstance(tracks, dict):
                        tracks = [tracks]
                    if isinstance(tracks, list):
                        for tr in tracks[:5]:
                            if not isinstance(tr, dict):
                                continue
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
                            if not isinstance(jtag, dict):
                                continue
                            tag_tracks_section = (
                                jtag.get("tracks")
                                if isinstance(jtag.get("tracks"), dict)
                                else {}
                            )
                            tracks = (
                                tag_tracks_section.get("track", [])
                                if isinstance(tag_tracks_section, dict)
                                else []
                            )
                            if isinstance(tracks, dict):
                                tracks = [tracks]
                            if isinstance(tracks, list):
                                for tr in tracks[:5]:
                                    if not isinstance(tr, dict):
                                        continue
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

        if not isinstance(data, dict):
            return results

        toptracks_section = (
            data.get("toptracks") if isinstance(data.get("toptracks"), dict) else {}
        )
        tracks = (
            toptracks_section.get("track", [])
            if isinstance(toptracks_section, dict)
            else []
        )
        if isinstance(tracks, dict):
            tracks = [tracks]

        if not isinstance(tracks, list):
            return results

        for track in tracks[: limit * 2]:
            if not isinstance(track, dict):
                continue
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

                    if not isinstance(data, dict):
                        LOG.error(
                            f"Last.fm API error for track '{artist} - {track}': unexpected response type {type(data).__name__}"
                        )
                        return []

                    # Check for API errors
                    if "error" in data:
                        error_code = data.get("error", "unknown")
                        error_msg = data.get("message", "Unknown error")
                        LOG.error(
                            f"Last.fm API error {error_code}: {error_msg} for track '{artist} - {track}'"
                        )
                        return []

                    # Extract similar tracks
                    similar_section = data.get("similartracks")
                    if not isinstance(similar_section, dict):
                        LOG.warning(
                            f"⚠️ [Last.fm] No 'similartracks' payload for '{artist}' - '{track}'."
                        )
                        return []

                    similar_tracks = similar_section.get("track", [])

                    # Handle case where only 1 track returned (not a list)
                    if isinstance(similar_tracks, dict):
                        similar_tracks = [similar_tracks]

                    if not isinstance(similar_tracks, list):
                        LOG.warning(
                            f"⚠️ [Last.fm] Malformed similar tracks list for '{artist}' - '{track}'."
                        )
                        return []

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
                if LOG_LEVEL >= 1:
                    LOG.warning(
                        "⚠️ [YouTube] No search results for query '%s' (artist=%s, track=%s)",
                        query,
                        artist,
                        track,
                    )
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
                self._cache[cache_key] = (query, time.time())
                self._maybe_flush_mapping_cache()

                return best_match

            if LOG_LEVEL >= 1:
                LOG.warning(
                    "⚠️ [YouTube] No acceptable match for '%s - %s' (best_score=%.2f, threshold=0.3)",
                    artist,
                    track,
                    best_score,
                )
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
            score -= 0.4

        info = getattr(track_obj, "info", {}) or {}
        author = str(info.get("author") or getattr(track_obj, "author", ""))
        author_lower = author.lower()
        if author_lower:
            if any(hint in author_lower for hint in CANONICAL_CHANNEL_HINTS):
                score += 0.07
            if " - topic" in author_lower:
                score += 0.04

        view_count = info.get("viewCount") or info.get("views")
        if isinstance(view_count, (int, float)):
            normalized = min(max(view_count, 0) / 2_500_000.0, 1.0)
            score += 0.05 * normalized

        like_ratio = info.get("likeRatio")
        if isinstance(like_ratio, (int, float)):
            score += 0.03 * max(0.0, min(1.0, like_ratio))

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
