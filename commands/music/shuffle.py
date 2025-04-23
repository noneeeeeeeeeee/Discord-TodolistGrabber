import discord
from discord.ext import commands
from discord.ext.commands import cooldown, BucketType
import random


class Shuffle(commands.Cog):
    @commands.hybrid_command(name="shuffle")
    async def shuffle(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player or not player.queue:
            return await ctx.send("Queue is empty!")

        random.shuffle(player.queue)
        await ctx.send("Shuffled the queue")


async def setup(bot):
    await bot.add_cog(Shuffle(bot))
