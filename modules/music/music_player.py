import os
import asyncio
import time
import math
import logging
import json
import random
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque

import aiohttp
import discord
from discord.ext import commands, tasks
from modules.setconfig import json_get

import pomice
from pomice import events as pomice_events
from modules.music.lavalink.manager import (
    ensure_local_node,
    shutdown_local_node,
    is_local_node_managed,
    DEFAULT_YOUTUBE_PLUGIN_VERSION,
    DEFAULT_SPONSORBLOCK_PLUGIN_VERSION,
)

# Import Last.fm autoplay
try:
    from modules.music.lastfm_autoplay import get_lastfm_autoplay

    LASTFM_AUTOPLAY_AVAILABLE = True
except ImportError:
    LASTFM_AUTOPLAY_AVAILABLE = False
    get_lastfm_autoplay = None

# -- Pomice compatibility shims -------------------------------------------------
try:  # pragma: no cover - defensive
    _base_event = getattr(pomice_events, "PomiceEvent", None)
    if _base_event is not None:
        if not hasattr(pomice_events, "SegmentsLoaded"):

            class SegmentsLoaded(_base_event):
                """Compatibility event for Lavalink SponsorBlock segments."""

                name = "segments_loaded"
                __slots__ = ("player", "data", "segments")

                def __init__(self, data, player):
                    self.player = player
                    self.data = data
                    self.segments = (
                        data.get("segments") if isinstance(data, dict) else None
                    )
                    self.handler_args = (self.player, self.segments)

            pomice_events.SegmentsLoaded = SegmentsLoaded

        if not hasattr(pomice_events, "SegmentSkipped"):

            class SegmentSkipped(_base_event):
                """Compatibility event for Lavalink SponsorBlock segment skip."""

                name = "segment_skipped"
                __slots__ = ("player", "data", "segment", "category")

                def __init__(self, data, player):
                    self.player = player
                    self.data = data
                    payload = data.get("segment") if isinstance(data, dict) else None
                    if isinstance(payload, dict):
                        self.segment = payload
                        self.category = payload.get("category")
                    else:
                        self.segment = payload
                        self.category = None
                    self.handler_args = (self.player, self.segment, self.category)

            pomice_events.SegmentSkipped = SegmentSkipped
except Exception:
    pass

LOG = logging.getLogger(__name__)

MAIN_GUILD = os.getenv("MAIN_GUILD")
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
LAVALINK_SECURE = os.getenv("LAVALINK_SECURE", "false").lower() in ("1", "true", "yes")
LAVALINK_REGION = os.getenv("LAVALINK_REGION")  # optional
LAVALINK_AUTO_START = os.getenv("LAVALINK_AUTO_START", "true").lower() in (
    "1",
    "true",
    "yes",
)

SPONSORBLOCK_ALLOWED = {
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "filler",
    "music_offtopic",
}

NON_SONG_SEGMENTS = {"intro", "outro", "preview", "filler", "music_offtopic"}

LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}

DEFAULT_AUTOPLAY_MAX_RESULTS = 25
DEFAULT_VOTESKIP_PERCENT = 60


@dataclass(slots=True)
class EnsureConnectionResult:
    player: Optional["pomice.Player"]
    joined_channel: Optional[str] = None
    joined: bool = False
    error: Optional[str] = None


@dataclass(slots=True)
class EnqueueResult:
    success: bool
    track_title: Optional[str] = None
    queue_position: Optional[int] = None
    joined_channel: Optional[str] = None
    started_playback: bool = False
    queued_count: int = 0
    error: Optional[str] = None


def _voice_flag(vc: Any, attr: str) -> bool:
    if vc is None:
        return False
    value = getattr(vc, attr, None)
    if callable(value):
        try:
            value = value()
        except TypeError:
            pass
    return bool(value)


def is_voice_connected(vc: Any) -> bool:
    return _voice_flag(vc, "is_connected")


def is_voice_playing(vc: Any) -> bool:
    return _voice_flag(vc, "is_playing")


def is_voice_paused(vc: Any) -> bool:
    return _voice_flag(vc, "is_paused")


