import os
import asyncio
import time
import math
import logging
from typing import Dict, Any, Optional, List
from collections import defaultdict, deque

import aiohttp
import discord
from discord.ext import commands, tasks
from modules.setconfig import json_get

import wavelink
from modules.music.lavalink.manager import (
    ensure_local_node,
    shutdown_local_node,
    is_local_node_managed,
    DEFAULT_YOUTUBE_PLUGIN_VERSION,
    DEFAULT_SPONSORBLOCK_PLUGIN_VERSION,
)

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


class MusicPlayer(commands.Cog):
    """
    Lavalink-based music backend (wavelink):
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
        self._local_node_managed = False
        self._sponsorblock_lock = asyncio.Lock()
        self._node_ready = asyncio.Event()
        self._node_help_printed = False
        self._bootstrap_node.start()

    def cog_unload(self):
        self._bootstrap_node.cancel()
        for t in list(self._idle_tasks.values()):
            t.cancel()
        if self._local_node_managed:
            asyncio.create_task(shutdown_local_node())

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
            if isinstance(vc, wavelink.Player) and vc.is_connected():
                n += 1
        return n

    # ---- node bootstrap ----
    @tasks.loop(count=1)
    async def _bootstrap_node(self):
        await self.bot.wait_until_ready()
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
        elif LAVALINK_AUTO_START:
            LOG.info(
                "LAVALINK_AUTO_START enabled but host %s is not local; skipping auto provisioning.",
                LAVALINK_HOST,
            )

        ok = await self._try_connect_node()
        if ok:
            self._node_ready.set()
        else:
            LOG.error("Lavalink node unavailable. Music features will be limited.")
            # Print setup instructions once
            if not self._node_help_printed:
                self._print_lavalink_setup_help()
                self._node_help_printed = True

    @_bootstrap_node.before_loop
    async def _before_bootstrap(self):
        await self.bot.wait_until_ready()

    async def _try_connect_node(
        self, retry_delay: float = 1.0, attempts: int = 3
    ) -> bool:
        for i in range(attempts):
            try:
                if wavelink.NodePool.nodes:
                    return True
                await wavelink.NodePool.create_node(
                    bot=self.bot,
                    host=LAVALINK_HOST,
                    port=LAVALINK_PORT,
                    password=LAVALINK_PASSWORD,
                    https=LAVALINK_SECURE,
                    region=LAVALINK_REGION or None,
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
                await asyncio.sleep(retry_delay)
        return False

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, node: wavelink.Node):
        LOG.info("Wavelink node '%s' ready.", node.identifier)
        self._node_ready.set()

    @commands.Cog.listener()
    async def on_wavelink_node_closed(self, payload: wavelink.NodeClosedPayload):
        LOG.warning("Wavelink node closed: %s", payload)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player: wavelink.Player = payload.player
        track: wavelink.Playable = payload.track
        setattr(player, "_last_track", track)
        await self._announce_now_playing(player, track)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: wavelink.Player = payload.player
        gid = player.guild.id
        mode = self.repeat_mode.get(gid, "none")
        last = getattr(player, "_last_track", None)
        entry = self._current_entries.get(gid)
        if entry:
            self._record_recent(entry)
        if mode == "current" and last:
            self.voteskip[gid].clear()
            await player.play(last)
            return
        if mode == "queue" and last:
            try:
                player.queue.put_nowait(last)
                meta = self._current_entries.get(gid)
                if meta:
                    clone = {
                        "title": meta.get("title"),
                        "requester": meta.get("requester"),
                        "uri": meta.get("uri"),
                        "autoplay": meta.get("autoplay", False),
                    }
                    self.queues[gid].append(clone)
            except Exception:
                pass
        await self._advance_or_idle(player)

    async def _ensure_player_connected(
        self, guild: discord.Guild, requester_id: int
    ) -> Optional[wavelink.Player]:
        await self._node_ready.wait()

        now = time.time()
        cool = self.connection_cooldowns.get(guild.id, 0)
        if cool > now:
            return None

        # enforce global instance limit
        if (
            not guild.voice_client or not guild.voice_client.is_connected()
        ) and self.current_active_instances() >= self.get_global_instance_limit():
            return None

        member = guild.get_member(requester_id)
        channel = getattr(getattr(member, "voice", None), "channel", None)
        if channel is None:
            return None

        try:
            player: wavelink.Player = await channel.connect(cls=wavelink.Player)
            if not hasattr(player, "queue"):
                player.queue = wavelink.Queue()
            cfg = self._get_music_config(guild.id)
            volume = float(cfg.get("Volume", 0.5) or 0.5)
            try:
                await player.set_volume(int(max(0.0, min(2.0, volume)) * 100))
            except Exception:
                pass
            await self._apply_sponsorblock_settings(guild.id)
            return player
        except Exception as e:
            LOG.warning("Player connect failed in guild %s: %s", guild.id, e)
            self.connection_cooldowns[guild.id] = now + 60
            return None

    async def _play_next(self, player: wavelink.Player):
        if player.queue.is_empty:
            await self._schedule_idle_disconnect(player)
            return
        try:
            track: wavelink.Playable = player.queue.get()
            meta = self.queues[player.guild.id]
            entry = meta.popleft() if meta else {}
            entry.setdefault("title", getattr(track, "title", "Unknown"))
            entry.setdefault("requester", None)
            entry["uri"] = getattr(track, "uri", None)
            entry["track"] = track
            self._current_entries[player.guild.id] = entry
            self.voteskip[player.guild.id].clear()
            await player.play(track)
            await self._cancel_idle(player.guild.id)
        except Exception as e:
            LOG.warning("Failed to start track: %s", e)
            await self._advance_or_idle(player)

    async def _advance_or_idle(self, player: wavelink.Player):
        if not player.queue.is_empty:
            await self._play_next(player)
        else:
            autoplayed = await self._maybe_autoplay(player)
            if not autoplayed:
                await self._schedule_idle_disconnect(player)

    async def _maybe_autoplay(self, player: wavelink.Player) -> bool:
        cfg = self._get_music_config(player.guild.id)
        if not cfg.get("AutoPlay", False):
            return False

        recs = await self.recommend(player.guild, max_rec=1)
        if not recs:
            return False

        autoplay_track = recs[0]
        item = {
            "title": autoplay_track.get("title", "AutoPlay"),
            "source": autoplay_track.get("url"),
            "requester": getattr(self.bot.user, "id", 0),
        }

        ok = await self.enqueue(player.guild, item, from_autoplay=True)
        if ok:
            LOG.debug("AutoPlay queued %s in guild %s", item["title"], player.guild.id)
        return ok

    async def _announce_now_playing(
        self, player: wavelink.Player, track: wavelink.Playable
    ):
        guild = player.guild
        txt = None
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                txt = ch
                break
        if not txt:
            return
        try:
            embed = discord.Embed(
                title="Now Playing",
                description=f"{getattr(track, 'title', 'Unknown')} — [{getattr(track, 'author', 'Unknown')}]",
                color=discord.Color.blurple(),
            )
            dur = getattr(track, "length", None)
            if dur:
                embed.add_field(name="Duration", value=f"{int(dur/1000)}s", inline=True)
            url = getattr(track, "uri", None)
            if url:
                embed.add_field(name="URL", value=f"[Link]({url})", inline=True)
            await txt.send(embed=embed)
        except Exception:
            pass

    async def _schedule_idle_disconnect(self, player: wavelink.Player):
        gid = player.guild.id
        await self._cancel_idle(gid)

        timeout = self._get_idle_timeout(gid)

        async def _idle():
            try:
                await asyncio.sleep(timeout)
                if player.queue.is_empty and not player.playing:
                    await player.disconnect()
            except Exception:
                pass

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

    def _get_idle_timeout(self, guild_id: int) -> int:
        cfg = self._get_music_config(guild_id)
        try:
            seconds = int(cfg.get("AutoDisconnectSeconds", 300) or 300)
        except Exception:
            seconds = 300
        return max(30, min(7200, seconds))

    async def _apply_sponsorblock_settings(self, guild_id: int) -> None:
        cfg = self._get_music_config(guild_id)
        enabled = cfg.get("SponsorBlockEnabled", True)
        categories = list(cfg.get("SponsorBlockCategories", []))
        if not cfg.get("RemoveNonSongsUsingSponsorBlock", True):
            categories = [c for c in categories if c not in NON_SONG_SEGMENTS]

        categories = [c for c in categories if c in SPONSORBLOCK_ALLOWED]

        method = "PUT" if enabled and categories else "DELETE"

        node = None
        try:
            node = wavelink.NodePool.get_node()
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
        # Console guidance for manual setup
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
    ) -> bool:
        # Return fast if node isn’t ready instead of waiting indefinitely
        if not self._node_ready.is_set():
            # try a short wait and then fail fast
            try:
                await asyncio.wait_for(self._node_ready.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                return False

        player = (
            guild.voice_client
            if isinstance(guild.voice_client, wavelink.Player)
            else None
        )
        if not player or not player.is_connected():
            player = await self._ensure_player_connected(guild, item.get("requester"))
            if not player:
                return False

        cfg = self._get_music_config(guild.id)
        queue_limit_enabled = cfg.get("QueueLimitEnabled", True)
        queue_limit = int(cfg.get("QueueLimit", 10) or 10)
        playlist_limit = int(cfg.get("PlaylistAddLimit", 10) or 10)
        track_max_duration = int(cfg.get("TrackMaxDuration", 600) or 600)

        query = item.get("source") or item.get("title")
        tracks: List[wavelink.Playable] = []
        try:
            if isinstance(query, str) and (
                query.startswith("http://") or query.startswith("https://")
            ):
                fetched = await wavelink.Pool.fetch_tracks(query)
                if isinstance(fetched, wavelink.Playlist):
                    tracks = list(fetched.tracks)[:playlist_limit]
                else:
                    tracks = list(fetched) if fetched else []
            else:
                res = await wavelink.YouTubeTrack.search(
                    query or "", return_first=False
                )
                tracks = res[:1] if res else []
        except Exception as e:
            LOG.warning("Track fetch failed: %s", e)
            return False

        if not tracks:
            return False

        total_in_queue = player.queue.count
        queued = 0
        for tr in tracks:
            if (
                not from_autoplay
                and queue_limit_enabled
                and total_in_queue + queued >= queue_limit
            ):
                break
            if (
                track_max_duration > 0
                and getattr(tr, "length", 0) > track_max_duration * 1000
            ):
                continue
            try:
                player.queue.put_nowait(tr)
                entry = {
                    "title": getattr(tr, "title", "Unknown"),
                    "requester": item.get("requester"),
                    "uri": getattr(tr, "uri", None),
                    "autoplay": from_autoplay,
                }
                self.queues[guild.id].append(entry)
                setattr(tr, "requester_id", item.get("requester"))
                setattr(tr, "is_autoplay", from_autoplay)
                queued += 1
            except Exception:
                break

        if not player.playing and not player.paused:
            await self._play_next(player)

        return queued > 0

    async def recommend(
        self, guild: discord.Guild, max_rec: int = 3
    ) -> List[Dict[str, Any]]:
        player = (
            guild.voice_client
            if isinstance(guild.voice_client, wavelink.Player)
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
                results = await wavelink.YouTubeTrack.search(query, return_first=False)
            except Exception:
                continue
            for cand in results:
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
        cfg = self._get_music_config(guild.id)
        percent = int(cfg.get("VoteSkipPercent", 60) or 0)
        floor = int(cfg.get("VoteSkipFloor", 2) or 1)
        try:
            members = [m for m in guild.voice_client.channel.members if not m.bot]
        except Exception:
            return max(1, floor)

        listeners = len(members)
        if listeners <= 1:
            return 1

        needed = math.ceil(listeners * (percent / 100.0)) if percent > 0 else 0
        return max(1, floor, needed)

    async def handle_vote_skip(
        self, guild: discord.Guild, user_id: int
    ) -> tuple[bool, int, int]:
        s = self.voteskip[guild.id]
        if user_id in s:
            return False, len(s), self.votes_needed(guild)
        s.add(user_id)
        return True, len(s), self.votes_needed(guild)


async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
