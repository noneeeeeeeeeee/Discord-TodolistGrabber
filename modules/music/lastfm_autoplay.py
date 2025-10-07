import os
import asyncio
import time
import logging
import json
import re
from collections import Counter
from typing import List, Optional, Tuple, Dict, Any
from pathlib import Path

import aiohttp
from google import genai
from google.genai import types

LOG = logging.getLogger(__name__)


# Replace logger methods with print wrappers so messages always appear on stdout
def _install_print_logger(logger: logging.Logger) -> None:
    """Override common logger methods to print to stdout for visibility."""

    def _make(method_name: str):
        def _m(*args, **kwargs):
            try:
                if args:
                    msg = args[0]
                else:
                    msg = ""
                print(f"[{method_name.upper()}] {msg}")
            except Exception:
                pass

        return _m

    for _n in ("debug", "info", "warning", "error", "critical"):
        setattr(logger, _n, _make(_n))


_install_print_logger(LOG)
# Configuration
CACHE_MAX_AGE = 60 * 60 * 24 * 15
HISTORY_LIMIT = 50
DURATION_TOLERANCE_SECONDS = 5
DURATION_TOLERANCE_PERCENT = 0.12
METADATA_CACHE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
GEMINI_MODEL = "gemini-2.5-flash-lite"

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
    "hq",
    "hd",
    "4k",
    "lossless",
    "remastered",  # sometimes official remaster is desired
    "explicit",  # indicates official release (may be part of title)
)

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
)

# Last.fm API Configuration
LASTFM_API_BASE = "http://ws.audioscrobbler.com/2.0/"
LASTFM_API_KEY_ENV = "LASTFM_API_KEY"

# Gemini API Configuration
GEMINI_API_KEY_ENV = "GeminiApiKey"