class MusicPlayer(commands.Cog):
    """
    Lavalink-based music backend (Pomice):
    - Node connect/reconnect + optional auto-start local node if unavailable
    - Per-guild player queue, repeat modes (none/current/queue)
    - Vote-skip helpers
    - Inactivity auto-disconnect
    - Global MaxConcurrentInstances enforcement
    - Now Playing embeds
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voteskip: Dict[int, set] = defaultdict(set)
        self.repeat_mode: Dict[int, str] = {}
        self._idle_tasks: Dict[int, asyncio.Task] = {}
        self.connection_cooldowns: Dict[int, float] = {}
        self.queues: Dict[int, deque] = defaultdict(deque)
        self.shuffle_flags: Dict[int, bool] = defaultdict(bool)
        self._current_entries: Dict[int, Dict[str, Any]] = {}
        self._playing_flags: Dict[int, bool] = defaultdict(bool)
        self._local_node_managed = False
        self._sponsorblock_lock = asyncio.Lock()
        self._node_ready = asyncio.Event()
        self._node_help_printed = False
        self._enqueue_errors: Dict[int, str] = {}
        self._bootstrap_error: Optional[str] = None
        self._now_playing_messages: Dict[int, discord.Message] = {}
        self._session_autoplay_disabled = defaultdict(bool)
        self._command_channels: Dict[int, discord.abc.Messageable] = {}
        self._last_successful_autoplay_track: Dict[int, Dict[str, Any]] = {}
        self._autoplay_session_started: Dict[int, bool] = (
            {}
        )  # Track first autoplay per session
        self._playback_state: Dict[int, Dict[str, Any]] = {}
        self._pending_feedback: Dict[int, Dict[str, Any]] = {}

        # Initialize Last.fm autoplay
        LOG.info("[AutoPlay] Attempting to initialize Last.fm module...")
        self._lastfm_autoplay = None
        if LASTFM_AUTOPLAY_AVAILABLE and get_lastfm_autoplay:
            try:
                self._lastfm_autoplay = get_lastfm_autoplay(bot)
                if self._lastfm_autoplay and self._lastfm_autoplay.is_available():
                    LOG.info("[AutoPlay] ✅ Last.fm autoplay is READY and AVAILABLE!")
                else:
                    LOG.warning(
                        "[AutoPlay] Last.fm module loaded but API key not configured"
                    )
            except Exception as e:
                LOG.error(f"[AutoPlay] ❌ Failed to initialize Last.fm autoplay: {e}")
                import traceback

                traceback.print_exc()
        else:
            LOG.warning(
                f"[AutoPlay] Last.fm module not available (AVAILABLE={LASTFM_AUTOPLAY_AVAILABLE})"
            )

        self._disconnect_messages = self._load_disconnect_messages()
        self._bootstrap_node.start()

    def is_session_autoplay_enabled(self, guild_id: int) -> bool:
        return not self._session_autoplay_disabled.get(guild_id, False)

    def set_session_autoplay(self, guild_id: int, enabled: bool) -> None:
        if enabled:
            self._session_autoplay_disabled.pop(guild_id, None)
        else:
            self._session_autoplay_disabled[guild_id] = True
            # Reset autoplay session when manually disabled
            self._autoplay_session_started[guild_id] = False

    def reset_session_state(self, guild_id: int) -> None:
        self._session_autoplay_disabled.pop(guild_id, None)
        self._autoplay_session_started.pop(guild_id, None)

    def set_command_channel(
        self, guild_id: int, channel: discord.abc.Messageable
    ) -> None:
        """Store the channel where a command was invoked for announcements."""
        self._command_channels[guild_id] = channel

    def check_user_in_bot_vc(
        self, member: discord.Member, guild: discord.Guild
    ) -> Tuple[bool, Optional[str]]:
        """Check if user is in the same VC as the bot.

        Returns:
            Tuple[bool, Optional[str]]: (is_valid, error_message)
                - (True, None) if validation passes
                - (False, error_message) if validation fails
        """
        # Check if bot is connected
        bot_vc = guild.voice_client
        if not bot_vc or not isinstance(bot_vc, pomice.Player):
            return True, None  # Bot not connected, allow command

        # Check if bot is actually connected to a channel
        if not is_voice_connected(bot_vc):
            return True, None  # Bot not actually connected

        # Check if user is in a voice channel
        user_vc = getattr(member, "voice", None)
        if not user_vc or not getattr(user_vc, "channel", None):
            return False, ":x: You must be in a voice channel to use music commands."

        # Check if user is in the same VC as bot
        bot_channel = getattr(bot_vc, "channel", None)
        if bot_channel and user_vc.channel.id != bot_channel.id:
            return (
                False,
                f":x: You must be in the same voice channel as the bot (<#{bot_channel.id}>) to use this command.",
            )

        return True, None

    @staticmethod
    def _clamp_seconds(value: Any, default: int) -> int:
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return default
        return max(30, min(7200, seconds))

    def _resolve_disconnect_timeout(self, guild_id: int, kind: str) -> int:
        cfg = self._get_music_config(guild_id)
        if kind == "idle":
            key = "AutoDisconnectIdleSeconds"
            fallback = 300
        else:
            key = "AutoDisconnectEmptySeconds"
            fallback = 180

        if key in cfg and cfg[key] is not None:
            return self._clamp_seconds(cfg[key], fallback)
        return fallback

    def cog_unload(self):
        self._bootstrap_node.cancel()
        for t in list(self._idle_tasks.values()):
            t.cancel()
        if self._local_node_managed:
            asyncio.create_task(shutdown_local_node())
        # No cleanup needed for Last.fm autoplay

    def _set_enqueue_error(self, guild_id: int, message: str) -> None:
        if guild_id:
            self._enqueue_errors[guild_id] = message

    def get_last_enqueue_error(self, guild_id: int) -> Optional[str]:
        return self._enqueue_errors.get(guild_id)

    def is_playback_active(self, guild_id: int) -> bool:
        return bool(self._playing_flags.get(guild_id))

    def _load_disconnect_messages(self) -> Dict[str, List[str]]:
        messages: Dict[str, List[str]] = {}
        try:
            path = (
                Path(__file__).resolve().parent.parent
                / "sentenceslist"
                / "disconnectMessages.json"
            )
            if not path.exists():
                return messages
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception:
            LOG.debug("Failed to load disconnect messages", exc_info=True)
            return messages

        if not isinstance(data, dict):
            return messages

        for key, value in data.items():
            bucket: List[str] = []
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        msgs = entry.get("messages")
                        if isinstance(msgs, list):
                            bucket.extend([m for m in msgs if isinstance(m, str)])
                    elif isinstance(entry, list):
                        bucket.extend([m for m in entry if isinstance(m, str)])
                    elif isinstance(entry, str):
                        bucket.append(entry)
            elif isinstance(value, dict):
                msgs = value.get("messages")
                if isinstance(msgs, list):
                    bucket.extend([m for m in msgs if isinstance(m, str)])

            if bucket:
                messages[key] = bucket

        return messages

    def _pick_disconnect_message(self, category: str) -> Optional[str]:
        choices = (
            self._disconnect_messages.get(category)
            if hasattr(self, "_disconnect_messages")
            else None
        )
        if not choices:
            return None
        return random.choice(choices)

    def get_disconnect_message(self, category: str) -> Optional[str]:
        if not getattr(self, "_disconnect_messages", None):
            self._disconnect_messages = self._load_disconnect_messages()
        return self._pick_disconnect_message(category)

    def _get_announcement_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.abc.Messageable]:
        # First, check if we have a stored command channel for this guild
        if guild.id in self._command_channels:
            return self._command_channels[guild.id]

        # Fallback: find first available text channel with send permissions
        member = getattr(guild, "me", None)
        if member is None and getattr(self.bot, "user", None):
            member = guild.get_member(self.bot.user.id)  # type: ignore[arg-type]

        for channel in getattr(guild, "text_channels", []):
            try:
                perms = channel.permissions_for(member or guild.default_role)
            except Exception:
                continue
            if getattr(perms, "send_messages", False):
                return channel
        return None

    async def send_disconnect_message(
        self,
        guild: discord.Guild,
        category: str,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> bool:
        message = self.get_disconnect_message(category)
        if not message:
            return False
        target = channel or self._get_announcement_channel(guild)
        if not target:
            return False
        try:
            await target.send(message)
            return True
        except Exception:
            LOG.debug(
                "Failed to send disconnect message in guild %s", guild.id, exc_info=True
            )
        return False

    async def _send_autoplay_no_recommendations_message(
        self, guild: discord.Guild, track_title: str, track_author: str
    ) -> bool:
        """Send a message when autoplay couldn't find any recommendations."""
        target = self._get_announcement_channel(guild)
        if not target:
            LOG.debug(f"No announcement channel found for guild {guild.id}")
            return False

        try:
            message = f":x: **AutoPlay** couldn't find any tracks related to `{track_title}` by `{track_author}`"
            await target.send(message)
            LOG.info(
                f"[AutoPlay] Sent 'no recommendations' message to guild {guild.id}"
            )
            return True
        except Exception as e:
            LOG.debug(
                f"Failed to send autoplay message in guild {guild.id}: {e}",
                exc_info=True,
            )
        return False

    async def _send_autoplay_started_message(
        self, guild: discord.Guild, track_title: str
    ) -> bool:
        """Send a message when autoplay starts playing a track."""
        target = self._get_announcement_channel(guild)
        if not target:
            LOG.debug(f"No announcement channel found for guild {guild.id}")
            return False

        try:
            message = f":sparkles: **AutoPlay** started playing: `{track_title}`"
            await target.send(message)
            LOG.info(f"[AutoPlay] Sent 'started playing' message to guild {guild.id}")
            return True
        except Exception as e:
            LOG.debug(
                f"Failed to send autoplay started message in guild {guild.id}: {e}",
                exc_info=True,
            )
        return False

    async def _send_autoplay_recommending_message(self, guild: discord.Guild) -> bool:
        """Send a message when autoplay starts recommending tracks."""
        target = self._get_announcement_channel(guild)
        if not target:
            LOG.debug(f"No announcement channel found for guild {guild.id}")
            return False

        try:
            message = ":sparkles: **AutoPlay** (Beta) is now Recommending your next tracks. To stop it do `/autoplay state:Disable`"
            await target.send(message)
            LOG.info(f"[AutoPlay] Sent 'recommending' message to guild {guild.id}")
            return True
        except Exception as e:
            LOG.debug(
                f"Failed to send autoplay recommending message in guild {guild.id}: {e}",
                exc_info=True,
            )
        return False

    async def _send_autoplay_now_playing_message(
        self, guild: discord.Guild, track_title: str
    ) -> bool:
        """Send a message for consecutive autoplay tracks."""
        target = self._get_announcement_channel(guild)
        if not target:
            LOG.debug(f"No announcement channel found for guild {guild.id}")
            return False

        try:
            message = f":sparkles: AutoPlaying (Beta): **{track_title}**"
            await target.send(message)
            LOG.info(f"[AutoPlay] Sent 'now playing' message to guild {guild.id}")
            return True
        except Exception as e:
            LOG.debug(
                f"Failed to send autoplay now playing message in guild {guild.id}: {e}",
                exc_info=True,
            )
        return False

    async def _handle_track_exception(
        self,
        player: pomice.Player,
        track: Optional[pomice.Track],
        exc_payload: Any,
    ) -> None:
        guild = player.guild
        gid = guild.id
        title = getattr(track, "title", "Unknown track")
        self._playing_flags[gid] = False
        detail, hint, raw = self._summarize_track_exception(exc_payload)
        await self._delete_now_playing_message(gid)

        entry = self._current_entries.get(gid) or {}
        entry.setdefault("title", title)

        fallback_source = await self._attempt_track_fallback(player, track, entry)

        message_parts = [f":warning: I couldn't play **{title}**."]
        if detail:
            message_parts.append(detail)
        if hint:
            message_parts.append(hint)
        if fallback_source:
            message_parts.append(f"Trying {fallback_source}...")

        notify_text = " ".join(message_parts)

        self._set_enqueue_error(gid, hint or detail or "Playback failed.")

        channel = self._get_announcement_channel(guild)
        if channel:
            try:
                await channel.send(notify_text)
            except Exception:
                LOG.debug(
                    "Failed to send track exception message in guild %s",
                    gid,
                    exc_info=True,
                )

        LOG.warning(
            "Track exception in guild %s: %s", gid, raw or detail or exc_payload
        )

        await self._advance_or_idle(player)

    def _summarize_track_exception(
        self, exc_payload: Any
    ) -> tuple[str, Optional[str], str]:
        raw = ""
        detail = ""
        hint: Optional[str] = None

        if isinstance(exc_payload, dict):
            detail = str(
                exc_payload.get("message") or exc_payload.get("error") or ""
            ).strip()
            cause = str(exc_payload.get("cause") or "").strip()
            raw = f"{detail} {cause}".strip()
        elif exc_payload is not None:
            raw = str(exc_payload).strip()
            detail = raw

        combined = raw or detail

        if combined and "ScriptExtractionException" in combined:
            hint = "YouTube changed its playback signature. Uhoh Stinky, There is no easy workaround to this... Check the lavalink youtube source github to see how to solve the issue."
            if not detail:
                detail = "YouTube signature extractor failed."

        short_detail = detail
        if short_detail and len(short_detail) > 200:
            short_detail = short_detail[:197] + "..."

        return short_detail or "Playback failed.", hint, combined or short_detail

    async def _attempt_track_fallback(
        self,
        player: pomice.Player,
        track: Optional[pomice.Track],
        entry: Dict[str, Any],
    ) -> Optional[str]:
        guild_id = player.guild.id
        retries = entry.get("_retry_attempts", 0)
        if retries >= 1:
            return None

        query_seed = (
            entry.get("title")
            or getattr(track, "title", None)
            or getattr(track, "uri", None)
        )
        if not query_seed:
            return None

        entry["_retry_attempts"] = retries + 1

        original_identifier = entry.get("identifier") or (
            getattr(track, "identifier", None) if track else None
        )
        original_uri = entry.get("uri") or getattr(track, "uri", None)

        title = entry.get("title") or getattr(track, "title", None) or query_seed
        author = entry.get("author") or getattr(track, "author", None)
        query_text = title or query_seed
        if author and author not in query_text:
            query_text = f"{query_text} {author}"

        search_targets: List[Tuple[str, str]] = []

        if query_seed.startswith("ytsearch:"):
            search_targets.append(("YouTube", query_seed))
        else:
            search_targets.append(("YouTube", f"ytsearch:{query_text}"))
        if title and not query_seed.startswith("ytsearch:"):
            search_targets.append(("YouTube", f"ytsearch:{title}"))
        search_targets.append(("YouTube Music", f"ytmsearch:{query_text}"))

        for provider, query in search_targets:
            try:
                results = await player.get_tracks(query=query)
            except Exception as exc:
                LOG.debug(
                    "Fallback search (%s) failed in guild %s: %s",
                    provider,
                    guild_id,
                    exc,
                    exc_info=True,
                )
                continue

            candidates: List[pomice.Track] = []
            if isinstance(results, pomice.Playlist):
                candidates = list(results.tracks)
            elif isinstance(results, list):
                candidates = list(results)
            elif results:
                candidates = [results]  # type: ignore[list-item]

            if not candidates:
                continue

            for candidate in candidates:
                candidate_id = getattr(candidate, "identifier", None)
                candidate_uri = getattr(candidate, "uri", None)
                if (
                    candidate_id
                    and original_identifier
                    and candidate_id == original_identifier
                ):
                    continue
                if candidate_uri and original_uri and candidate_uri == original_uri:
                    continue

                try:
                    if hasattr(player.queue, "put_at_front"):
                        result = player.queue.put_at_front(candidate)
                    else:
                        result = player.queue.put(candidate)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    LOG.debug(
                        "Failed to enqueue %s fallback track in guild %s: %s",
                        provider,
                        guild_id,
                        exc,
                        exc_info=True,
                    )
                    continue

                meta = entry.copy()
                meta["title"] = getattr(
                    candidate, "title", meta.get("title", "Unknown")
                )
                meta["author"] = getattr(candidate, "author", meta.get("author"))
                meta["uri"] = candidate_uri or meta.get("uri")
                meta["identifier"] = candidate_id or meta.get("identifier")
                meta["track"] = candidate
                meta["fallback"] = True
                meta["fallback_source"] = provider
                self.queues[guild_id].appendleft(meta)
                self._current_entries[guild_id] = meta
                self._playing_flags[guild_id] = False
                LOG.info("Queued fallback track via %s in guild %s", provider, guild_id)
                return provider

        return None

    # ---- limits ----
    def get_global_instance_limit(self) -> int:
        try:
            if MAIN_GUILD:
                cfg = json_get(int(MAIN_GUILD))
                v = cfg.get("Music", {}).get("MaxConcurrentInstances")
                if isinstance(v, int) and v >= 1:
                    return v
        except Exception:
            pass
        return 5

    def current_active_instances(self) -> int:
        n = 0
        for g in self.bot.guilds:
            vc = getattr(g, "voice_client", None)
            if isinstance(vc, pomice.Player) and is_voice_connected(vc):
                n += 1
        return n

    # ---- node bootstrap ----
    @tasks.loop(count=1)
    async def _bootstrap_node(self):
        await self.bot.wait_until_ready()
        if not hasattr(pomice, "NodePool"):
            version = getattr(pomice, "__version__", "unknown")
            msg = (
                "Installed Pomice package is missing NodePool. "
                "Please install pomice>=2.9 (current version: %s)."
            ) % version
            LOG.error(msg)
            self._bootstrap_error = msg
            self._node_ready.clear()
            if not self._node_help_printed:
                self._print_lavalink_setup_help()
                self._node_help_printed = True
            return

        if LAVALINK_AUTO_START and LAVALINK_HOST in LOCAL_HOSTS:
            try:
                ensured = await ensure_local_node(
                    LAVALINK_HOST, LAVALINK_PORT, LAVALINK_PASSWORD, LAVALINK_SECURE
                )
                self._local_node_managed = ensured and is_local_node_managed()
                if ensured:
                    LOG.info(
                        "Local Lavalink node ensured (managed=%s)",
                        self._local_node_managed,
                    )
                else:
                    LOG.warning(
                        "Unable to auto-provision Lavalink locally; continuing with connect attempts."
                    )
            except Exception as exc:  # pragma: no cover - defensive logging
                LOG.exception("Auto-starting Lavalink failed: %s", exc)
                self._bootstrap_error = str(exc)
        elif LAVALINK_AUTO_START:
            LOG.info(
                "LAVALINK_AUTO_START enabled but host %s is not local; skipping auto provisioning.",
                LAVALINK_HOST,
            )

        ok = await self._try_connect_node()
        if ok:
            self._node_ready.set()
            self._bootstrap_error = None
        else:
            LOG.error("Lavalink node unavailable. Music features will be limited.")
            if self._bootstrap_error is None:
                self._bootstrap_error = (
                    "Unable to reach Lavalink at configured host/port."
                )
            # Print setup instructions once
            if not self._node_help_printed:
                self._print_lavalink_setup_help()
                self._node_help_printed = True

    @_bootstrap_node.before_loop
    async def _before_bootstrap(self):
        await self.bot.wait_until_ready()

        # Clear all guild history files on startup for fresh sessions
        if self._lastfm_autoplay:
            try:
                cache_dir = Path("cache/music")
                if cache_dir.exists():
                    deleted_count = 0
                    for history_file in cache_dir.glob("*_history.json"):
                        try:
                            history_file.unlink()
                            deleted_count += 1
                        except Exception as e:
                            LOG.warning(f"Failed to delete {history_file}: {e}")
                    if deleted_count > 0:
                        LOG.info(
                            f"🗑️ [Last.fm] Cleared {deleted_count} guild history files on startup"
                        )
            except Exception as e:
                LOG.warning(f"Failed to clear guild histories on startup: {e}")

    async def _try_connect_node(
        self, retry_delay: float = 1.0, attempts: int = 3
    ) -> bool:
        node_pool_cls = getattr(pomice, "NodePool", None)
        if node_pool_cls is None:
            self._bootstrap_error = "Pomice NodePool is unavailable."
            return False

        identifier = LAVALINK_REGION or f"default-{LAVALINK_HOST}:{LAVALINK_PORT}"

        for i in range(attempts):
            try:
                if getattr(node_pool_cls, "_nodes", {}):
                    return True
                await node_pool_cls.create_node(
                    bot=self.bot,
                    host=LAVALINK_HOST,
                    port=LAVALINK_PORT,
                    password=LAVALINK_PASSWORD,
                    identifier=str(identifier),
                    secure=LAVALINK_SECURE,
                    loop=getattr(self.bot, "loop", None),
                    logger=LOG,
                )
                LOG.info(
                    "Connected to Lavalink %s:%s (secure=%s)",
                    LAVALINK_HOST,
                    LAVALINK_PORT,
                    LAVALINK_SECURE,
                )
                return True
            except Exception as e:
                LOG.warning(
                    "Lavalink connect failed (attempt %d/%d): %s", i + 1, attempts, e
                )
                self._bootstrap_error = str(e)
                await asyncio.sleep(retry_delay)
        return False

    @commands.Cog.listener()
    async def on_pomice_websocket_open(self, event: pomice.WebSocketOpenEvent):
        LOG.info("Pomice node websocket open: %s", getattr(event, "target", "unknown"))
        self._node_ready.set()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Clean up history when bot leaves voice channel"""
        if member.id == self.bot.user.id and before.channel and not after.channel:
            guild_id = before.channel.guild.id
            if self._lastfm_autoplay:
                self._lastfm_autoplay.clear_history(guild_id)
                LOG.info(
                    f"🗑️ [AutoPlay] Cleared Last.fm history for guild {guild_id} on disconnect"
                )

    @commands.Cog.listener()
    async def on_pomice_websocket_closed(
        self, event: pomice.WebSocketClosedEvent
    ) -> None:
        payload = getattr(event, "payload", None)
        reason = getattr(payload, "reason", None)
        code = getattr(payload, "code", None)
        by_remote = getattr(payload, "by_remote", None)

        LOG.warning(
            "🔴 Pomice websocket closed: code=%s reason=%s by_remote=%s event=%s",
            code,
            reason,
            by_remote,
            event,
        )

        # Clear node ready flag
        self._node_ready.clear()
        LOG.info("Node ready flag cleared")

        # If session destroyed (4014) or similar critical errors, attempt reconnection
        if code in (4014, 4015, 4009) or code is None:
            LOG.info(
                "Session destroyed or error (code %s), attempting reconnection...", code
            )
            asyncio.create_task(self._attempt_reconnection())
        else:
            LOG.info("Websocket closed with code %s, no reconnection triggered", code)

    async def _attempt_reconnection(self):
        """Attempt to reconnect to Lavalink after websocket closure."""
        LOG.info("Starting reconnection attempt after websocket closure...")
        await asyncio.sleep(2)  # Brief delay before reconnecting

        try:
            # Get node pool
            node_pool_cls = getattr(pomice, "NodePool", None)
            if not node_pool_cls:
                LOG.error("NodePool not available for reconnection")
                return

            # Try to destroy and remove all existing nodes
            try:
                existing_nodes = getattr(node_pool_cls, "_nodes", {})
                LOG.info("Found %d existing nodes to clean up", len(existing_nodes))

                for node_id, node in list(existing_nodes.items()):
                    try:
                        # Try to close websocket if it exists
                        if hasattr(node, "_websocket") and node._websocket:
                            await node._websocket.close()
                        # Try to destroy the node properly
                        if hasattr(node, "destroy"):
                            await node.destroy()
                    except Exception as e:
                        LOG.warning("Error cleaning up node %s: %s", node_id, e)

                # Clear the nodes dict
                existing_nodes.clear()
                LOG.info("Cleared all existing nodes")
            except Exception as e:
                LOG.warning("Error during node cleanup: %s", e)

            # Now attempt to create fresh connection
            identifier = LAVALINK_REGION or f"default-{LAVALINK_HOST}:{LAVALINK_PORT}"

            for attempt in range(1, 6):  # 5 attempts
                try:
                    LOG.info("Reconnection attempt %d/5...", attempt)

                    await node_pool_cls.create_node(
                        bot=self.bot,
                        host=LAVALINK_HOST,
                        port=LAVALINK_PORT,
                        password=LAVALINK_PASSWORD,
                        identifier=str(identifier),
                        secure=LAVALINK_SECURE,
                        loop=getattr(self.bot, "loop", None),
                        logger=LOG,
                    )

                    LOG.info("Successfully created new node connection")

                    # Wait a moment for websocket to fully establish
                    await asyncio.sleep(0.5)

                    self._node_ready.set()
                    self._bootstrap_error = None
                    LOG.info("✅ Reconnection successful!")
                    return

                except Exception as e:
                    LOG.warning("Reconnection attempt %d/5 failed: %s", attempt, e)
                    if attempt < 5:
                        await asyncio.sleep(2.0)

            LOG.error("❌ All reconnection attempts failed")

        except Exception as e:
            LOG.error("Reconnection process failed: %s", e, exc_info=True)

    @commands.Cog.listener()
    async def on_pomice_track_start(self, *args, **kwargs):
        event = args[0] if args else kwargs.get("event")
        player: Optional[pomice.Player] = None
        track: Optional[pomice.Track] = None

        if isinstance(event, pomice.TrackStartEvent):
            player = getattr(event, "player", None)
            track = getattr(event, "track", None)
        else:
            player = kwargs.get("player")
            track = kwargs.get("track")
            if len(args) >= 1 and player is None:
                player = args[0]
            if len(args) >= 2 and track is None:
                track = args[1]

        if not isinstance(player, pomice.Player):
            return

        if track is None and isinstance(event, pomice.TrackStartEvent):
            track = getattr(event, "track", None)
        if track is None:
            track = getattr(player, "_last_track", None)
        if not track:
            return

        setattr(player, "_last_track", track)
        self._playing_flags[player.guild.id] = True
        self._initialize_playback_state(
            player.guild.id, track, self._current_entries.get(player.guild.id)
        )
        await self._announce_now_playing(player, track)

    @commands.Cog.listener()
    async def on_pomice_chapters_loaded(self, *args, **kwargs):
        """Suppress Pomice chapter events if not supported."""
        pass

    @commands.Cog.listener()
    async def on_pomice_chapter_started(self, *args, **kwargs):
        """Suppress Pomice chapter events if not supported."""
        pass

    @commands.Cog.listener()
    async def on_pomice_segments_loaded(self, *args, **kwargs):
        """Suppress SponsorBlock segment events not supported by Pomice."""
        pass

    @commands.Cog.listener()
    async def on_pomice_segment_skipped(self, *args, **kwargs):
        """Suppress SponsorBlock segment events not supported by Pomice."""
        pass

    @commands.Cog.listener()
    async def on_pomice_track_end(self, *args, **kwargs):
        event = args[0] if args else kwargs.get("event")
        player: Optional[pomice.Player] = None
        track: Optional[pomice.Track] = None

        if isinstance(event, pomice.TrackEndEvent):
            player = getattr(event, "player", None)
            track = getattr(event, "track", None)
        else:
            player = kwargs.get("player")
            track = kwargs.get("track")
            if len(args) >= 1 and player is None:
                player = args[0]
            if len(args) >= 2 and track is None:
                track = args[1]

        if not isinstance(player, pomice.Player):
            return

        reason = ""
        if isinstance(event, pomice.TrackEndEvent):
            reason = getattr(event, "reason", "") or ""
        else:
            reason = kwargs.get("reason", "") or ""

        pending_feedback = self._pending_feedback.pop(player.guild.id, None)
        await self._record_playback_feedback_for_current(
            player,
            player.guild.id,
            pending=pending_feedback,
            reason=reason,
        )
        self._playback_state.pop(player.guild.id, None)

        gid = player.guild.id
        self._playing_flags[gid] = False
        await self._delete_now_playing_message(gid)
        mode = self.repeat_mode.get(gid, "off")
        last = track or getattr(player, "_last_track", None)
        entry = self._current_entries.get(gid)
        if entry:
            self._record_recent(entry)

        # Track repeat: replay the same track immediately
        if mode == "track" and last:
            LOG.info(f"[Repeat] Repeating track in guild {gid}")
            self.voteskip[gid].clear()
            self._playing_flags[gid] = True
            try:
                await player.play(last)
            except Exception:
                LOG.debug(
                    "Failed to replay track for repeat-track in guild %s",
                    gid,
                    exc_info=True,
                )
                await self._advance_or_idle(player)
            return

        await self._advance_or_idle(player)

    @commands.Cog.listener()
    async def on_pomice_track_exception(self, *args, **kwargs):
        event = args[0] if args else kwargs.get("event")
        player: Optional[pomice.Player] = None
        track: Optional[pomice.Track] = None
        exc_payload: Any = None

        if isinstance(event, pomice.TrackExceptionEvent):
            player = getattr(event, "player", None)
            track = getattr(event, "track", None)
            exc_payload = getattr(event, "exception", None)
        else:
            player = kwargs.get("player")
            track = kwargs.get("track")
            exc_payload = kwargs.get("exception")
            if len(args) >= 1 and player is None:
                player = args[0]
            if len(args) >= 2 and track is None:
                track = args[1]
            if len(args) >= 3 and exc_payload is None:
                exc_payload = args[2]

        if not isinstance(player, pomice.Player):
            return

        pending_feedback = self._pending_feedback.pop(player.guild.id, None)
        await self._record_playback_feedback_for_current(
            player,
            player.guild.id,
            pending=pending_feedback,
            reason="EXCEPTION",
        )
        state = self._playback_state.get(player.guild.id)
        if state is None:
            self._playback_state[player.guild.id] = {
                "length_ms": None,
                "last_known_position_ms": 0,
                "position_timestamp": time.time(),
                "recorded": True,
            }
        else:
            state["recorded"] = True

        await self._handle_track_exception(player, track, exc_payload)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        # Check if bot itself was disconnected
        if member.bot and getattr(member, "id", None) == getattr(
            self.bot.user, "id", None
        ):
            # Bot was in a channel and now isn't (disconnected)
            if before.channel and not after.channel:
                guild = before.channel.guild
                if guild:
                    # Clean up all state for this guild
                    guild_id = guild.id
                    self._playing_flags[guild_id] = False
                    self._current_entries.pop(guild_id, None)
                    self.queues[guild_id].clear()
                    self.voteskip.pop(guild_id, None)
                    self.repeat_mode.pop(guild_id, None)
                    self.shuffle_flags.pop(guild_id, None)
                    self.connection_cooldowns.pop(guild_id, None)
                    self.clear_votes_for_guild(guild_id)
                    await self._cancel_idle(guild_id)
                    await self._delete_now_playing_message(guild_id)
                    LOG.debug(f"Cleaned up state for guild {guild_id} after disconnect")
            return

        guild = getattr(member, "guild", None)
        if guild is None:
            return

        player = getattr(guild, "voice_client", None)
        if not isinstance(player, pomice.Player):
            return

        channel = getattr(player, "channel", None)
        if channel is None:
            return

        if channel not in {before.channel, after.channel}:
            return

        listeners = [m for m in channel.members if not getattr(m, "bot", False)]

        if not listeners:
            empty_timeout = self._resolve_disconnect_timeout(guild.id, "empty")
            await self._schedule_idle_disconnect(
                player, timeout=empty_timeout, reason="empty"
            )
            return

        await self._cancel_idle(guild.id)

        queue_obj = getattr(player, "queue", None)
        queue_empty = (
            getattr(queue_obj, "is_empty", True) if queue_obj is not None else True
        )

        if queue_empty and not is_voice_playing(player) and not is_voice_paused(player):
            idle_timeout = self._resolve_disconnect_timeout(guild.id, "idle")
            await self._schedule_idle_disconnect(
                player, timeout=idle_timeout, reason="idle"
            )

    async def ensure_player_connected(
        self, guild: discord.Guild, requester_id: int
    ) -> EnsureConnectionResult:
        # Check if node is available, if not attempt reconnection
        if not self._node_ready.is_set():
            LOG.warning("Node not ready, checking if reconnection needed...")
            node_pool_cls = getattr(pomice, "NodePool", None)
            if node_pool_cls:
                existing_nodes = getattr(node_pool_cls, "_nodes", {})
                if not existing_nodes:
                    LOG.warning("No nodes found, triggering reconnection...")
                    asyncio.create_task(self._attempt_reconnection())

        # Wait for node with timeout to prevent hanging
        try:
            await asyncio.wait_for(self._node_ready.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            LOG.error("Timeout waiting for node to be ready")
            message = (
                "Music service is currently reconnecting. Please try again in a moment."
            )
            result = EnsureConnectionResult(player=None)
            self._set_enqueue_error(guild.id, message)
            result.error = message
            return result

        result = EnsureConnectionResult(player=None)

        now = time.time()
        cool = self.connection_cooldowns.get(guild.id, 0)
        if cool > now:
            message = "Connection temporarily rate-limited. Please wait a moment and try again."
            self._set_enqueue_error(guild.id, message)
            result.error = message
            return result

        existing_vc = getattr(guild, "voice_client", None)
        if (
            not existing_vc or not is_voice_connected(existing_vc)
        ) and self.current_active_instances() >= self.get_global_instance_limit():
            message = (
                "Maximum number of active music players reached."
                " Try again after another guild stops playback."
            )
            self._set_enqueue_error(guild.id, message)
            result.error = message
            return result

        member = guild.get_member(requester_id)
        channel = getattr(getattr(member, "voice", None), "channel", None)
        if channel is None:
            message = "You need to join a voice channel before using music commands."
            self._set_enqueue_error(guild.id, message)
            result.error = message
            return result

        player = existing_vc if isinstance(existing_vc, pomice.Player) else None
        if player and is_voice_connected(player):
            result.player = player
            result.joined_channel = getattr(player.channel, "name", None)
            result.joined = False
            return result

        try:
            player = await channel.connect(cls=pomice.Player)
            if not hasattr(player, "queue"):
                player.queue = pomice.Queue()
            cfg = self._get_music_config(guild.id)

            remember_volume = cfg.get("RememberLastVolume", False)
            if remember_volume:
                # Use saved volume from RememberLastVolumeBetweenSessions (0.0-2.0 range)
                volume = float(cfg.get("RememberLastVolumeBetweenSessions", 1.0) or 1.0)
            else:
                # Default to 100 (1.0 in 0-2.0 range) when not persisting
                volume = 1.0

            try:
                await player.set_volume(int(max(0.0, min(2.0, volume)) * 100))
            except Exception:
                pass
            await self._apply_sponsorblock_settings(guild.id)
            self._playing_flags[guild.id] = False
            self.reset_session_state(guild.id)
            result.player = player
            result.joined_channel = getattr(channel, "name", None)
            result.joined = True
            return result
        except Exception as e:
            LOG.warning("Player connect failed in guild %s: %s", guild.id, e)
            self.connection_cooldowns[guild.id] = now + 60
            message = f"Failed to connect to voice channel: {e}"
            self._set_enqueue_error(guild.id, message)
            result.error = message
            return result

    async def _play_next(self, player: pomice.Player):
        if player.queue.is_empty:
            await self._schedule_idle_disconnect(player)
            return
        try:
            track: pomice.Track = player.queue.get()
            meta = self.queues[player.guild.id]
            entry = meta.popleft() if meta else {}
            entry.setdefault("title", getattr(track, "title", "Unknown"))
            entry.setdefault("author", getattr(track, "author", None))
            entry.setdefault("requester", None)
            entry.setdefault("length", getattr(track, "length", None))
            entry["uri"] = getattr(track, "uri", None)
            entry["identifier"] = getattr(track, "identifier", None)
            entry["track"] = track
            self._current_entries[player.guild.id] = entry
            self.voteskip[player.guild.id].clear()
            self._playing_flags[player.guild.id] = True
            await player.play(track)
            await self._cancel_idle(player.guild.id)
        except Exception as e:
            LOG.warning("Failed to start track: %s", e)
            self._playing_flags[player.guild.id] = False
            await self._advance_or_idle(player)

    def _initialize_playback_state(
        self,
        guild_id: int,
        track: Optional[pomice.Track],
        entry: Optional[Dict[str, Any]] = None,
    ) -> None:
        data = entry or self._current_entries.get(guild_id, {}) or {}
        length = data.get("length")
        if length is None and track is not None:
            length = getattr(track, "length", None) or getattr(track, "duration", None)
        length_ms: Optional[int]
        try:
            length_ms = int(length) if length is not None else None
        except (TypeError, ValueError):
            length_ms = None

        self._playback_state[guild_id] = {
            "length_ms": length_ms,
            "last_known_position_ms": 0,
            "position_timestamp": time.time(),
        }
        self._pending_feedback.pop(guild_id, None)

    def _refresh_player_position(self, player: pomice.Player, guild_id: int) -> None:
        state = self._playback_state.get(guild_id)
        if not state:
            return
        try:
            pos = getattr(player, "position", None)
            if callable(pos):
                pos = pos()
            if inspect.isawaitable(pos):
                return
            if isinstance(pos, (int, float)):
                state["last_known_position_ms"] = max(0, int(pos))
                state["position_timestamp"] = time.time()
        except Exception:
            pass

    def _estimate_progress_ratio(
        self, player: pomice.Player, guild_id: int
    ) -> Optional[float]:
        state = self._playback_state.get(guild_id)
        if state is None:
            entry = self._current_entries.get(guild_id, {})
            track = entry.get("track") if isinstance(entry, dict) else None
            self._initialize_playback_state(guild_id, track, entry)
            state = self._playback_state.get(guild_id)
        if not state:
            return None

        self._refresh_player_position(player, guild_id)

        length_ms = state.get("length_ms")
        if not length_ms or length_ms <= 0:
            return None

        position_ms = max(0.0, float(state.get("last_known_position_ms", 0)))
        timestamp = state.get("position_timestamp")
        if timestamp is not None:
            elapsed_ms = max(0.0, (time.time() - timestamp) * 1000.0)
            position_ms = min(position_ms + elapsed_ms, float(length_ms))
            state["last_known_position_ms"] = int(position_ms)
            state["position_timestamp"] = time.time()

        ratio = position_ms / float(length_ms)
        return max(0.0, min(1.0, ratio))

    def note_seek(self, guild_id: int, position_ms: int) -> None:
        state = self._playback_state.get(guild_id)
        if state is None:
            entry = self._current_entries.get(guild_id, {})
            track = entry.get("track") if isinstance(entry, dict) else None
            self._initialize_playback_state(guild_id, track, entry)
            state = self._playback_state.get(guild_id)
        if state is None:
            return
        try:
            state["last_known_position_ms"] = max(0, int(position_ms))
        except (TypeError, ValueError):
            state["last_known_position_ms"] = 0
        state["position_timestamp"] = time.time()

    async def note_skip(
        self,
        pomice_player: Optional[pomice.Player],
        guild_id: int,
        user_id: Optional[int] = None,
        *,
        primary_listener_bias: bool = False,
    ) -> None:
        if not isinstance(pomice_player, pomice.Player):
            return
        ratio = self._estimate_progress_ratio(pomice_player, guild_id)
        if ratio is None:
            ratio = 0.0
        self._pending_feedback[guild_id] = {
            "ratio": max(0.0, min(1.0, float(ratio))),
            "user_id": user_id,
            "primary_listener_bias": primary_listener_bias,
            "timestamp": time.time(),
        }

    async def _record_playback_feedback_for_current(
        self,
        player: pomice.Player,
        guild_id: int,
        *,
        pending: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        if not self._lastfm_autoplay or not self._lastfm_autoplay.is_available():
            return

        entry = self._current_entries.get(guild_id) or {}
        track = entry.get("track") if isinstance(entry, dict) else None
        artist = entry.get("author") or getattr(track, "author", None)
        title = entry.get("title") or getattr(track, "title", None)

        if not artist or not title:
            return

        ratio = None
        if pending:
            ratio = pending.get("ratio")
        if ratio is None:
            state = self._playback_state.get(guild_id)
            if state and state.get("recorded"):
                return
            ratio = self._estimate_progress_ratio(player, guild_id)
            state = self._playback_state.get(guild_id)
        if ratio is None:
            return

        if reason and reason.upper() == "FINISHED":
            ratio = 1.0

        ratio = max(0.0, min(1.0, float(ratio)))
        primary_bias = bool(pending.get("primary_listener_bias")) if pending else False

        duration_ms = None
        state = self._playback_state.get(guild_id)
        if state:
            duration_ms = state.get("length_ms")

        try:
            await self._lastfm_autoplay.record_playback_feedback(
                guild_id,
                artist,
                title,
                ratio,
                duration_ms=duration_ms,
                primary_listener_bias=primary_bias,
            )
            if state is not None:
                state["recorded"] = True
        except Exception:
            LOG.debug(
                "Failed to record playback feedback for %s - %s",
                artist,
                title,
                exc_info=True,
            )

    async def _advance_or_idle(self, player: pomice.Player):
        if not player.queue.is_empty:
            await self._play_next(player)
        else:
            autoplayed = await self._maybe_autoplay(player)
            if not autoplayed:
                await self._schedule_idle_disconnect(player)

    def _has_user_queued_tracks(self, guild_id: int) -> bool:
        """Check if there are any user-queued (non-autoplay) tracks in the queue."""
        queue_entries = self.queues.get(guild_id, [])
        for entry in queue_entries:
            if not entry.get("autoplay", False):
                return True
        return False

    async def _maybe_autoplay(self, player: pomice.Player) -> bool:
        """Attempt to queue autoplay tracks when queue is empty."""
        guild_id = player.guild.id

        LOG.debug(
            f"[AutoPlay] Checking if autoplay should trigger for guild {guild_id}"
        )

        # Check if autoplay is enabled for this session
        if not self.is_session_autoplay_enabled(guild_id):
            LOG.debug(f"[AutoPlay] AutoPlay is DISABLED for guild {guild_id}")
            return False

        # Don't autoplay if there are user-queued tracks
        if self._has_user_queued_tracks(guild_id):
            LOG.debug(
                f"[AutoPlay] User tracks in queue for guild {guild_id}, skipping autoplay"
            )
            return False

        # Get current track info for seed
        current_entry = self._current_entries.get(guild_id)
        if not current_entry:
            LOG.debug(f"[AutoPlay] No current track entry for guild {guild_id}")
            return False

        LOG.info(
            f"✨ [AutoPlay] AutoPlay is now recommending your next track for guild {guild_id}!"
        )

        # Try Last.fm-based recommendations first
        lastfm_success = False
        if self._lastfm_autoplay and self._lastfm_autoplay.is_available():
            LOG.info(f"[AutoPlay] Using Last.fm recommendations...")
            try:
                lastfm_success = await self._lastfm_autoplay_enqueue(
                    player, current_entry, guild_id
                )
            except Exception as e:
                LOG.error(f"[AutoPlay] Last.fm autoplay error in guild {guild_id}: {e}")
                import traceback

                traceback.print_exc()
        else:
            LOG.warning(f"[AutoPlay] Last.fm not available, will use YouTube fallback")

        # Fallback to YouTube-based recommendations if Last.fm failed
        if not lastfm_success:
            LOG.info(f"[AutoPlay] Falling back to YouTube recommendations...")
            lastfm_success = await self._youtube_autoplay_fallback(
                player, current_entry, guild_id
            )

        return lastfm_success

    async def _lastfm_autoplay_enqueue(
        self, player: pomice.Player, current_entry: Dict[str, Any], guild_id: int
    ) -> bool:
        """Use Last.fm recommendations for autoplay."""
        LOG.info(
            f"[AutoPlay] Getting Last.fm recommendations based on: {current_entry.get('title', 'Unknown')} by {current_entry.get('author', 'Unknown')}"
        )

        track_info = {
            "title": current_entry.get("title", ""),
            "author": current_entry.get("author", ""),
            "length": current_entry.get("length"),
            "guild_id": guild_id,
        }

        # Check if this is the first autoplay track in this session
        is_first_autoplay = not self._autoplay_session_started.get(guild_id, False)

        # Send "recommending" message only on first autoplay
        if is_first_autoplay:
            await self._send_autoplay_recommending_message(player.guild)
            self._autoplay_session_started[guild_id] = True

        # Get recommendations
        recommendations = await self._lastfm_autoplay.get_recommendations_for_track(
            track_info, limit=1  # Queue 1 track at a time
        )

        if not recommendations:
            LOG.warning(
                f"[AutoPlay] ⚠️ No Last.fm recommendations found for current track in guild {guild_id}"
            )

            # Try fallback to last successful autoplay track
            last_successful = self._last_successful_autoplay_track.get(guild_id)
            if last_successful:
                LOG.info(
                    f"[AutoPlay] 🔄 Trying fallback: Last successful track '{last_successful.get('title', 'Unknown')}' by {last_successful.get('author', 'Unknown')}"
                )
                recommendations = (
                    await self._lastfm_autoplay.get_recommendations_for_track(
                        last_successful, limit=1
                    )
                )

                if recommendations:
                    LOG.info(
                        f"[AutoPlay] ✅ Fallback successful! Found recommendations from last successful track"
                    )
                else:
                    LOG.warning(
                        f"[AutoPlay] ❌ Fallback also failed - no recommendations from last successful track"
                    )

            if not recommendations:
                # Resort to YouTube-based fallback recommendations
                yt_success = await self._youtube_autoplay_fallback(
                    player, current_entry, guild_id
                )
                if yt_success:
                    LOG.info(
                        "[AutoPlay] ▶️ Switched to YouTube fallback recommendations"
                    )
                    return True

            if not recommendations:
                # Send message to channel
                await self._send_autoplay_no_recommendations_message(
                    player.guild,
                    current_entry.get("title", "Unknown"),
                    current_entry.get("author", "Unknown"),
                )
                return False

        # Enqueue the first recommended track
        track_key, playable_track = recommendations[0]
        LOG.info(f"[AutoPlay] 🎵 Found recommendation: {playable_track.title}")

        try:
            # Create entry for the track
            item = {
                "title": playable_track.title,
                "source": playable_track,
                "requester": "autoplay",  # Special marker for autoplay
                "autoplay": True,
            }

            result = await self.enqueue(
                player.guild, item, from_autoplay=True, player=player
            )

            if result.success:
                LOG.info(f"▶️ [AutoPlay] Now playing: {playable_track.title}")
                # Store this as last successful autoplay track
                self._last_successful_autoplay_track[guild_id] = track_info.copy()

                # Send appropriate notification based on whether this is first or consecutive track
                if is_first_autoplay:
                    await self._send_autoplay_started_message(
                        player.guild, playable_track.title
                    )
                else:
                    await self._send_autoplay_now_playing_message(
                        player.guild, playable_track.title
                    )

                return True
            else:
                LOG.warning(f"[AutoPlay] Failed to enqueue track: {result}")
                return False
        except Exception as e:
            LOG.error(f"[AutoPlay] Failed to enqueue Last.fm recommendation: {e}")
            import traceback

            traceback.print_exc()
            return False

    async def _youtube_autoplay_fallback(
        self, player: pomice.Player, current_entry: Dict[str, Any], guild_id: int
    ) -> bool:
        """Fallback to YouTube-based autoplay recommendations."""
        LOG.debug(f"Using YouTube autoplay fallback for guild {guild_id}")

        # Use existing recommend method
        recs = await self.recommend(player.guild, max_rec=1)
        if not recs:
            return False

        # Queue only the first recommendation
        rec = recs[0]
        item = {
            "title": rec.get("title", "AutoPlay"),
            "source": rec.get("url"),
            "requester": "autoplay",  # Special marker for autoplay
            "autoplay": True,
        }

        result = await self.enqueue(
            player.guild, item, from_autoplay=True, player=player
        )

        if result.success:
            LOG.debug(f"Queued 1 YouTube autoplay track for guild {guild_id}")
            # Send notification that autoplay started
            await self._send_autoplay_started_message(
                player.guild, rec.get("title", "AutoPlay")
            )
            return True

        return False

    async def _delete_now_playing_message(self, guild_id: int) -> None:
        message = self._now_playing_messages.pop(guild_id, None)
        if not message:
            return
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        except Exception:
            LOG.debug(
                "Failed to delete now playing message in guild %s",
                guild_id,
                exc_info=True,
            )

    async def _announce_now_playing(self, player: pomice.Player, track: pomice.Track):
        guild = player.guild
        txt = self._get_announcement_channel(guild)
        if not txt:
            return
        try:
            await self._delete_now_playing_message(guild.id)
            title = getattr(track, "title", "Unknown")
            author = getattr(track, "author", "Unknown")
            uri = getattr(track, "uri", None)

            # Get requester info from current entry
            current_entry = self._current_entries.get(guild.id, {})
            requester_id = current_entry.get("requester")

            voice_channel = getattr(player, "channel", None)
            channel_name = getattr(voice_channel, "name", "Unknown channel")

            # Create embed matching !np command style
            if uri:
                embed = discord.Embed(
                    title="Now Playing",
                    description=f"[{title}]({uri})",
                    color=0x510F7B,  # Rythm's purple color
                )
            else:
                embed = discord.Embed(
                    title="Now Playing",
                    description=title,
                    color=0x510F7B,
                )

            # Add thumbnail from artwork
            artwork_url = getattr(track, "artwork_url", None)
            if artwork_url:
                embed.set_thumbnail(url=artwork_url)
            elif uri and "youtube.com/watch?v=" in uri:
                video_id = uri.split("v=")[1].split("&")[0]
                embed.set_thumbnail(
                    url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                )
            elif uri and "youtu.be/" in uri:
                video_id = uri.split("youtu.be/")[1].split("?")[0]
                embed.set_thumbnail(
                    url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                )

            # Add duration with seekbar
            dur = getattr(track, "length", None)
            if dur:
                total_seconds = int(dur / 1000)
                minutes, seconds = divmod(total_seconds, 60)
                duration_str = f"{minutes:02d}:{seconds:02d}"

                # Visual seekbar (at start of track)
                seekbar = "▰" + "▱" * 15
                embed.description += (
                    f"\n\n**Duration**\n00:00 {seekbar} {duration_str}\n"
                )

            # Add requester as author
            if requester_id:
                try:
                    requester = guild.get_member(requester_id)
                    if requester:
                        embed.set_author(
                            name=f"Requested By: {requester.display_name}",
                            icon_url=requester.display_avatar.url,
                        )
                    else:
                        embed.set_author(name=f"Requested By: User#{requester_id}")
                except Exception:
                    embed.set_author(name=f"Requested By: <@{requester_id}>")

            # Add voice channel location with speaker emoji
            embed.add_field(
                name=" ",
                value=f":speaker: {channel_name}",
                inline=False,
            )

            # Import PlayerControlView for interactive buttons
            try:
                from commands.Music.nowplaying import PlayerControlView
                import discord.ext.commands as cmd_module

                # Create a fake context for the view
                class FakeContext:
                    def __init__(self, bot, guild, author_id, channel):
                        self.bot = bot
                        self.guild = guild
                        self.author = type("obj", (object,), {"id": author_id})
                        self.channel = channel  # Add channel attribute

                fake_ctx = FakeContext(
                    self.bot, guild, requester_id or self.bot.user.id, txt
                )
                view = PlayerControlView(
                    fake_ctx, self, persistent=True, is_announcement=True
                )
                message = await txt.send(embed=embed, view=view)
                view.message = message
                self._now_playing_messages[guild.id] = message
            except Exception as e:
                # Fallback: send without buttons if import fails
                LOG.debug(f"Failed to add buttons to announcement: {e}")
                message = await txt.send(embed=embed)
                self._now_playing_messages[guild.id] = message
        except Exception:
            pass

    async def _schedule_idle_disconnect(
        self,
        player: pomice.Player,
        *,
        timeout: Optional[int] = None,
        reason: str = "idle",
    ) -> None:
        gid = player.guild.id
        await self._cancel_idle(gid)

        channel = getattr(player, "channel", None)
        members = getattr(channel, "members", []) if channel else []
        listeners = [m for m in members if not getattr(m, "bot", False)]

        idle_timeout = self._resolve_disconnect_timeout(gid, "idle")
        empty_timeout = self._resolve_disconnect_timeout(gid, "empty")

        if timeout is None:
            if not listeners:
                timeout = empty_timeout
                reason = "empty"
            else:
                timeout = idle_timeout
                reason = "idle"

        async def _idle() -> None:
            try:
                await asyncio.sleep(timeout)
                if not is_voice_connected(player):
                    return
                channel_now = getattr(player, "channel", None)
                members_now = getattr(channel_now, "members", []) if channel_now else []
                listeners_now = [m for m in members_now if not getattr(m, "bot", False)]

                queue_obj = getattr(player, "queue", None)
                queue_empty = (
                    getattr(queue_obj, "is_empty", True)
                    if queue_obj is not None
                    else True
                )

                if reason == "empty":
                    if listeners_now:
                        return
                else:
                    if listeners_now and (
                        is_voice_playing(player) or is_voice_paused(player)
                    ):
                        return
                    if not queue_empty:
                        return
                    if is_voice_playing(player) or is_voice_paused(player):
                        return

                self._playing_flags[gid] = False
                guild = player.guild
                try:
                    await player.disconnect()
                finally:
                    self.reset_session_state(gid)
                    category = (
                        "disconnected_due_to_empty_channel"
                        if reason == "empty"
                        else "disconnected_by_inactivity"
                    )
                    await self.send_disconnect_message(guild, category)
            except Exception:
                LOG.debug("Idle disconnect failed for guild %s", gid, exc_info=True)

        self._idle_tasks[gid] = asyncio.create_task(_idle())

    async def _cancel_idle(self, guild_id: int):
        t = self._idle_tasks.pop(guild_id, None)
        if t:
            t.cancel()

    def _get_music_config(self, guild_id: int) -> Dict[str, Any]:
        try:
            return json_get(guild_id).get("Music", {})
        except Exception:
            return {}

    async def _apply_sponsorblock_settings(self, guild_id: int) -> None:
        cfg = self._get_music_config(guild_id)
        enabled = cfg.get("SponsorBlockEnabled", True)
        categories = list(cfg.get("SponsorBlockCategories", []))
        if not cfg.get("RemoveNonSongsUsingSponsorBlock", True):
            categories = [c for c in categories if c not in NON_SONG_SEGMENTS]

        categories = [c for c in categories if c in SPONSORBLOCK_ALLOWED]

        method = "PUT" if enabled and categories else "DELETE"

        node_pool_cls = getattr(pomice, "NodePool", None)
        if node_pool_cls is None:
            return

        node = None
        try:
            node = node_pool_cls.get_node()
        except Exception:
            return

        if not node:
            return

        session_id = getattr(node, "session_id", None) or getattr(
            node, "_session_id", None
        )
        if not session_id:
            return

        scheme = "https" if LAVALINK_SECURE else "http"
        base_url = f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}"
        endpoint = f"{base_url}/v4/sessions/{session_id}/players/{guild_id}/sponsorblock/categories"
        headers = {
            "Authorization": LAVALINK_PASSWORD,
            "Content-Type": "application/json",
        }

        ssl_param = None if LAVALINK_SECURE else False

        async with self._sponsorblock_lock:
            timeout = aiohttp.ClientTimeout(total=10)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    if method == "PUT":
                        await session.put(
                            endpoint, json=categories, headers=headers, ssl=ssl_param
                        )
                    else:
                        await session.delete(endpoint, headers=headers, ssl=ssl_param)
            except Exception as exc:
                LOG.debug(
                    "Failed to sync SponsorBlock categories for guild %s: %s",
                    guild_id,
                    exc,
                )

    def _record_recent(self, entry: Dict[str, Any]) -> None:
        requester = entry.get("requester")
        if not requester:
            return

        history = getattr(self.bot, "_recent_tracks", None)
        if history is None:
            history = {}
            self.bot._recent_tracks = history

        dq = history.get(requester)
        if dq is None:
            dq = deque(maxlen=25)
            history[requester] = dq

        payload = {
            "title": entry.get("title", "Unknown"),
            "requester": requester,
        }
        source = entry.get("uri") or entry.get("title")
        if source:
            payload["source"] = source
        dq.appendleft(payload)

    def _print_lavalink_setup_help(self):
        jar_dir = os.path.join(os.path.dirname(__file__), "lavalink")
        app_yml_path = os.path.join(jar_dir, "application.yml")
        print("\n========== Lavalink Setup Assistance ==========")
        print("Automatic Lavalink provisioning failed. You can configure it manually:")
        print("1) Install Java 17+ (Adoptium or Microsoft Build).")
        print(
            "2) Download Lavalink v4.1.1 from https://github.com/lavalink-devs/Lavalink/releases/tag/4.1.1"
        )
        print(f"3) Place 'Lavalink.jar' in: {jar_dir}")
        print(
            f"4) Ensure 'application.yml' exists next to the jar (path: {app_yml_path}) with youtube-source and SponsorBlock plugins."
        )
        print("------------------------------------------------------------")
        print(
            f"""server:
    port: {LAVALINK_PORT}
    address: 0.0.0.0

