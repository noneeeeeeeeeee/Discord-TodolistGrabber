import discord
from discord.ext import commands
from modules.setconfig import json_get

class MusicQueue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="queue", aliases=["q"], description="Shows the Song Queue")
    async def queue(self, ctx):
        music_player = self.bot.get_cog("MusicPlayer")
        guild_id = ctx.guild.id

        if guild_id not in music_player.music_queue or not music_player.music_queue[guild_id]:
            await ctx.send(":x: The queue is currently empty.")
            return

        queue = music_player.music_queue[guild_id]
        queue_list = "\n".join([f"{idx + 1}. {title} ({duration // 60}:{duration % 60:02})" for idx, (_, title, duration) in enumerate(queue)])

        embed = discord.Embed(
            title="Current Song Queue",
            description=queue_list,
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(MusicQueue(bot))
