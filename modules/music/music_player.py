import os
import asyncio
import time
import logging
from typing import Dict, Deque, Any, Optional, List
from collections import defaultdict
import discord
from discord.ext import commands, tasks
from modules.setconfig import json_get

import wavelink

LOG = logging.getLogger(__name__)

MAIN_GUILD = os.getenv("MAIN_GUILD")

LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
LAVALINK_SECURE = os.getenv("LAVALINK_SECURE", "false").lower() in ("1", "true", "yes")
LAVALINK_REGION = os.getenv("LAVALINK_REGION")  # optional
LAVALINK_AUTO_START = os.getenv("LAVALINK_AUTO_START", "false").lower() in (
    "1",
    "true",
    "yes",
)

INACTIVITY_SECONDS = 300  # 5 minutes


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
        self._node_ready = asyncio.Event()
        self._bootstrap_node.start()

    def cog_unload(self):
        self._bootstrap_node.cancel()
        for t in list(self._idle_tasks.values()):
            t.cancel()

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
        # Try to connect to configured node
        ok = await self._try_connect_node()
        if not ok and LAVALINK_AUTO_START:
            # Start local node then connect
            try:
                from .lavalink.manager import ensure_local_node

                started = await ensure_local_node(
                    host=LAVALINK_HOST,
                    port=LAVALINK_PORT,
                    password=LAVALINK_PASSWORD,
                    secure=LAVALINK_SECURE,
                )
                if started:
                    ok = await self._try_connect_node(retry_delay=2.0, attempts=10)
            except Exception as e:
                LOG.error("Failed to start local Lavalink node: %s", e)

        if ok:
            self._node_ready.set()
        else:
            LOG.error("Lavalink node unavailable. Music features will be limited.")

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
        if mode == "current" and last:
            await player.play(last)
            return
        if mode == "queue" and last:
            try:
                player.queue.put_nowait(last)
            except Exception:
                pass
        await self._advance_or_idle(player)

    # ---- helpers ----
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
            await player.play(track)
            await self._cancel_idle(player.guild.id)
        except Exception as e:
            LOG.warning("Failed to start track: %s", e)
            await self._advance_or_idle(player)

    async def _advance_or_idle(self, player: wavelink.Player):
        if not player.queue.is_empty:
            await self._play_next(player)
        else:
            await self._schedule_idle_disconnect(player)

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

        async def _idle():
            try:
                await asyncio.sleep(INACTIVITY_SECONDS)
                if player.queue.is_empty and not player.playing:
                    await player.disconnect()
            except Exception:
                pass

        self._idle_tasks[gid] = asyncio.create_task(_idle())

    async def _cancel_idle(self, guild_id: int):
        t = self._idle_tasks.pop(guild_id, None)
        if t:
            t.cancel()

    # ---- public API for commands ----
    async def enqueue(self, guild: discord.Guild, item: Dict[str, Any]) -> bool:
        player = (
            guild.voice_client
            if isinstance(guild.voice_client, wavelink.Player)
            else None
        )
        if not player or not player.is_connected():
            player = await self._ensure_player_connected(guild, item.get("requester"))
            if not player:
                return False

        cfg = json_get(guild.id).get("Music", {})
        queue_limit = int(cfg.get("QueueLimit", 10) or 10)
        playlist_limit = int(cfg.get("PlaylistAddLimit", 10) or 10)

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
            if total_in_queue + queued >= queue_limit:
                break
            try:
                player.queue.put_nowait(tr)
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
        if not player or player.queue.is_empty:
            return []
        recs: List[Dict[str, Any]] = []
        seen = set()
        for tr in list(player.queue._queue)[:10]:
            q = getattr(tr, "title", None) or getattr(tr, "uri", None)
            if not q:
                continue
            try:
                results = await wavelink.YouTubeTrack.search(q, return_first=False)
            except Exception:
                continue
            for cand in results:
                url = getattr(cand, "uri", None)
                if (
                    url
                    and url not in seen
                    and getattr(cand, "title", None) != getattr(tr, "title", None)
                ):
                    seen.add(url)
                    recs.append(
                        {"title": getattr(cand, "title", "Unknown"), "url": url}
                    )
                    break
            if len(recs) >= max_rec:
                break
        return recs

    # votes
    def votes_needed(self, guild: discord.Guild) -> int:
        cfg = json_get(guild.id)
        percent = int(cfg.get("Music", {}).get("VoteSkipPercent", 50) or 50)
        try:
            members = [m for m in guild.voice_client.channel.members if not m.bot]
            return max(1, int(len(members) * (percent / 100.0)))
        except Exception:
            return 3

    async def handle_vote_skip(
        self, guild: discord.Guild, user_id: int
    ) -> (bool, int, int):
        s = self.voteskip[guild.id]
        if user_id in s:
            return False, len(s), self.votes_needed(guild)
        s.add(user_id)
        return True, len(s), self.votes_needed(guild)


async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
