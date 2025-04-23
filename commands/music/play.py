from discord.ext import commands


class Play(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="play", aliases=["p"])
    async def play(self, ctx, *, query: str):
        music_player = self.bot.get_cog("MusicPlayer")
        if music_player is None:
            return await ctx.send("❌ Music subsystem not initialized.")
        await music_player.play_track(ctx, query)


async def setup(bot):
    await bot.add_cog(Play(bot))
