import discord
from discord.ext import commands


class ClearQueue(commands.Cog):
    @commands.hybrid_command(name="clearqueue", aliases=["cq"])
    async def clear_queue(self, ctx, *, items: str = None):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player or not player.queue:
            return await ctx.send("Queue is already empty!")

        if items:
            # Handle specific item removal
            indices = self.parse_indices(items)
            for idx in sorted(indices, reverse=True):
                if 0 <= idx < len(player.queue):
                    del player.queue[idx]
            await ctx.send(f"Removed {len(indices)} tracks")
        else:
            player.queue.clear()
            await ctx.send("Cleared entire queue")


async def setup(bot):
    await bot.add_cog(ClearQueue(bot))
