import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Any, Dict, List, Optional

# google-genai provides the official Gemini client. Optional import keeps
# static analysis happy when the dependency is not available at design time.
try:  # pragma: no cover - import shim
    from google import genai  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - fallback stub for tooling
    genai = None  # type: ignore[assignment]

LOG = logging.getLogger(__name__)

GEMINI_KEYS_ENV = "GeminiApiKeys"
LEGACY_GEMINI_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_DAILY_LIMIT = 500
RATE_LIMIT_COOLDOWN_SECONDS = 600
AUTH_FAILURE_COOLDOWN_SECONDS = 3600
ENRICHMENT_BATCH_MAX = 25
ENRICHMENT_BATCH_DELAY_SECONDS = 1.0
ENRICHMENT_BATCH_TIMEOUT_SECONDS = 15.0


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
        if artist and title:
            return {"artist": artist, "title": title}
        return None

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
        normalized_tags = [str(tag).strip() for tag in existing_tags if str(tag).strip()]
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
                if self._batch_task and not self._batch_task.done():
                    self._batch_task.cancel()
                self._batch_task = asyncio.create_task(self._flush_enrichment_batch())
            elif not self._batch_task or self._batch_task.done():
                self._batch_task = asyncio.create_task(self._schedule_enrichment_flush())

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

        track_list = ", ".join([f"'{entry.artist} - {entry.title}'" for entry in batch[:3]])
        if len(batch) > 3:
            track_list += f" and {len(batch) - 3} more"
        
        LOG.info("📊 [Gemini] Requested Metadata Enrichment for %d tracks: [%s]", len(batch), track_list)
        try:
            result_map = await self._process_enrichment_batch(batch)
            success_count = sum(1 for v in result_map.values() if v)
            LOG.info("✅ [Gemini] Batch complete: %d/%d enriched successfully", success_count, len(batch))
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
            payload_entry = {
                "tags": tags if isinstance(tags, list) else [],
                "moods": moods if isinstance(moods, list) else [],
                "energy": energy if isinstance(energy, str) else None,
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
            f"{grounding_hint} If unsure, return empty arrays or default values."
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
        raw = os.getenv(GEMINI_KEYS_ENV) or os.getenv(LEGACY_GEMINI_KEY_ENV, "")
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
            "You are a music metadata parser. Extract the primary artist and song title\n"
            "from the provided YouTube metadata. Respond with a compact JSON object\n"
            "containing keys 'artist' and 'title'.\n\n"
            f"YouTube Title: \"{raw_title}\"\n"
            f"Channel Name: \"{channel_name}\"\n\n"
            "Rules:\n"
            "1. Remove descriptors such as '(Official Video)', '[Lyrics]', 'HD', etc.\n"
            "2. Prefer the canonical artist over channel branding or fan accounts.\n"
            "3. If multiple artists are listed, keep only the primary credited artist.\n"
            "4. Keep featuring artists inside the title only if they are part of the song name.\n"
            "5. Return only JSON, with double-quoted keys and string values."
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
        return any(token in lowered for token in ("401", "403", "permission", "unauthorized", "forbidden"))
