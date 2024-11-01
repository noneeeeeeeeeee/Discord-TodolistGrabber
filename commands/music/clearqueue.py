import discord
from discord.ext import commands

class ClearQueue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="clearqueue", aliases=["clear", "cq"])
    async def clear_queue(self, ctx):
        """Clears the music queue."""
        music_player = self.bot.get_cog("MusicPlayer")
        guild_id = ctx.guild.id

        if guild_id in music_player.music_queue:
            music_player.music_queue[guild_id].clear()
            await ctx.send(":white_check_mark: The queue has been cleared.")
        else:
            await ctx.send(":x: The queue is already empty.")

async def setup(bot):
    await bot.add_cog(ClearQueue(bot))