import discord
from discord.ext import commands
from modules.setconfig import json_get


class Skip(commands.Cog):
    @commands.hybrid_command()
    async def skip(self, ctx, count: int = 1):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player or not player.current:
            return await ctx.send("Nothing playing!")

        skipped = min(count, len(self.queues.get(ctx.guild.id, [])) + 1)
        player.queue.clear()
        if ctx.guild.id in self.queues:
            del self.queues[ctx.guild.id][: count - 1]

        await player.skip()
        await ctx.send(f"Skipped {skipped} tracks")


async def setup(bot):
    await bot.add_cog(Skip(bot))
