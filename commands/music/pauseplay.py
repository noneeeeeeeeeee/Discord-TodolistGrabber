import discord
from discord.ext import commands


class PausePlay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="pauseplay", aliases=["pause", "resume", "pp"])
    async def pause_play(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player or not player.current:
            return await ctx.send("Nothing playing!")

        if player.paused:
            await player.set_pause(False)
            await ctx.send("▶️ Resumed")
        else:
            await player.set_pause(True)
            await ctx.send("⏸ Paused")


async def setup(bot):
    await bot.add_cog(PausePlay(bot))
