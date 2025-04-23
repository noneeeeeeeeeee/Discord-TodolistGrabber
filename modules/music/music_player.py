import discord
import lavalink
from discord.ext import commands


class MusicPlayer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.lavalink = lavalink.Client(bot.user.id)
        self.bot.lavalink.add_node(
            "localhost", 2333, "youshallnotpass", "na", "default-node"
        )
        self.volume = {}
        self.bot.lavalink.add_event_hooks(self)

    async def connect_to(self, guild_id: int, channel: discord.VoiceChannel):
        player = self.bot.lavalink.player_manager.create(guild_id)
        # store the voice channel for Lavalink
        player.store("channel", channel.id)
        # perform a real Discord connection
        await channel.connect()
        return player

    async def play_track(self, ctx, query: str):
        # Parse provider and query
        provider, query = self.parse_query(query)
        search_prefix = self.get_search_prefix(provider)

        # abort if no Lavalink nodes are up
        if not self.bot.lavalink.node_manager.available_nodes:
            return await ctx.send(
                "❌ Music server unavailable. Please try again later."
            )

        # attempt to fetch tracks
        try:
            results = await self.bot.lavalink.get_tracks(f"{search_prefix}{query}")
        except IndexError:
            return await ctx.send(
                "❌ Music server unavailable. Please try again later."
            )

        if not results.tracks:
            return await ctx.send("Nothing found!")

        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player:
            if not ctx.author.voice or not ctx.author.voice.channel:
                return await ctx.send("Join a voice channel first!")
            player = await self.connect_to(ctx.guild.id, ctx.author.voice.channel)

        # Add track to queue
        if results.load_type == lavalink.LoadType.PLAYLIST_LOADED:
            for track in results.tracks:
                player.add(requester=ctx.author.id, track=track)
            await ctx.send(
                f"Added playlist {results.playlist_info.name} with {len(results.tracks)} tracks"
            )
        else:
            track = results.tracks[0]
            player.add(requester=ctx.author.id, track=track)
            await ctx.send(f"Added to queue: {track.title}")

        if not player.is_playing:
            await player.play()

    def parse_query(self, query: str):
        providers = ["yt", "sc", "sp", "am", "dz"]
        parts = query.split(" ", 1)
        if parts[0].lower() in providers and len(parts) > 1:
            return parts[0].lower(), parts[1]
        return "yt", query

    def get_search_prefix(self, provider: str):
        prefixes = {
            "yt": "ytsearch:",
            "sc": "scsearch:",
            "sp": "spsearch:",
            "am": "amsearch:",
            "dz": "dzsearch:",
        }
        return prefixes.get(provider, "ytsearch:")

    @commands.Cog.listener()
    async def on_track_start(self, event):
        player = event.player
        guild_id = int(player.guild_id)
        self.volume[guild_id] = player.volume

    @commands.Cog.listener()
    async def on_track_end(self, event):
        if event.reason in ["FINISHED", "STOPPED"]:
            player = event.player
            if player.queue:
                await player.play()

    @commands.Cog.listener()
    async def on_socket_response(self, payload):
        t = payload.get("t")
        d = payload.get("d", {})
        if t == "VOICE_SERVER_UPDATE":
            await self.bot.lavalink.on_voice_server_update(d)
        if t == "VOICE_STATE_UPDATE":
            await self.bot.lavalink.on_voice_state_update(d)


async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
