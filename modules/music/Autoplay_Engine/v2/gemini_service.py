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
except Exception:
    genai = None

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

        payload = self._extract_json_dict(response)
        if not isinstance(payload, dict):
            return None

        artist = str(payload.get("artist", "")).strip()
        title = str(payload.get("title", "")).strip()
        if not (artist and title):
            return None

        # Extract and validate track_type
        track_type = str(payload.get("track_type", "music")).strip().lower()
        if track_type not in {"music", "ost", "game_soundtrack", "anime_opening"}:
            track_type = "music"

        # Extract primary_entity (only valid if track_type is NOT "music")
        primary_entity = None
        if track_type != "music":
            raw_entity = payload.get("primary_entity")
            if raw_entity and isinstance(raw_entity, str):
                primary_entity = raw_entity.strip() or None
            # If OST but no entity, fallback to "music"
            if not primary_entity:
                track_type = "music"

        return {
            "artist": artist,
            "title": title,
            "track_type": track_type,
            "primary_entity": primary_entity,
        }

    async def classify_mood_vector(
        self,
        metadata: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Derive mood/energy descriptors from enriched metadata."""

        tags = metadata.get("tags") or []
        genre = metadata.get("genre") or ""
        description = metadata.get("description") or ""
        prompt = self._build_mood_prompt(tags, genre, description)
        response = await self._generate(prompt)
        if not response:
            return None

        payload = self._extract_json_dict(response)
        if not isinstance(payload, dict):
            return None

        mood = str(payload.get("mood", "")).strip() or None
        energy = float(payload.get("energy", 0.0) or 0.0)
        valence = float(payload.get("valence", 0.0) or 0.0)
        tempo = float(payload.get("tempo", 0.0) or 0.0)
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        return {
            "mood": mood,
            "energy": energy,
            "valence": valence,
            "tempo": tempo,
            "confidence": confidence,
        }

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

    async def query_gemini(self, prompt: str) -> Optional[str]:
        """
        Simple query method for Gemini. Returns the text response.
        Used for non-enrichment tasks like track selection.
        """
        response = await self._generate(prompt)
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
        await self._flush_enrichment_batch()

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
        response = await self._generate(prompt)
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
            "- 'tags' (list of canonical genres)\n"
            "- 'moods' (up to 3 descriptive moods)\n"
            "- 'energy' (single word: low, medium, high)\n"
            "- 'mood_vector' (object with: energy (0-1), valence (0-1), tempo (0-1), confidence (0-1), mood (string))\n"
            "- 'bpm' (estimated tempo in beats per minute, integer, or null if unknown)\n"
            "- 'key' (musical key, e.g., 'C major', 'A minor', or null)\n"
            "- 'activity_affinity' (best use case: 'workout', 'study', 'party', 'relaxation', 'driving', or null)\n"
            "- 'emotional_intensity' (0.0 to 1.0, how emotionally intense the track feels)\n"
            "- 'daypart_affinity' (when track fits best: 'morning', 'afternoon', 'evening', 'night', or null)\n"
            f"{grounding_hint} If unsure, return empty arrays or null values."
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
        raw = os.getenv(GEMINI_KEYS_ENV)
        keys: List[str] = []
        if not raw:
            return keys

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                keys = [str(item).strip() for item in parsed if str(item).strip()]
            elif isinstance(parsed, str):
                keys = [parsed.strip()]
        except json.JSONDecodeError:
            keys = [part.strip() for part in raw.split(",") if part.strip()]

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

    async def _generate(self, prompt: str) -> Optional[Any]:
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

            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=self._model,
                    contents=prompt,
                )
            except Exception as exc:  # pragma: no cover - network failure path
                self._handle_api_exception(exc)
                return None
            else:
                self._status = "ready"
                return response

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
            "3. **Distinguish franchise or show names**: If the title begins with a known series, "
            'show, game, or franchise name (for example "MURDER DRONES", "FNAF", "Hazbin Hotel", '
            '"Undertale", "Jujutsu Kaisen", "Attack on Titan", etc.), '
            'treat that part as *non-artist (series/franchise)*. Do not assign it as the "artist". '
            'Instead, set "track_type" appropriately and "primary_entity" to the franchise name.\n'
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
            "   - If the channel name is a generic label, publisher, or a show/series channel "
            "(not performing artist), you should still examine the title to find a performing artist.\n"
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
            "Respond with JSON containing numeric fields 'energy', 'valence', 'tempo'\n"
            "(each 0..1) and a string 'mood'. Add 'confidence' (0..1).\n\n"
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
