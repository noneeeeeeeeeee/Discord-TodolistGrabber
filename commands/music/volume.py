import discord
from discord.ext import commands

class Volume(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx, volume: int):
        """Adjusts the volume (0-100)."""
        if 0 <= volume <= 100:
            voice_client = ctx.guild.voice_client
            if voice_client:
                voice_client.source.volume = volume / 100
                await ctx.send(f":speaker: Volume set to {volume}%. (Beta)")
            else:
                await ctx.send(":x: I'm not connected to a voice channel.")
        else:
            await ctx.send(":x: Invalid volume. Please enter a value between 0 and 100.")

async def setup(bot):
    await bot.add_cog(Volume(bot))