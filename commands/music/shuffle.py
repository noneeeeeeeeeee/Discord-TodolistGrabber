import discord
from discord.ext import commands
from discord.ext.commands import cooldown, BucketType
import random


class Shuffle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="shuffle", aliases=["mix"])
    @cooldown(1, 30, BucketType.guild)
    async def shuffle(self, ctx):
        """Shuffles the music queue."""
        music_player = self.bot.get_cog("MusicPlayer")
        guild_id = ctx.guild.id

        if (
            guild_id not in music_player.music_queue
            or not music_player.music_queue[guild_id]
        ):
            await ctx.send(":x: Cannot shuffle as the queue is empty.")
            return

        queue = music_player.music_queue[guild_id]
        random.shuffle(queue)
        await ctx.send(":twisted_rightwards_arrows: The queue has been shuffled.")
    @shuffle.error
    async def shuffle_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f":clock1: This command is on cooldown. Try again in {error.retry_after:.2f} seconds.")

async def setup(bot):
    await bot.add_cog(Shuffle(bot))
