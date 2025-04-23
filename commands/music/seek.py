import discord
from discord.ext import commands
import re


class Seek(commands.Cog):
    @commands.hybrid_command()
    async def seek(self, ctx, time: str):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player or not player.current:
            return await ctx.send("Nothing playing!")

        try:
            time_ms = self.parse_time(time) * 1000
            if time_ms < 0 or time_ms > player.current.duration:
                return await ctx.send("Invalid time!")

            await player.seek(time_ms)
            await ctx.send(f"Seeked to {time}")
        except:
            await ctx.send("Invalid time format! Use HH:MM:SS or MM:SS")

    def parse_time(self, time_str):
        parts = list(map(int, time_str.split(":")))
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        elif len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return 0


async def setup(bot):
    await bot.add_cog(Seek(bot))