lavalink:
    server:
        password: "{LAVALINK_PASSWORD}"
        sources:
            youtube: false
            bandcamp: true
            soundcloud: true
            twitch: true
            vimeo: true
            http: true
            local: false
    plugins:
        - dependency: "dev.lavalink.youtube:youtube-plugin:{DEFAULT_YOUTUBE_PLUGIN_VERSION}"
            snapshot: false
        - dependency: "com.github.topi314.sponsorblock:sponsorblock-plugin:{DEFAULT_SPONSORBLOCK_PLUGIN_VERSION}"
            snapshot: false
"""
        )
        print("------------------------------------------------------------")
        print("5) Run Lavalink from that folder:  java -jar Lavalink.jar")
        print(
            f"6) Ensure your environment matches: HOST={LAVALINK_HOST} PORT={LAVALINK_PORT} PASSWORD={LAVALINK_PASSWORD} SECURE={'true' if LAVALINK_SECURE else 'false'}\n"
        )

    # ---- public API for commands ----
    async def enqueue(
        self,
        guild: discord.Guild,
        item: Dict[str, Any],
        *,
        from_autoplay: bool = False,
        player: Optional["pomice.Player"] = None,
        is_dj: bool = False,
    ) -> EnqueueResult:
        if not self._node_ready.is_set():
            try:
                await asyncio.wait_for(self._node_ready.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                self._set_enqueue_error(
                    guild.id,
                    self._bootstrap_error
                    or "Lavalink node is not connected yet. Check that Lavalink is running and reachable.",
                )
                return EnqueueResult(
                    success=False,
                    error=self._bootstrap_error
                    or "Lavalink node is not connected yet. Check that Lavalink is running and reachable.",
                )
        if not self._node_ready.is_set():
            error_msg = (
                self._bootstrap_error
                or "Lavalink node is not connected yet. Check that Lavalink is running and reachable."
            )
            self._set_enqueue_error(guild.id, error_msg)
            return EnqueueResult(success=False, error=error_msg)

        joined_channel: Optional[str] = None
        if player is None or not is_voice_connected(player):
            conn = await self.ensure_player_connected(guild, item.get("requester"))
            if not conn.player:
                error_msg = conn.error or self._enqueue_errors.get(guild.id)
                if not error_msg:
                    error_msg = "Unable to join voice channel. Please verify permissions and try again."
                    self._set_enqueue_error(guild.id, error_msg)
                return EnqueueResult(success=False, error=error_msg)
            player = conn.player
            if conn.joined:
                joined_channel = conn.joined_channel

        cfg = self._get_music_config(guild.id)
        queue_limit_enabled = cfg.get("QueueLimitEnabled", True)
        queue_limit = int(cfg.get("QueueLimit", 10) or 10)
        playlist_limit = int(cfg.get("PlaylistAddLimit", 10) or 10)
        track_max_duration = int(cfg.get("TrackMaxDuration", 600) or 600)

        query = item.get("source") or item.get("title")
        is_url = isinstance(query, str) and (
            query.startswith("http://") or query.startswith("https://")
        )
        tracks: List[pomice.Track] = []

        def _normalize_results(
            results_obj: Any, prefer_single: bool
        ) -> List[pomice.Track]:
            if isinstance(results_obj, pomice.Playlist):
                return list(results_obj.tracks)[:playlist_limit]
            if isinstance(results_obj, list):
                return list(results_obj[:1]) if prefer_single else list(results_obj)
            if results_obj:
                return [results_obj]
            return []

        try:
            results = await player.get_tracks(query=str(query or ""))
            tracks = _normalize_results(results, prefer_single=not is_url)
        except Exception as e:
            LOG.warning("Track fetch failed: %s", e)
            self._set_enqueue_error(
                guild.id,
                f"Failed to fetch tracks for '{query}': {e}",
            )
            error_msg = self._enqueue_errors.get(guild.id)
            return EnqueueResult(success=False, error=error_msg)

        if not tracks and not is_url:
            search_term = str(item.get("title") or "").strip()
            if isinstance(query, str) and ":" in query:
                _, _, tail = query.partition(":")
                search_term = search_term or tail
            fallback_queries = []
            if search_term:
                fallback_queries.extend(
                    [
                        f"ytsearch:{search_term}",
                        f"ytmsearch:{search_term}",
                        f"scsearch:{search_term}",
                    ]
                )
            else:
                fallback_queries.append("ytsearch:" + str(query))

            seen_terms = {str(query or "").lower()}
            for fq in fallback_queries:
                if not fq or fq.lower() in seen_terms:
                    continue
                seen_terms.add(fq.lower())
                try:
                    results = await player.get_tracks(query=fq)
                    candidate_tracks = _normalize_results(results, prefer_single=True)
                except Exception as exc:
                    LOG.debug("Fallback track lookup failed for %s: %s", fq, exc)
                    continue
                if candidate_tracks:
                    tracks = candidate_tracks
                    break

        if not tracks:
            self._set_enqueue_error(
                guild.id,
                "No playable tracks were found for that query.",
            )
            error_msg = self._enqueue_errors.get(guild.id)
            return EnqueueResult(success=False, error=error_msg)

        total_in_queue = player.queue.count
        queued = 0
        first_track_title: Optional[str] = None
        first_queue_position: Optional[int] = None
        filtered_by_duration = 0
        LOG.debug(
            "Guild %s enqueue: track_max_duration=%s, queue_limit=%s, tracks=%d",
            guild.id,
            track_max_duration,
            queue_limit if queue_limit_enabled else "disabled",
            len(tracks),
        )
        for tr in tracks:
            # Check queue limit (DJ can bypass)
            if (
                not from_autoplay
                and not is_dj
                and queue_limit_enabled
                and total_in_queue + queued >= queue_limit
            ):
                self._set_enqueue_error(
                    guild.id,
                    f"❌ Queue limit reached ({queue_limit} tracks). DJs can bypass this limit.",
                )
                break

            # Check track duration limit (always enforced, even for DJ)
            track_length_ms = getattr(tr, "length", 0)
            if track_max_duration > 0 and track_length_ms > track_max_duration * 1000:
                filtered_by_duration += 1
                LOG.debug(
                    "Filtered track '%s' (length=%dms > limit=%dms)",
                    getattr(tr, "title", "Unknown"),
                    track_length_ms,
                    track_max_duration * 1000,
                )
                continue
            try:
                player.queue.put(tr)
                entry = {
                    "title": getattr(tr, "title", "Unknown"),
                    "requester": item.get("requester"),
                    "uri": getattr(tr, "uri", None),
                    "autoplay": from_autoplay,
                    "track": tr,
                }
                self.queues[guild.id].append(entry)
                queued += 1
                if first_track_title is None:
                    first_track_title = entry["title"]
                    first_queue_position = total_in_queue + queued
            except Exception as exc:
                LOG.warning(
                    "Failed to queue track '%s': %s",
                    getattr(tr, "title", "Unknown"),
                    exc,
                )
                break

        playback_active = (
            self._playing_flags.get(guild.id, False)
            or is_voice_playing(player)
            or is_voice_paused(player)
        )

        if not playback_active:
            self._playing_flags[guild.id] = True
            await self._play_next(player)

        if queued == 0:
            if guild.id not in self._enqueue_errors:
                if filtered_by_duration > 0:
                    minutes = track_max_duration // 60
                    seconds = track_max_duration % 60
                    time_str = (
                        f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
                    )
                    self._set_enqueue_error(
                        guild.id,
                        f"No tracks could be queued. {filtered_by_duration} track(s) exceeded the configured duration limit of {time_str}.",
                    )
                else:
                    self._set_enqueue_error(
                        guild.id,
                        "No tracks could be queued. Check logs for details.",
                    )
            error_msg = self._enqueue_errors.get(guild.id)
            return EnqueueResult(success=False, error=error_msg)

        self._enqueue_errors.pop(guild.id, None)
        started_playback = not playback_active
        return EnqueueResult(
            success=True,
            track_title=first_track_title,
            queue_position=first_queue_position,
            joined_channel=joined_channel,
            started_playback=started_playback,
            queued_count=queued,
        )

    async def recommend(
        self, guild: discord.Guild, max_rec: int = 3
    ) -> List[Dict[str, Any]]:
        try:
            max_rec_int = int(max_rec)
        except (TypeError, ValueError):
            max_rec_int = 3
        max_rec = max(1, min(DEFAULT_AUTOPLAY_MAX_RESULTS, max_rec_int))

        player = (
            guild.voice_client
            if isinstance(guild.voice_client, pomice.Player)
            else None
        )
        if not player:
            return []

        seed_entries: List[Dict[str, Any]] = list(self.queues.get(guild.id, []))
        current_entry = self._current_entries.get(guild.id)
        if current_entry:
            seed_entries.insert(0, current_entry)

        if not seed_entries:
            last_track = getattr(player, "_last_track", None)
            if last_track:
                seed_entries.append(
                    {
                        "title": getattr(last_track, "title", None),
                        "uri": getattr(last_track, "uri", None),
                    }
                )

        if not seed_entries:
            return []

        recs: List[Dict[str, Any]] = []
        seen_queries: set[str] = set()

        for entry in seed_entries[:10]:
            query = entry.get("uri") or entry.get("title")
            if not query or query in seen_queries:
                continue
            seen_queries.add(query)
            try:
                results = await player.get_tracks(query=str(query))
            except Exception:
                continue

            candidates: List[pomice.Track] = []
            if isinstance(results, pomice.Playlist):
                candidates = list(results.tracks)
            elif isinstance(results, list):
                candidates = list(results)
            elif results:
                candidates = [results]

            for cand in candidates:
                url = getattr(cand, "uri", None)
                title = getattr(cand, "title", None)
                if not url:
                    continue
                if url == entry.get("uri"):
                    continue
                recs.append({"title": title or "Unknown", "url": url})
                break
            if len(recs) >= max_rec:
                break

        return recs

    # votes
    def votes_needed(self, guild: discord.Guild) -> int:
        try:
            members = [m for m in guild.voice_client.channel.members if not m.bot]
        except Exception:
            return 1

        listeners = len(members)
        if listeners <= 1:
            return 1

        needed = math.ceil(listeners * (DEFAULT_VOTESKIP_PERCENT / 100.0))
        return max(1, needed)

    async def handle_vote_skip(
        self, guild: discord.Guild, user_id: int
    ) -> tuple[bool, int, int]:
        s = self.voteskip[guild.id]
        if user_id in s:
            return False, len(s), self.votes_needed(guild)
        s.add(user_id)
        return True, len(s), self.votes_needed(guild)

    async def _check_dj(self, user: discord.Member, guild: discord.Guild) -> bool:
        """
        Check if user has DJ role or admin permissions.
        Returns True if user has DJ permissions, False otherwise.
        """
        # Check admin permissions first
        if user.guild_permissions.administrator or user.guild_permissions.manage_guild:
            return True

        # Check DJ role
        cfg = self._get_music_config(guild.id)
        dj_role_id = cfg.get("DJRole")

        if dj_role_id:
            try:
                return any(r.id == int(dj_role_id) for r in user.roles)
            except Exception:
                pass

        return False

    async def _check_dj_mode_permission(
        self, user: discord.Member, guild: discord.Guild, action: str
    ) -> dict:
        """
        Check if user can perform action based on DJ mode settings.

        Returns dict with:
            - allowed: bool - Can perform action without voting
            - needs_vote: bool - Needs to vote for action
            - error: Optional[str] - Error message if not allowed
        """
        cfg = self._get_music_config(guild.id)
        dj_mode = cfg.get("DJMode", "DJ Vote Bypass")
        is_dj = await self._check_dj(user, guild)

        # DJ Only mode: Only DJ can control
        if dj_mode == "DJ Only":
            if is_dj:
                return {"allowed": True, "needs_vote": False, "error": None}
            else:
                return {
                    "allowed": False,
                    "needs_vote": False,
                    "error": "❌ DJ role required to control music.",
                }

        # DJ Vote Bypass: DJ bypasses votes, others must vote
        elif dj_mode == "DJ Vote Bypass":
            if is_dj:
                return {"allowed": True, "needs_vote": False, "error": None}
            else:
                return {"allowed": True, "needs_vote": True, "error": None}

        # User Only: Everyone treated as non-DJ, must vote
        elif dj_mode == "User Only":
            return {"allowed": True, "needs_vote": True, "error": None}

        # Disabled: Anyone can control without voting
        elif dj_mode == "Disabled":
            return {"allowed": True, "needs_vote": False, "error": None}

        # Default to DJ Vote Bypass behavior
        else:
            if is_dj:
                return {"allowed": True, "needs_vote": False, "error": None}
            else:
                return {"allowed": True, "needs_vote": True, "error": None}

    async def handle_vote_action(
        self,
        guild_id: int,
        user_id: int,
        action: str,
        voice_channel: Optional[discord.VoiceChannel] = None,
    ) -> dict:
        """
        Handle voting for music actions.

        Args:
            guild_id: Guild ID
            user_id: User ID voting
            action: Action being voted on (e.g., "pause", "skip", "repeat_off")
            voice_channel: Voice channel to count members from

        Returns:
            dict with:
                - passed: bool - Whether vote threshold reached
                - votes: int - Current vote count
                - needed: int - Votes needed to pass
        """
        # Create vote key for this action
        vote_key = f"{guild_id}_{action}"

        if not hasattr(self, "_active_votes"):
            self._active_votes = defaultdict(set)

        # Add user's vote
        self._active_votes[vote_key].add(user_id)
        current_votes = len(self._active_votes[vote_key])

        # Calculate needed votes
        needed_votes = 1
        if voice_channel:
            try:
                members = [m for m in voice_channel.members if not m.bot]
                listeners = len(members)
                if listeners > 1:
                    needed_votes = math.ceil(
                        listeners * (DEFAULT_VOTESKIP_PERCENT / 100.0)
                    )
                    needed_votes = max(1, needed_votes)
            except Exception:
                needed_votes = 1

        # Check if vote passed
        passed = current_votes >= needed_votes

        # Clear votes if passed
        if passed:
            self._active_votes.pop(vote_key, None)

        return {"passed": passed, "votes": current_votes, "needed": needed_votes}

    def clear_votes_for_guild(self, guild_id: int) -> None:
        """Clear all active votes for a guild."""
        if not hasattr(self, "_active_votes"):
            return

        keys_to_remove = [
            k for k in self._active_votes.keys() if k.startswith(f"{guild_id}_")
        ]
        for key in keys_to_remove:
            self._active_votes.pop(key, None)

    async def _delete_now_playing_message(self, guild_id: int) -> None:
        """Delete the now playing message for a guild."""
        message = self._now_playing_messages.pop(guild_id, None)
        if message:
            try:
                await message.delete()
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
