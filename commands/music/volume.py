import discord
from discord.ext import commands


class Volume(commands.Cog):
    @commands.hybrid_command()
    async def volume(self, ctx, volume: int):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player:
            return await ctx.send("Not connected!")

        if 0 <= volume <= 1000:
            await player.set_volume(volume)
            self.volume[ctx.guild.id] = volume
            await ctx.send(f"Volume set to {volume}/1000")
        else:
            await ctx.send("Volume must be between 0 and 1000")


async def setup(bot):
    await bot.add_cog(Volume(bot))
