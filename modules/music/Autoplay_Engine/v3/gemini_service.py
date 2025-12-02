import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Any, Dict, List, Optional

# google-genai provides the official Gemini client.
try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None

LOG = logging.getLogger(__name__)

GEMINI_KEYS_ENV = "GeminiApiKeys"
DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_DAILY_LIMIT = 500
RATE_LIMIT_COOLDOWN_SECONDS = 600
AUTH_FAILURE_COOLDOWN_SECONDS = 3600
ENRICHMENT_BATCH_MAX = 50
ENRICHMENT_BATCH_DELAY_SECONDS = 8.0
ENRICHMENT_BATCH_TIMEOUT_SECONDS = 15.0
REQUESTS_PER_MINUTE_LIMIT = 15
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_SAFETY_MARGIN = 0.25
MAX_GENERATE_ATTEMPTS = 5
GROUNDING_MODEL_NAME = "gemini-2.5-flash"


class GroundingQuotaManager:
    """
    Manages Google Grounding quota across multiple API keys.
    
    FREE tier: 500 grounding requests per day per key.
    With 3 keys: 1,500 grounding requests per day total.
    Quota resets at midnight Pacific Time.
    """

    def __init__(self, api_keys: List[str], quota_per_key: int = 500):
        """
        Initialize quota manager.
        
        Args:
            api_keys: List of Gemini API keys for rotation
            quota_per_key: Daily grounding quota per key (default: 500 for FREE tier)
        """
        self.api_keys = api_keys
        self.quota_per_key = quota_per_key
        self.daily_quota = quota_per_key * len(api_keys)
        self.used_today = 0
        self.current_key_index = 0
        self.reset_time = self._get_next_midnight_pacific()
        
        LOG.info(
            f"🔑 GroundingQuotaManager initialized: {len(api_keys)} keys, "
            f"{self.daily_quota} total grounding requests/day"
        )

    def can_use_grounding(self) -> bool:
        """Check if grounding quota is available."""
        if datetime.now() >= self.reset_time:
            self._reset_quota()
        return self.used_today < self.daily_quota

    def get_next_api_key(self) -> str:
        """
        Rotate through API keys for load balancing.
        
        Returns:
            Next API key in rotation
        """
        key = self.api_keys[self.current_key_index]
        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        return key

    def increment_usage(self) -> None:
        """Track grounding usage."""
        self.used_today += 1
        LOG.info(f"📊 Grounding quota: {self.used_today}/{self.daily_quota} used today")

    def _get_next_midnight_pacific(self) -> datetime:
        """Calculate next midnight Pacific Time for quota reset."""
        from datetime import timezone, timedelta
        
        # Pacific Time is UTC-8 (PST) or UTC-7 (PDT)
        # For simplicity, use UTC-8 as baseline
        pacific_offset = timedelta(hours=-8)
        pacific_tz = timezone(pacific_offset)
        
        now_pacific = datetime.now(pacific_tz)
        midnight_pacific = now_pacific.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        
        # Convert back to local time for comparison
        return midnight_pacific.astimezone().replace(tzinfo=None)

    def _reset_quota(self) -> None:
        """Reset quota at midnight Pacific Time."""
        self.used_today = 0
        self.reset_time = self._get_next_midnight_pacific()
        LOG.info(
            f"🔄 Grounding quota reset: {self.daily_quota} requests available, "
            f"next reset at {self.reset_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )


@dataclass
class EnrichmentRequest:
    artist: str
    title: str
    tags: List[str]
    allow_grounding: bool
    key: str
    future: asyncio.Future


class GeminiQuotaExceeded(Exception):
    """Raised when the daily Gemini quota is exhausted."""


class GeminiService:
    """Managed interface for Gemini calls with key rotation and quota tracking."""

    def __init__(
        self,
        cache_dir: Path | str = Path("cache/music"),
        *,
        model: str = DEFAULT_MODEL,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._usage_file = self._cache_dir / "gemini_usage.json"
        self._model = model
        self._daily_limit = max(1, daily_limit)

        self._keys: List[str] = self._load_api_keys()
        self._key_index: int = 0
        self._key_cooldowns: Dict[str, float] = {key: 0.0 for key in self._keys}
        self._client: Any = None
        self._active_key: Optional[str] = None
        self._available: bool = False
        self._status: str = "uninitialized"

        self._usage_state: Dict[str, Any] = {"date": self._today_key(), "count": 0}
        self._load_usage_state()

        self._lock = asyncio.Lock()
        self._initialize_client()

        self._batch_queue: List[EnrichmentRequest] = []
        self._batch_lock = asyncio.Lock()
        self._batch_task: Optional[asyncio.Task] = None
        self._batch_max = ENRICHMENT_BATCH_MAX
        self._batch_delay = ENRICHMENT_BATCH_DELAY_SECONDS
        self._batch_timeout = ENRICHMENT_BATCH_TIMEOUT_SECONDS
        self._rpm_limit = REQUESTS_PER_MINUTE_LIMIT
        self._rate_window = RATE_LIMIT_WINDOW_SECONDS
        self._request_history = deque()
        
        # Phase 0.5: Initialize grounding quota manager with multi-API-key support
        self._grounding_quota_manager = GroundingQuotaManager(
            api_keys=self._keys,
            quota_per_key=500  # FREE tier: 500 grounding requests/day/key
        )
        LOG.info(
            f"🔑 [GeminiService] Initialized with {len(self._keys)} API key(s), "
            f"{self._grounding_quota_manager.daily_quota} grounding requests/day total"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def status(self) -> str:
        return self._status

    @property
    def quota_remaining(self) -> int:
        self._reset_usage_if_needed()
        remaining = self._daily_limit - int(self._usage_state.get("count", 0))
        return max(0, remaining)
    
    def can_use_grounding(self) -> bool:
        """Check if grounding quota is available (Phase 0.5)."""
        return self._grounding_quota_manager.can_use_grounding()
    
    def use_grounding_quota(self) -> None:
        """Increment grounding usage counter (Phase 0.5)."""
        self._grounding_quota_manager.increment_usage()

    async def parse_track_metadata(
        self,
        raw_title: str,
        channel_name: str,
    ) -> Optional[Dict[str, str]]:
        """Parse artist/title from YouTube metadata using Gemini."""

        prompt = self._build_track_prompt(raw_title, channel_name)
        response = await self._generate(prompt)
        if not response:
            return None

        raw_payload = self._extract_json_dict(response)
        if not isinstance(raw_payload, dict):
            return None

        # DEBUG: Log raw Gemini response for OST detection debugging
        raw_track_type = raw_payload.get("track_type")
        raw_entity = raw_payload.get("primary_entity")
        if raw_track_type and raw_track_type != "music":
            LOG.info(
                f"🎬 [OST Detection Debug] Track: '{raw_title}' | "
                f"Channel: '{channel_name}' | "
                f"Gemini returned track_type='{raw_track_type}', primary_entity='{raw_entity}'"
            )

        # Extract artist and title
        raw_artist = raw_payload.get("artist")
        artist = str(raw_artist).strip() if raw_artist else None
        title = str(raw_payload.get("title", "")).strip()

        # Title is required, artist can be null for OST content
        if not title:
            return None

        # Extract and validate track_type
        track_type = str(raw_payload.get("track_type", "music")).strip().lower()
        if track_type not in {"music", "ost", "game_soundtrack", "anime_opening"}:
            LOG.warning(
                f"⚠️ [OST Detection] Invalid track_type '{track_type}' returned by Gemini for '{raw_title}', "
                f"falling back to 'music'"
            )
            track_type = "music"

        # Extract primary_entity (only valid if track_type is NOT "music")
        primary_entity = None
        if track_type != "music":
            raw_entity = raw_payload.get("primary_entity")
            if raw_entity and isinstance(raw_entity, str):
                primary_entity = raw_entity.strip() or None
            # If OST but no entity, fallback to "music"
            if not primary_entity:
                LOG.warning(
                    f"⚠️ [OST Detection] track_type='{track_type}' but no valid primary_entity for '{raw_title}', "
                    f"falling back to 'music'"
                )
                track_type = "music"

        # For standard music, artist is required
        if track_type == "music" and not artist:
            return None

        # Log final classification
        if track_type != "music":
            LOG.info(
                f"✅ [OST Classification] '{artist or 'Unknown'} - {title}' classified as track_type='{track_type}', "
                f"primary_entity='{primary_entity}'"
            )

        payload: Dict[str, str] = {
            "artist": artist or "",
            "title": title,
            "track_type": track_type,
        }
        if primary_entity:
            payload["primary_entity"] = primary_entity
        return payload

    async def generate_deezer_queries_lite(
        self,
        title: str,
        failed_queries: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Stage 1: Use Flash-Lite (no thinking) to generate 9 Deezer queries.

        Returns 3 sets of 3 variations:
        - Set A (Artist-focused): Focus on canonical artist name
        - Set B (Entity-focused): Focus on franchise/anime/game name
        - Set C (Title-focused): Focus on clean title without suffixes

        Args:
            title: Raw YouTube title to parse
            failed_queries: Optional list of previously failed queries (for retry logic)

        Returns:
            Dict with 'queries' (list of 9 strings) and 'track_type' (str)
        """
        prompt = f"""Generate 9 Deezer search queries for: "{title}"

Return 3 sets of 3 variations each:

Set A - Artist-focused (if artist is a real person/band, not a franchise):
1. [real_artist_name] [clean_title]
2. [real_artist_name] [clean_title] [album_hint]
3. [primary_artist_if_collaboration] [clean_title]

Set B - Entity-focused (if artist is a franchise/anime/game):
1. [franchise_name] [clean_title] soundtrack
2. [franchise_name] OST [clean_title]
3. [franchise_name] [clean_title] original

Set C - Title-focused (clean title variations):
1. [clean_title] [year_if_known]
2. [clean_title] [language] version
3. [clean_title] official

Rules:
- Extract actual artist/band names, NOT placeholders like "PERSON" or "ARTIST"
- Remove YouTube suffixes (Official Audio, Lyric Video, Nightcore, etc.)
- Expand acronyms (JJK → Jujutsu Kaisen)
- For covers, use original artist
- Remove pipes, season markers, "ft.", "sing-along"

Return JSON: {{"queries": ["query1", "query2", ...], "track_type": "music|ost|anime_opening|game_soundtrack"}}
"""

        # Use Flash-Lite model directly (no thinking capability)
        async with self._lock:
            if not self._ensure_client():
                return None

            try:
                self._reserve_quota(1)
            except GeminiQuotaExceeded:
                LOG.warning("⚠️ Gemini daily quota exhausted")
                return None

            if self._client is None:
                LOG.error("❌ Gemini client not available")
                return None

            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model="gemini-2.5-flash-lite",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ) if types else None,
                )
            except Exception as e:
                LOG.error(f"❌ Flash-Lite query generation failed: {e}")
                return None

        payload = self._extract_json_dict(response)
        if not isinstance(payload, dict):
            return None

        queries = payload.get("queries", [])
        track_type = str(payload.get("track_type", "music")).strip().lower()

        # Validate we got 9 queries
        if not isinstance(queries, list) or len(queries) != 9:
            LOG.warning(
                f"⚠️ Flash-Lite returned {len(queries) if isinstance(queries, list) else 0} queries "
                f"instead of 9 for '{title}'"
            )
            return None

        LOG.info(f"✅ Flash-Lite generated 9 Deezer queries for '{title}' (track_type={track_type})")
        return {"queries": queries, "track_type": track_type}

    async def generate_deezer_queries_grounded(
        self,
        title: str,
        failed_queries: List[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Stage 2: Use Flash + Google Grounding + Thinking to generate 3 refined queries.
        
        Grounding provides web-sourced canonical names for OST/anime content.
        Thinking enables multi-step reasoning about artist attribution.

        Args:
            title: Raw YouTube title to parse
            failed_queries: List of queries that failed in Stage 1

        Returns:
            Dict with 'queries' (list of 3 strings), 'confidence', and 'reasoning'
        """
        if genai is None or types is None:
            LOG.error("google-genai not available, cannot use grounding")
            return None

        prompt = f"""Previous Deezer queries failed: {failed_queries}

Use Google Search to find the CANONICAL artist/album for: "{title}"

Think through:
1. Is this OST (game/anime soundtrack)?
2. Is this a cover/remix (use original artist)?
3. Is this a collaboration (who's the primary artist)?
4. Are there multiple artists with this name?

Then generate 3 refined Deezer queries:
1. [canonical_artist_from_web] [canonical_title_from_web]
2. [canonical_artist] [canonical_title] [album_from_web]
3. [canonical_artist] album:[album_name_from_web]

Return JSON: {{"queries": ["query1", "query2", "query3"], "confidence": "high|medium|low", "reasoning": "brief explanation"}}
"""

        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        
        async with self._lock:
            if not self._ensure_client():
                return None

            try:
                self._reserve_quota(1)
            except GeminiQuotaExceeded:
                LOG.warning("⚠️ Gemini daily quota exhausted")
                return None

            if self._client is None:
                LOG.error("❌ Gemini client not available")
                return None

            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=GROUNDING_MODEL_NAME,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[grounding_tool],
                        # NOTE: response_mime_type="application/json" is incompatible with tools (grounding)
                        # _extract_json_dict() will parse JSON from text response instead
                        thinking_config=types.ThinkingConfig(thinking_budget=-1),
                    ),
                )
            except Exception as e:
                LOG.error(f"❌ Grounding query failed for '{title}': {e}")
                return None

            payload = self._extract_json_dict(response)
            if not isinstance(payload, dict):
                return None

            queries = payload.get("queries", [])
            confidence = str(payload.get("confidence", "low")).strip().lower()
            reasoning = str(payload.get("reasoning", "")).strip()

            # Validate we got 3 queries
            if not isinstance(queries, list) or len(queries) != 3:
                LOG.warning(
                    f"⚠️ Flash+Grounding returned {len(queries) if isinstance(queries, list) else 0} queries "
                    f"instead of 3 for '{title}'"
                )
                return None

            LOG.info(
                f"✅ Flash+Grounding+Thinking generated 3 queries for '{title}' "
                f"(confidence={confidence})\n   Reasoning: {reasoning}"
            )
            return {
                "queries": queries,
                "confidence": confidence,
                "reasoning": reasoning,
            }

    async def generate_fallback_metadata(
        self,
        title: str,
        failed_queries: List[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Stage 3 Fallback: Use Flash + Grounding + Thinking to extract best guess metadata.
        
        Reuses the same Flash+Grounding+Thinking connection from Stage 2, but instead of
        generating Deezer queries, directly extracts artist/title from web sources.
        
        Sets is_best_guess=True (7-day TTL) for adaptive expiration.

        Args:
            title: Raw YouTube title to parse
            failed_queries: List of all queries that failed

        Returns:
            Dict with artist, title, album, confidence, reasoning, track_type, is_best_guess
        """
        if genai is None or types is None:
            LOG.error("google-genai not available, cannot use grounding")
            return None

        prompt = f"""All Deezer searches failed for: "{title}"
Failed queries: {failed_queries}

Use Google Search to find the MOST LIKELY canonical metadata.

Think through:
1. What type of content is this (music/OST/anime/game)?
2. Who is the ACTUAL artist (person vs franchise)?
3. What is the CLEAN title (remove YouTube suffixes)?
4. Is this official or fan-made?

Return the BEST GUESS metadata for Last.fm scrobbling:
{{
    "artist": "canonical artist name",
    "title": "clean track title",
    "album": "album name if known, else empty string",
    "confidence": "high|medium|low",
    "reasoning": "why this is the best guess",
    "track_type": "music|ost|anime_opening|game_soundtrack|cover|fan_made"
}}
"""

        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        
        async with self._lock:
            if not self._ensure_client():
                return None

            try:
                self._reserve_quota(1)
            except GeminiQuotaExceeded:
                LOG.warning("⚠️ Gemini daily quota exhausted")
                return None

            if self._client is None:
                LOG.error("❌ Gemini client not available")
                return None

            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=GROUNDING_MODEL_NAME,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[grounding_tool],
                        # NOTE: response_mime_type="application/json" is incompatible with tools (grounding)
                        # _extract_json_dict() will parse JSON from text response instead
                        thinking_config=types.ThinkingConfig(thinking_budget=-1),
                    ),
                )
            except Exception as e:
                LOG.error(f"❌ Fallback metadata extraction failed for '{title}': {e}")
                return None

            payload = self._extract_json_dict(response)
            if not isinstance(payload, dict):
                return None

            artist = str(payload.get("artist", "")).strip()
            track_title = str(payload.get("title", "")).strip()
            album = str(payload.get("album", "")).strip()
            confidence = str(payload.get("confidence", "low")).strip().lower()
            reasoning = str(payload.get("reasoning", "")).strip()
            track_type = str(payload.get("track_type", "music")).strip().lower()

            if not artist or not track_title:
                LOG.warning(f"⚠️ Fallback extraction missing artist or title for '{title}'")
                return None

            LOG.info(
                f"✅ Fallback metadata extracted: '{artist} - {track_title}' "
                f"(confidence={confidence}, track_type={track_type})\n   Reasoning: {reasoning}"
            )
            
            result = {
                "artist": artist,
                "title": track_title,
                "album": album,
                "confidence": confidence,
                "reasoning": reasoning,
                "track_type": track_type,
                "is_best_guess": True,  # Mark for 7-day TTL
            }
            return result

    def refresh_keys(self) -> None:
        """Reload API keys from environment and reset state."""

        self._keys = self._load_api_keys()
        self._key_index = 0
        self._key_cooldowns = {key: 0.0 for key in self._keys}
        self._client = None
        self._active_key = None
        self._available = False
        self._status = "reloaded"
        self._initialize_client()

    async def request_enrichment(
        self,
        artist: str,
        title: str,
        existing_tags: Optional[List[str]] = None,
        *,
        allow_grounding: bool = False,
    ) -> Dict[str, Any]:
        future = await self._enqueue_enrichment_future(
            artist,
            title,
            existing_tags or [],
            allow_grounding,
        )
        try:
            return await asyncio.wait_for(future, timeout=self._batch_timeout)
        except asyncio.TimeoutError:
            if not future.done():
                future.set_result({})
            return {}

    async def query_gemini(
        self,
        prompt: str,
        *,
        allow_grounding: bool = False,
    ) -> Optional[str]:
        """
        Simple query method for Gemini. Returns the text response.
        Used for non-enrichment tasks like track selection.
        """
        response = await self._generate(prompt, allow_grounding=allow_grounding)
        if not response:
            return None

        try:
            # Extract text from Gemini response
            if hasattr(response, "text"):
                return response.text
            elif hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, "content") and hasattr(
                    candidate.content, "parts"
                ):
                    parts = candidate.content.parts
                    if parts and hasattr(parts[0], "text"):
                        return parts[0].text
            return None
        except Exception as e:
            LOG.error(f"Failed to extract text from Gemini response: {e}")
            return None

    async def flush_enrichment_queue(self) -> None:
        """Manually flush the enrichment queue and return batch results."""
        await self._flush_enrichment_batch()

    def get_last_batch_results(self) -> Dict[str, Dict[str, Any]]:
        """Get the last batch enrichment results for caching by caller."""
        return getattr(self, "_last_batch_results", {})

    async def _enqueue_enrichment_future(
        self,
        artist: str,
        title: str,
        existing_tags: List[str],
        allow_grounding: bool,
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        normalized_tags = [
            str(tag).strip() for tag in existing_tags if str(tag).strip()
        ]
        entry = EnrichmentRequest(
            artist=artist,
            title=title,
            tags=normalized_tags,
            allow_grounding=allow_grounding,
            key=self._make_track_key(artist, title),
            future=future,
        )

        async with self._batch_lock:
            self._batch_queue.append(entry)
            flush_now = len(self._batch_queue) >= self._batch_max
            if flush_now:
                # Batch is full - cancel pending flush and flush immediately
                if self._batch_task and not self._batch_task.done():
                    self._batch_task.cancel()
                self._batch_task = asyncio.create_task(self._flush_enrichment_batch())
            elif not self._batch_task or self._batch_task.done():
                # Schedule a flush if no task is active
                self._batch_task = asyncio.create_task(
                    self._schedule_enrichment_flush()
                )

        return future

    async def _schedule_enrichment_flush(self) -> None:
        try:
            await asyncio.sleep(self._batch_delay)
        except asyncio.CancelledError:
            return
        await self._flush_enrichment_batch()

    async def _flush_enrichment_batch(self) -> None:
        async with self._batch_lock:
            batch = list(self._batch_queue)
            self._batch_queue.clear()
            self._batch_task = None

        if not batch:
            return

        track_list = ", ".join(
            [f"'{entry.artist} - {entry.title}'" for entry in batch[:3]]
        )
        if len(batch) > 3:
            track_list += f" and {len(batch) - 3} more"

        LOG.info(
            "📊 [Gemini] Requested Metadata Enrichment for %d tracks: [%s]",
            len(batch),
            track_list,
        )
        try:
            result_map = await self._process_enrichment_batch(batch)
            success_count = sum(1 for v in result_map.values() if v)
            LOG.info(
                "✅ [Gemini] Batch complete: %d/%d enriched successfully",
                success_count,
                len(batch),
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            LOG.error("❌ Gemini enrichment batch failed: %s", exc)
            result_map = {}

        # Store batch results for caller to cache
        self._last_batch_results = result_map
        self._last_batch_entries = batch

        # Set futures with results (caller will handle caching)
        for entry in batch:
            payload = result_map.get(entry.key, {})
            if not entry.future.done():
                entry.future.set_result(payload)

    async def _process_enrichment_batch(
        self,
        batch_entries: List[EnrichmentRequest],
    ) -> Dict[str, Dict[str, Any]]:
        if not batch_entries:
            return {}

        if not self._ensure_client():
            return {entry.key: {} for entry in batch_entries}

        allow_grounding = any(entry.allow_grounding for entry in batch_entries)
        prompt = self._build_enrichment_prompt(batch_entries, allow_grounding)
        response = await self._generate(
            prompt, allow_grounding=allow_grounding
        )
        if not response:
            return {entry.key: {} for entry in batch_entries}

        payload = self._extract_json_payload(response)
        if payload is None:
            return {entry.key: {} for entry in batch_entries}

        records: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            candidate = payload.get("results")
            if isinstance(candidate, list):
                records = [record for record in candidate if isinstance(record, dict)]
            elif isinstance(candidate, dict):
                records = [candidate]
            else:
                records = [payload]
        elif isinstance(payload, list):
            records = [record for record in payload if isinstance(record, dict)]

        result_map: Dict[str, Dict[str, Any]] = {}
        unmatched = [entry.key for entry in batch_entries]
        fallback: List[Dict[str, Any]] = []

        for record in records:
            track_id = (
                record.get("id")
                or record.get("track_id")
                or record.get("key")
                or record.get("trackKey")
            )
            tags = record.get("tags") or []
            moods = record.get("moods") or []
            energy = record.get("energy")
            bpm = record.get("bpm")
            key = record.get("key")
            activity_affinity = record.get("activity_affinity")
            emotional_intensity = record.get("emotional_intensity")
            daypart_affinity = record.get("daypart_affinity")
            mood_vector = record.get("mood_vector")

            vibe_guess_raw = record.get("simple_vibe_guess") or record.get("vibe_guess")
            vibe_guess: Optional[List[float]] = None
            if isinstance(vibe_guess_raw, list):
                cleaned: List[float] = []
                for value in vibe_guess_raw[:5]:
                    try:
                        cleaned.append(max(0.0, min(1.0, float(value))))
                    except (TypeError, ValueError):
                        cleaned = []
                        break
                if len(cleaned) == 5:
                    vibe_guess = cleaned

            payload_entry = {
                "tags": tags if isinstance(tags, list) else [],
                "moods": moods if isinstance(moods, list) else [],
                "energy": energy if isinstance(energy, str) else None,
                "bpm": int(bpm) if bpm and str(bpm).isdigit() else None,
                "key": str(key).strip() if key else None,
                "activity_affinity": (
                    str(activity_affinity).strip() if activity_affinity else None
                ),
                "emotional_intensity": (
                    float(emotional_intensity)
                    if emotional_intensity is not None
                    else None
                ),
                "daypart_affinity": (
                    str(daypart_affinity).strip() if daypart_affinity else None
                ),
                "mood_vector": mood_vector if isinstance(mood_vector, dict) else None,
                "simple_vibe_guess": vibe_guess,
            }
            if track_id and track_id in unmatched:
                result_map[track_id] = payload_entry
                unmatched.remove(track_id)
            else:
                fallback.append(payload_entry)

        for key, record in zip(unmatched, fallback):
            result_map[key] = record

        for key in unmatched[len(fallback) :]:
            result_map.setdefault(key, {})

        return result_map

    def _build_enrichment_prompt(
        self,
        batch_entries: List[EnrichmentRequest],
        allow_grounding: bool,
    ) -> str:
        tracks_payload = [
            {
                "id": entry.key,
                "artist": entry.artist,
                "title": entry.title,
                "existing_tags": entry.tags,
            }
            for entry in batch_entries
        ]
        grounding_hint = (
            "Ground answers in reliable published sources when uncertain."
            if allow_grounding
            else "Use only widely known music knowledge."
        )
        instructions = (
            "You are enriching music metadata. Return JSON with a 'results' array. "
            "Each result must echo the input 'id' and include:\n"
            "- 'tags' (list of canonical genres, e.g., ['pop', 'rock', 'electronic'])\n"
            "- 'moods' (up to 3 descriptive moods, e.g., ['energetic', 'uplifting', 'danceable'])\n"
            "- 'energy' (single word: low, medium, high)\n"
            "- 'mood' (single descriptive word for overall mood, e.g., 'happy', 'melancholic', 'intense')\n"
            "- 'simple_vibe_guess' (5 floats between 0-1: [energy, valence, danceability, acousticness, brightness])\n"
            f"{grounding_hint} If unsure, return empty arrays or null values.\n"
            "Note: Audio features (tempo, key, loudness) are computed separately and should NOT be included."
        )
        return (
            f"{instructions}\n\n"
            f"Tracks:\n{json.dumps({'tracks': tracks_payload}, ensure_ascii=False, indent=2)}\n\n"
            "Respond with JSON only."
        )

    def _extract_json_payload(self, response: Any) -> Optional[Any]:
        for text in self._collect_texts(response):
            data = self._try_parse_json_any(text)
            if data is not None:
                return data
        return None

    @staticmethod
    def _try_parse_json_any(payload: str) -> Optional[Any]:
        if not isinstance(payload, str):
            return None
        cleaned = GeminiService._strip_code_fence(payload)
        if not cleaned:
            return None
        try:
            data = json.loads(cleaned)
            if isinstance(data, (dict, list)):
                return data
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cleaned.find(opener)
            end = cleaned.rfind(closer)
            if start != -1 and end != -1 and end > start:
                snippet = cleaned[start : end + 1]
                try:
                    data = json.loads(snippet)
                    if isinstance(data, (dict, list)):
                        return data
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def _make_track_key(artist: str, title: str) -> str:
        return f"{artist.strip().lower()}::{title.strip().lower()}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _initialize_client(self) -> None:
        if not self._keys:
            self._status = "missing api key"
            self._available = False
            return

        for idx in range(len(self._keys)):
            if self._activate_key(idx):
                self._available = True
                self._status = "ready"
                return

        self._status = "no valid api key"
        self._available = False

    def _load_api_keys(self) -> List[str]:
        """
        Load Gemini API keys from environment.
        
        Supports two formats:
        1. GeminiApiKeys: JSON array or comma-separated string (legacy)
        2. GEMINI_API_KEY_1, GEMINI_API_KEY_2, GEMINI_API_KEY_3 (Phase 0.5, preferred)
        
        Phase 0.5 multi-key format takes precedence if both are present.
        """
        keys: List[str] = []
        
        # Phase 0.5: Check for individual API keys (GEMINI_API_KEY_1/2/3)
        for i in range(1, 4):  # Support up to 3 keys
            key_env_name = f"GEMINI_API_KEY_{i}"
            key_value = os.getenv(key_env_name, "").strip()
            if key_value:
                keys.append(key_value)
                LOG.info(f"🔑 Loaded Gemini API key #{i} from {key_env_name}")
        
        # If Phase 0.5 keys found, use them (preferred)
        if keys:
            LOG.info(f"✅ Using {len(keys)} Gemini API key(s) from GEMINI_API_KEY_1/2/3 format")
            return self._deduplicate_keys(keys)
        
        # Fallback: Legacy GeminiApiKeys format
        raw = os.getenv(GEMINI_KEYS_ENV)
        if not raw:
            LOG.warning("⚠️ No Gemini API keys found. Set GEMINI_API_KEY_1/2/3 or GeminiApiKeys")
            return []

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                keys = [str(item).strip() for item in parsed if str(item).strip()]
            elif isinstance(parsed, str):
                keys = [parsed.strip()]
        except json.JSONDecodeError:
            keys = [part.strip() for part in raw.split(",") if part.strip()]

        LOG.info(f"✅ Using {len(keys)} Gemini API key(s) from GeminiApiKeys (legacy format)")
        return self._deduplicate_keys(keys)
    
    def _deduplicate_keys(self, keys: List[str]) -> List[str]:
        """Remove duplicate keys while preserving order."""
        seen: set[str] = set()
        unique: List[str] = []
        for key in keys:
            if key and key not in seen:
                unique.append(key)
                seen.add(key)
        return unique

    def _activate_key(self, index: int) -> bool:
        if not self._keys:
            return False

        index = index % len(self._keys)
        candidate = self._keys[index]
        cooldown_until = self._key_cooldowns.get(candidate, 0.0)
        if time.time() < cooldown_until:
            return False

        try:
            if genai is None:  # pragma: no cover - import guard
                raise RuntimeError("google-genai is not installed")
            self._client = genai.Client(api_key=candidate)
            self._active_key = candidate
            self._key_index = index
            self._key_cooldowns[candidate] = 0.0
            LOG.info(
                "✅ Gemini key #%d ready (fingerprint=%s)",
                index + 1,
                self._fingerprint(candidate),
            )
            return True
        except Exception as exc:  # pragma: no cover - network failure path
            fingerprint = self._fingerprint(candidate)
            LOG.error("❌ Gemini key #%d failed (%s): %s", index + 1, fingerprint, exc)
            self._key_cooldowns[candidate] = time.time() + AUTH_FAILURE_COOLDOWN_SECONDS
            self._client = None
            self._active_key = None
            return False

    async def _generate(
        self,
        prompt: str,
        *,
        allow_grounding: bool = False,
    ) -> Optional[Any]:
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_GENERATE_ATTEMPTS + 1):
            should_retry = False
            backoff = 0.0

            async with self._lock:
                if not self._ensure_client():
                    return None

                await self._throttle_requests()

                try:
                    self._reserve_quota(1)
                except GeminiQuotaExceeded:
                    LOG.warning("⚠️ Gemini daily quota exhausted")
                    self._status = "quota"
                    return None

                client = self._client
                if client is None:
                    self._status = "error"
                    self._available = False
                    return None

                model_name = self._model_for_attempt(attempt)
                use_grounding = allow_grounding and attempt >= 4
                request_kwargs: Dict[str, Any] = {
                    "model": model_name,
                    "contents": prompt,
                }
                if use_grounding:
                    request_kwargs["grounding_config"] = {"google_search": {}}

                try:
                    if LOG.isEnabledFor(logging.DEBUG):
                        LOG.debug(
                            "🧠 Gemini attempt %d/%d using model '%s'%s",
                            attempt,
                            MAX_GENERATE_ATTEMPTS,
                            model_name,
                            " with grounding" if use_grounding else "",
                        )
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        **request_kwargs,
                    )
                except Exception as exc:  # pragma: no cover - network failure path
                    last_error = exc
                    should_retry = True
                    backoff = min(2.5, 0.6 * attempt)
                    self._handle_api_exception(exc)
                else:
                    self._status = "ready"
                    return response

            if should_retry and attempt < MAX_GENERATE_ATTEMPTS:
                if backoff > 0:
                    await asyncio.sleep(backoff)
                continue
            if not should_retry:
                break

        if last_error:
            LOG.error(
                "❌ Gemini generation failed after %d attempts: %s",
                MAX_GENERATE_ATTEMPTS,
                last_error,
            )
        return None

    def _model_for_attempt(self, attempt: int) -> str:
        if attempt >= 4:
            return GROUNDING_MODEL_NAME
        return self._model

    async def _throttle_requests(self) -> None:
        """Throttle per-minute request rate to avoid API 429 responses."""
        if self._rpm_limit <= 0:
            return

        history = self._request_history
        window = self._rate_window

        while True:
            now = time.monotonic()

            # Drop entries that are outside the rolling window
            while history and now - history[0] >= window:
                history.popleft()

            if len(history) < self._rpm_limit:
                history.append(now)
                return

            wait = window - (now - history[0]) + RATE_LIMIT_SAFETY_MARGIN
            if wait <= 0:
                history.popleft()
                continue

            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "⏳ Gemini throttling for %.2fs to respect per-minute limit", wait
                )
            await asyncio.sleep(wait)

    def _ensure_client(self) -> bool:
        if not self._keys:
            self._status = "missing api key"
            self._available = False
            return False

        now = time.time()
        if self._active_key and self._client:
            cooldown_until = self._key_cooldowns.get(self._active_key, 0.0)
            if now >= cooldown_until:
                self._available = True
                return True

        for offset in range(len(self._keys)):
            idx = (self._key_index + offset) % len(self._keys)
            key = self._keys[idx]
            cooldown_until = self._key_cooldowns.get(key, 0.0)
            if now < cooldown_until:
                continue
            if idx == self._key_index and self._client is not None:
                self._active_key = key
                self._available = True
                return True
            if self._activate_key(idx):
                if offset:
                    LOG.info("🔁 Switched to Gemini key #%d", idx + 1)
                self._available = True
                return True

        self._available = False
        self._status = "no key available"
        self._active_key = None
        self._client = None
        return False

    def _handle_api_exception(self, exc: Exception) -> None:
        message = str(exc)
        LOG.error("❌ Gemini API error: %s", message)

        if not self._active_key:
            self._available = False
            self._client = None
            self._status = "error"
            return

        cooldown = RATE_LIMIT_COOLDOWN_SECONDS
        if self._is_auth_error(message):
            cooldown = AUTH_FAILURE_COOLDOWN_SECONDS
            self._status = "auth"
            LOG.error(
                "🚫 Gemini key %s disabled due to authentication failure",
                self._fingerprint(self._active_key),
            )
        elif self._is_rate_limit(message):
            self._status = "rate-limit"
            LOG.warning(
                "⏳ Gemini key %s hit rate limit; cooling for %ds",
                self._fingerprint(self._active_key),
                cooldown,
            )
        else:
            self._status = "error"

        self._key_cooldowns[self._active_key] = time.time() + cooldown
        self._client = None
        self._active_key = None
        self._available = False

    # ------------------------------------------------------------------
    # Quota tracking
    # ------------------------------------------------------------------
    def _reserve_quota(self, count: int) -> None:
        self._reset_usage_if_needed()
        used = int(self._usage_state.get("count", 0))
        if used + count > self._daily_limit:
            raise GeminiQuotaExceeded()
        self._usage_state["count"] = used + count
        self._persist_usage_state()
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(
                "Gemini quota reserved: %s",
                {
                    "requested": count,
                    "total_used": self._usage_state["count"],
                    "remaining": self.quota_remaining,
                    "daily_limit": self._daily_limit,
                },
            )

    def _load_usage_state(self) -> None:
        if not self._usage_file.exists():
            return

        try:
            with self._usage_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                date = str(data.get("date", ""))
                count = int(data.get("count", 0))
                self._usage_state = {"date": date, "count": max(0, count)}
        except (OSError, ValueError, TypeError):
            self._usage_state = {"date": self._today_key(), "count": 0}

    def _persist_usage_state(self) -> None:
        try:
            with self._usage_file.open("w", encoding="utf-8") as handle:
                json.dump(self._usage_state, handle)
        except OSError:  # pragma: no cover - disk failure path
            LOG.warning("⚠️ Failed to persist Gemini usage state")

    def _reset_usage_if_needed(self) -> None:
        today = self._today_key()
        if self._usage_state.get("date") != today:
            self._usage_state = {"date": today, "count": 0}
            self._persist_usage_state()

    @staticmethod
    def _today_key() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------
    @staticmethod
    def _build_track_prompt(raw_title: str, channel_name: str) -> str:
        return (
            "You are a music metadata parser.\n"
            "Your task: Extract the performing artist (or primary creator), the song title, "
            "track type, and any franchise entity from a given YouTube video title and channel name.\n"
            "Respond **only** with a compact JSON object with keys:\n"
            '- "artist": string\n'
            '- "title": string\n'
            '- "featuring_artists": array of strings (can be empty)\n'
            '- "track_type": string (one of: "music", "ost", "game_soundtrack", "anime_opening")\n'
            '- "primary_entity": string or null (franchise/show/game name if this is OST content)\n\n'
            "Input:\n"
            f'YouTube Title: "{raw_title}"\n'
            f'Channel Name: "{channel_name}"\n\n'
            "Parsing Instructions:\n"
            '1. **Clean the title**: Remove standard descriptors such as "(Official Video)", '
            '"[Lyrics]", "(Audio)", "(Music Video)", "– Lyric Video", etc. '
            "Also remove trailing or leading extra punctuation and whitespace.\n"
            "2. **Identify separators**: Common separators between artist and title include "
            '"–", "—", ":", "|". Use the first such clear separator **if** the text '
            "before it is plausibly an artist name rather than a franchise/show/series.\n"
            "3. **Distinguish franchise or show names**: If the title contains a known series, "
            'show, game, or franchise name (for example "MURDER DRONES", "FNAF", "Hazbin Hotel", '
            '"Helluva Boss", "Undertale", "Jujutsu Kaisen", "Attack on Titan", etc.), '
            "treat that part as *non-artist (series/franchise)*. "
            "Look for patterns like 'Song Title | Franchise Name' or 'Franchise - Song Title'. "
            'Set "track_type" appropriately and "primary_entity" to the franchise name.\n'
            "4. **Track type classification**:\n"
            '   - "music": Standard music track (artist is a musical performer/band)\n'
            '   - "ost": Original soundtrack from a TV show or movie (e.g., Hazbin Hotel, MURDER DRONES)\n'
            '   - "game_soundtrack": Video game music (e.g., Undertale, FNAF)\n'
            '   - "anime_opening": Anime opening/ending theme (e.g., Jujutsu Kaisen, Attack on Titan)\n'
            "5. **Primary entity**: If track_type is NOT 'music', extract the franchise/show/game name "
            '(e.g., "Hazbin Hotel", "Undertale", "Jujutsu Kaisen"). Otherwise set to null.\n'
            "6. **Channel name as potential artist**:\n"
            '   - If the channel name is clearly a music creator or publisher (e.g., "GLITCH", '
            '"The Living Tombstone"), it may serve as the "artist".\n'
            "   - If the channel name is a streaming service or generic publisher "
            '(e.g., "Prime Video", "Netflix", "Crunchyroll", "Sony Music"), do NOT use it as the artist.\n'
            "   - For OST/soundtrack content: If you can identify the actual voice actor or performer, use that. "
            "Otherwise, set artist to null - do NOT use fictional placeholders like '[Franchise] Cast'. "
            "The system will use primary_entity for recommendation fetching.\n"
            '7. **Featuring artists**: Look for markers like "ft.", "feat.", "featuring", "with" '
            'in the title. Extract the names following these markers into the "featuring_artists" array. '
            "The main artist remains the primary one identified earlier.\n"
            "8. **Ambiguous cases**:\n"
            "   - If you cannot confidently identify a separate artist in the title, then use the "
            'channel name as "artist".\n'
            "   - If both the channel and the title suggest different possible artists, prefer the "
            "musical creator (rather than a franchise/title).\n"
            "9. **Song title extraction**: After removing artist parts and descriptors, what remains "
            'is the "title". Clean up extra whitespace, correct apostrophes or stylization where '
            "obvious, but don't alter meaning.\n"
            "10. **Return format**: Exactly one JSON object (no additional commentary).\n\n"
            "Example:\n"
            "Input:\n"
            "YouTube Title: \"MURDER DRONES – FIGHT TIL' I'M GOOD ENOUGH (ft. The Living Tombstone) "
            '[Official Music Video]"\n'
            'Channel Name: "GLITCH"\n\n'
            "Output:\n"
            "{\n"
            '  "artist": "GLITCH",\n'
            '  "title": "Fight Til I\'m Good Enough",\n'
            '  "featuring_artists": ["The Living Tombstone"],\n'
            '  "track_type": "ost",\n'
            '  "primary_entity": "MURDER DRONES"\n'
            "}\n\n"
            "Example 2:\n"
            "Input:\n"
            'YouTube Title: "Imagine Dragons - Believer (Official Music Video)"\n'
            'Channel Name: "ImagineDragonsVEVO"\n\n'
            "Output:\n"
            "{\n"
            '  "artist": "Imagine Dragons",\n'
            '  "title": "Believer",\n'
            '  "featuring_artists": [],\n'
            '  "track_type": "music",\n'
            '  "primary_entity": null\n'
            "}\n\n"
            "Example 3:\n"
            "Input:\n"
            'YouTube Title: "Poison Full Song | Hazbin Hotel | Prime Video"\n'
            'Channel Name: "Prime Video"\n\n'
            "Output:\n"
            "{\n"
            '  "artist": null,\n'
            '  "title": "Poison",\n'
            '  "featuring_artists": [],\n'
            '  "track_type": "ost",\n'
            '  "primary_entity": "Hazbin Hotel"\n'
            "}\n\n"
            "Now parse the provided input and return JSON only."
        )

    @staticmethod
    def _build_mood_prompt(tags: List[str], genre: str, description: str) -> str:
        tags_text = ", ".join(str(tag) for tag in tags) if tags else "(none)"
        genre_text = genre or "(unknown)"
        description_text = description or ""
        return (
            "You are enriching music metadata for a recommendation engine.\n"
            "Infer high-level mood and energy signals given the supplied context.\n"
            "Also provide cultural context tags.\n"
            "Respond with JSON containing:\n"
            "- 'energy', 'valence', 'tempo' (each 0..1)\n"
            "- 'mood' (single descriptive word)\n"
            "- 'confidence' (0..1)\n"
            "- 'vibe_situation' (e.g., 'gym workout', 'dinner party', 'studying')\n"
            "- 'lyrical_themes' (e.g., 'heartbreak', 'victory', 'social commentary')\n"
            "- 'similar_artists' (list of 3 artist names)\n"
            "- 'era_scene' (e.g., '90s Grunge', '2010s EDM')\n\n"
            f"Tags: {tags_text}\n"
            f"Genre: {genre_text}\n"
            f"Description: {description_text}\n"
            "Return only the JSON object."
        )

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------
    def _extract_json_dict(self, response: Any) -> Optional[Dict[str, Any]]:
        for text in self._collect_texts(response):
            data = self._try_parse_json_dict(text)
            if isinstance(data, dict):
                return data
        return None

    @staticmethod
    def _collect_texts(response: Any) -> List[str]:
        texts: List[str] = []
        if response is None:
            return texts

        primary_text = getattr(response, "text", None)
        if isinstance(primary_text, str) and primary_text.strip():
            texts.append(primary_text)

        parsed_payload = getattr(response, "parsed", None)
        if isinstance(parsed_payload, (dict, list)):
            try:
                texts.append(json.dumps(parsed_payload))
            except (TypeError, ValueError):
                pass

        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str) and part_text.strip():
                    texts.append(part_text)

        return texts

    @staticmethod
    def _try_parse_json_dict(payload: str) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, str):
            return None

        cleaned = GeminiService._strip_code_fence(payload)
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

    @staticmethod
    def _strip_code_fence(payload: str) -> str:
        stripped = payload.strip()
        match = re.match(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
        if match:
            return match.group(1).strip()
        return stripped

    @staticmethod
    def _fingerprint(key: str) -> str:
        return sha1(key.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _is_rate_limit(message: str) -> bool:
        lowered = message.lower()
        return "429" in lowered or "quota" in lowered or "resource_exhausted" in lowered

    @staticmethod
    def _is_auth_error(message: str) -> bool:
        lowered = message.lower()
        return any(
            token in lowered
            for token in ("401", "403", "permission", "unauthorized", "forbidden")
        )
