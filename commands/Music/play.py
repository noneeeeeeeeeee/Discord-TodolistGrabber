import re
import discord
from discord.ext import commands
from modules.setconfig import check_guild_config_available
from typing import Optional


class PlayCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _get_player(self):
        return self.bot.get_cog("MusicPlayer")

    @commands.hybrid_command(name="p", description="Play/search a track or playlist")
    async def p(self, ctx: commands.Context, *, query: str):
        if not check_guild_config_available(ctx.guild.id):
            await ctx.send("Server not setup. Please run !setup.")
            return
        player = self._get_player()
        if not player:
            await ctx.send("Player backend not available.")
            return
        q = query.strip()
        if re.match(r"https?://(www\.)?youtu", q) or q.startswith("ytsearch1:"):
            source = q
        else:
            source = f"ytsearch1:{q}"
        item = {"title": q, "requester": ctx.author.id, "source": source}
        ok = await player.enqueue(ctx.guild, item)
        if not ok:
            await ctx.send(
                ":x: Cannot join voice due to global instance limit or full queue."
            )
            return
        await ctx.send(f"Queued: **{q}**")

    @commands.hybrid_command(
        name="playrecent", description="Queue your recently played tracks."
    )
    async def playrecent(self, ctx: commands.Context, count: int = 1):
        recent = list(getattr(self.bot, "_recent_tracks", {}).get(ctx.author.id, []))[
            :count
        ]
        if not recent:
            await ctx.send("No recent tracks found for you.")
            return
        player = self._get_player()
        added = 0
        for it in recent:
            ok = await player.enqueue(ctx.guild, it)
            if ok:
                added += 1
        await ctx.send(f"Added {added} recent track(s) to the queue.")


async def setup(bot):
    await bot.add_cog(PlayCommands(bot))