class LastFMAutoplay:
    """Manages Last.fm-based autoplay recommendations with Gemini AI parsing."""

    def __init__(self, bot):
        self.bot = bot
        self.api_key: Optional[str] = None
        self._cache_dir = Path("cache/music")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "lastfm_mappings.json"
        self._cache: Dict[str, Tuple[str, float]] = {}
        self._recent_autoplayed: Dict[int, List[Dict[str, str]]] = (
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

        # Initialize Last.fm API
        self._init_lastfm_client()
        self._init_gemini()
        self._load_cache()
        self._load_metadata_cache()
        # History is now loaded per-guild on-demand

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
            gemini_api_key = os.getenv(GEMINI_API_KEY_ENV)
            if not gemini_api_key:
                LOG.warning(
                    f"⚠️ [Last.fm AutoPlay] {GEMINI_API_KEY_ENV} not found in .env. "
                    "Gemini parsing disabled; falling back to heuristic parsing."
                )
                LOG.warning("Get a free API key from https://ai.google.dev/")
                self._gemini_available = False
                return

            self._gemini_client = genai.Client(api_key=gemini_api_key)
            self._gemini_search_tool = types.Tool(google_search=types.GoogleSearch())
            self._gemini_base_config = types.GenerateContentConfig(
                tools=[self._gemini_search_tool]
            )
            self._gemini_available = True

        except Exception as e:
            LOG.error(
                f"❌ [Last.fm AutoPlay] Failed to initialize Gemini: {e}. Heuristic parsing will be used instead."
            )
            self._gemini_client = None
            self._gemini_search_tool = None
            self._gemini_base_config = None
            self._gemini_available = False

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
        except Exception as e:
            LOG.warning(f"Failed to load track metadata cache: {e}")

    def _save_metadata_cache(self) -> None:
        """Persist enriched track metadata to disk."""
        try:
            with open(self._metadata_cache_file, "w", encoding="utf-8") as f:
                json.dump(self._metadata_cache, f, indent=2)
        except Exception as e:
            LOG.warning(f"Failed to save track metadata cache: {e}")

    @staticmethod
    def _make_track_metadata_key(artist: str, track: str) -> str:
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
    ) -> Dict[str, Any]:
        """Use Gemini with Google Search grounding to enrich track metadata."""
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

        def _call_gemini() -> Optional[Any]:
            try:
                return self._gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=(
                            [self._gemini_search_tool]
                            if self._gemini_search_tool
                            else None
                        ),
                        system_instruction=instructions,
                    ),
                )
            except Exception as exc:  # Rate limits or network errors
                LOG.debug(f"⚠️ [Gemini] Metadata enrichment failed: {exc}")
                return None

        response = await asyncio.to_thread(_call_gemini)
        if response is None:
            return {}

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

        metadata_key = self._make_track_metadata_key(artist, track)

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
            enriched = await self._classify_track_with_gemini(artist, track, tags)
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
            self._save_metadata_cache()

        return metadata

    def _load_guild_history(self, guild_id: int) -> None:
        """Load recommendation history for a specific guild from disk."""
        history_file = self._cache_dir / f"{guild_id}_history.json"

        if not history_file.exists():
            return

        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._recent_autoplayed[guild_id] = data.get("history", [])
        except Exception as e:
            LOG.warning(f"Failed to load history for guild {guild_id}: {e}")

    def _save_guild_history(self, guild_id: int) -> None:
        """Save recommendation history for a specific guild to disk."""
        if guild_id not in self._recent_autoplayed:
            return

        history_file = self._cache_dir / f"{guild_id}_history.json"

        try:
            data = {
                "guild_id": guild_id,
                "history": self._recent_autoplayed[guild_id],
                "last_updated": time.time(),
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
        # Remove common collaborator separators (order matters - check longest first)
        for sep in [", ", " & ", " ft. ", " feat. ", " featuring ", " x "]:
            if sep in normalized:
                # Take the first (primary) artist
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
                # Save cache every 10 additions
                if len(self._cache) % 10 == 0:
                    self._save_cache()

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
        """
        Get track recommendations based on current track using Gemini AI parsing.

        Uses Gemini to intelligently parse artist/title from YouTube metadata,
        then queries Last.fm for similar tracks.

        Args:
            track_info: Dict with 'title', 'author', 'length' keys
            limit: Maximum number of recommendations to return

        Returns:
            List of (track_identifier, playable_track) tuples
        """
        if not self.is_available():
            LOG.debug(
                "Last.fm autoplay not available (Gemini or Last.fm not configured)"
            )
            return []

        raw_title = track_info.get("title", "").strip()
        channel_name = track_info.get("author", "").strip()
        expected_duration_ms = track_info.get("length")

        if not raw_title:
            LOG.warning("⚠️ [Last.fm] Missing track title")
            return []

        LOG.info(
            f"🎵 [Last.fm] Getting recommendations for: '{raw_title}' by '{channel_name}'"
        )

        # Phase 1: Parse track with Gemini AI
        parsed = await self._parse_track_with_gemini(raw_title, channel_name)

        if not parsed or not parsed.get("artist") or not parsed.get("title"):
            LOG.error(
                f"❌ [Last.fm] Gemini failed to parse track: '{raw_title}'. "
                "AutoPlay cannot continue without valid artist/title."
            )
            return []

        artist = parsed["artist"]
        title = parsed["title"]

        LOG.debug(f"🎧 [Last.fm] Using parsed artist/title: '{artist}' - '{title}'")

        # Phase 2: Build a blended recommendation pool from multiple sources
        recommendations_pool: List[Dict[str, Any]] = []

        # Load guild history early to check artist frequency
        guild_id = track_info.get("guild_id", 0)
        if guild_id not in self._recent_autoplayed:
            self._load_guild_history(guild_id)
            if guild_id not in self._recent_autoplayed:
                self._recent_autoplayed[guild_id] = []

        # Check how many times this artist has been played recently
        normalized_current_artist = self._normalize_artist_for_diversity(artist)
        recent_artist_count = sum(
            1
            for hist_entry in self._recent_autoplayed[guild_id]
            if self._normalize_artist_for_diversity(hist_entry.get("artist", ""))
            == normalized_current_artist
        )

        similar_tracks = await self._get_similar_tracks(artist, title, limit=limit * 2)

        # Only fetch same-artist tracks if artist hasn't been played much recently
        artist_top_tracks = []
        if (
            recent_artist_count < 2
        ):  # Only if artist played less than 2 times in recent history
            artist_top_tracks = await self._get_artist_top_tracks(
                artist, exclude_titles=[title], limit=1
            )

        # Interleave similar tracks with same-artist picks for variety
        blended_primary: List[Dict[str, Any]] = []
        max_primary = max(len(similar_tracks), len(artist_top_tracks))
        for i in range(max_primary):
            if i < len(similar_tracks):
                blended_primary.append(similar_tracks[i])
            if i < len(artist_top_tracks):
                blended_primary.append(artist_top_tracks[i])

        recommendations_pool.extend(blended_primary)

        # ALWAYS get fallback to ensure diverse artist pool
        fallback_tracks = await self._get_fallback_recommendations(
            artist, limit=limit * 3  # Get more fallback candidates
        )
        if fallback_tracks:
            recommendations_pool.extend(fallback_tracks)

        if not recommendations_pool:
            LOG.warning(
                f"⚠️ [Last.fm] No recommendations available for '{artist}' - '{title}' after blending sources"
            )

            # Try to get recommendations based on track's genre tags
            track_tags = await self._get_track_tags(artist, title)
            if track_tags:
                LOG.info(
                    f"🎵 [Last.fm] Found {len(track_tags)} genre tags for track, using for fallback"
                )
                for tag in track_tags[:3]:  # Try top 3 tags
                    genre_tracks = await self._get_tag_top_tracks(tag, limit=limit * 2)
                    if genre_tracks:
                        LOG.info(
                            f"✅ [Last.fm] Found {len(genre_tracks)} tracks from genre '{tag}'"
                        )
                        recommendations_pool.extend(genre_tracks)
                        break

            # If still no tracks, try default genre as last resort
            if not recommendations_pool:
                default_genre = "pop"
                genre_tracks = await self._get_tag_top_tracks(
                    default_genre, limit=limit
                )
                if genre_tracks:
                    LOG.warning(
                        f"⚠️ [Last.fm] Using default genre '{default_genre}' for fallback recommendations."
                    )
                    recommendations_pool.extend(genre_tracks)
                else:
                    LOG.warning(
                        f"❌ [Last.fm] No recommendations found even for default genre '{default_genre}'. Likely not music."
                    )
                    return []

        recommendations = []
        seen_tracks = set()

        # Build artist frequency map from recent history (normalized for variations)
        recent_artists = {}
        if guild_id in self._recent_autoplayed:
            for hist_entry in self._recent_autoplayed[guild_id]:
                hist_artist = hist_entry.get("artist", "")
                normalized = self._normalize_artist_for_diversity(hist_artist)
                recent_artists[normalized] = recent_artists.get(normalized, 0) + 1

        # Initialize cooldown tracking for this guild if not exists
        if guild_id not in self._artist_cooldowns:
            self._artist_cooldowns[guild_id] = {}

        scored_candidates = []
        for similar_track in recommendations_pool:
            similar_artist = similar_track.get("artist", {})
            if isinstance(similar_artist, dict):
                similar_artist = similar_artist.get("name", "")

            similar_name = similar_track.get("name", "")
            if not similar_name:
                similar_name = similar_track.get("title", "")

            if not similar_artist or not similar_name:
                continue

            artist_lower = similar_artist.lower()
            track_lower = similar_name.lower()

            # Skip if this is the currently playing track
            if artist_lower == artist.lower() and track_lower == title.lower():
                continue

            # Check if already in recent history (exact track match)
            track_key = f"{artist_lower}:{track_lower}"
            if guild_id in self._recent_autoplayed:
                already_played = any(
                    h.get("artist", "").lower() == artist_lower
                    and h.get("track", "").lower() == track_lower
                    for h in self._recent_autoplayed[guild_id]
                )
                if already_played:
                    continue

            normalized_artist = self._normalize_artist_for_diversity(similar_artist)
            artist_repeat_count = recent_artists.get(normalized_artist, 0)
            cooldown_remaining = self._artist_cooldowns[guild_id].get(
                normalized_artist, 0
            )

            diversity_penalty = 0
            if cooldown_remaining > 0:
                diversity_penalty = 1000 + cooldown_remaining * 100
            elif artist_repeat_count >= 2:
                diversity_penalty = 500 + artist_repeat_count * 50
            elif artist_repeat_count == 1:
                diversity_penalty = 50

            # Retrieve enriched metadata (tags/mood) for scoring
            track_profile = await self._get_track_profile(
                similar_artist, similar_name, require_enrichment=True
            )
            candidate_tags = track_profile.get("tags", [])

            session_tags = []
            if guild_id in self._genre_history and self._genre_history[guild_id]:
                session_top = Counter(self._genre_history[guild_id]).most_common(5)
                session_tags = [tag for tag, _ in session_top]

            tag_overlap = 0.0
            novelty = 0.5
            if candidate_tags and session_tags:
                overlap = len(set(candidate_tags) & set(session_tags))
                tag_overlap = overlap / max(len(session_tags), 1)
                novelty = max(0.0, min(1.0, 1.0 - tag_overlap))
            elif candidate_tags:
                tag_overlap = 0.5
                novelty = 0.7

            if artist_repeat_count == 0:
                artist_diversity_factor = 1.0
            elif artist_repeat_count == 1:
                artist_diversity_factor = 0.5
            else:
                artist_diversity_factor = 0.0

            # Encourage genre-based sources slightly when session history exists
            track_source = similar_track.get("source", "")
            if session_tags and track_source in {
                "similar-genre",
                "tag-top-track",
                "genre-fallback",
            }:
                novelty = min(1.0, novelty + 0.1)

            similarity_score = (
                (tag_overlap * 0.5) + (artist_diversity_factor * 0.3) + (novelty * 0.2)
            )

            scored_candidates.append(
                {
                    "artist": similar_artist,
                    "track": similar_name,
                    "track_key": track_key,
                    "diversity_penalty": diversity_penalty,
                    "similarity_score": similarity_score,
                    "source": track_source or "unknown",
                    "tags": candidate_tags,
                }
            )

        # Sort by penalty first (lower better), then by similarity score (higher better)
        scored_candidates.sort(
            key=lambda x: (x["diversity_penalty"], -x["similarity_score"])
        )

        # Resolve top candidates to playable tracks
        for candidate in scored_candidates:
            if len(recommendations) >= limit:
                break

            similar_artist = candidate["artist"]
            similar_name = candidate["track"]
            track_key = candidate["track_key"]

            # Deduplicate in this result set
            if track_key in seen_tracks:
                continue
            seen_tracks.add(track_key)

            # Try to resolve to YouTube
            playable_track = await self._resolve_lastfm_to_youtube(
                similar_artist,
                similar_name,
                duration_ms=expected_duration_ms,
            )

            if playable_track:
                recommendations.append((track_key, playable_track))

                # Add to history with artist and track info
                if guild_id not in self._recent_autoplayed:
                    self._recent_autoplayed[guild_id] = []
                self._recent_autoplayed[guild_id].append(
                    {"artist": similar_artist, "track": similar_name}
                )

                # Trim history to last N tracks
                if len(self._recent_autoplayed[guild_id]) > HISTORY_LIMIT:
                    self._recent_autoplayed[guild_id] = self._recent_autoplayed[
                        guild_id
                    ][-HISTORY_LIMIT:]

                # Update artist cooldown tracking (Spotify-like algorithm)
                normalized_selected = self._normalize_artist_for_diversity(
                    similar_artist
                )
                recent_artists[normalized_selected] = (
                    recent_artists.get(normalized_selected, 0) + 1
                )

                # Decrement cooldown for all artists except the one just played
                for artist_key in list(self._artist_cooldowns[guild_id].keys()):
                    if artist_key != normalized_selected:
                        self._artist_cooldowns[guild_id][artist_key] = max(
                            0, self._artist_cooldowns[guild_id][artist_key] - 1
                        )
                        # Remove artist from tracking if cooldown is 0
                        if self._artist_cooldowns[guild_id][artist_key] == 0:
                            del self._artist_cooldowns[guild_id][artist_key]

                # Update cooldown for the artist just played
                artist_play_count = recent_artists.get(normalized_selected, 0) + 1
                if artist_play_count >= 2:
                    self._artist_cooldowns[guild_id][normalized_selected] = 3
                elif normalized_selected in self._artist_cooldowns[guild_id]:
                    self._artist_cooldowns[guild_id][normalized_selected] = 0

                # Track primary genre to avoid getting stuck in a single lane
                track_tags = candidate.get("tags") or []
                if not track_tags:
                    profile = await self._get_track_profile(
                        similar_artist, similar_name
                    )
                    track_tags = profile.get("tags", [])

                if track_tags:
                    if guild_id not in self._genre_history:
                        self._genre_history[guild_id] = []
                    self._genre_history[guild_id].append(track_tags[0])
                    if len(self._genre_history[guild_id]) > 20:
                        self._genre_history[guild_id] = self._genre_history[guild_id][
                            -20:
                        ]

        # Save cache and guild history if we added new mappings
        if recommendations:
            self._save_cache()
            self._save_guild_history(guild_id)

        return recommendations

    async def _get_track_tags(self, artist: str, track: str) -> List[str]:
        """Get normalized tags for a specific track (cached + enriched)."""
        profile = await self._get_track_profile(artist, track)
        return profile.get("tags", [])

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
