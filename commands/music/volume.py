import discord
from discord.ext import commands

class Volume(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx, volume: int):
        """Adjusts the volume (0-200)."""
        if 0 <= volume <= 200:
            voice_client = ctx.guild.voice_client
            if voice_client and voice_client.source:
                if not isinstance(voice_client.source, discord.PCMVolumeTransformer):
                    voice_client.source = discord.PCMVolumeTransformer(voice_client.source)
                if volume == 0:
                    voice_client.pause()
                else:
                    if voice_client.is_paused():
                        voice_client.resume()
                    voice_client.source.volume = volume / 100
                if volume == 0:
                    await ctx.send(":mute: Volume set to 100%. (Beta)")
                else:
                    await ctx.send(f":speaker: Volume set to {volume}%. (Beta)")
            else:
                await ctx.send(":x: I'm not connected to a voice channel.")
        else:
            await ctx.send(":x: Invalid volume. Please enter a value between 0 and 200.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id and after.channel is None:
            voice_client = before.channel.guild.voice_client
            if voice_client:
                voice_client.source.volume = 1.0 

async def setup(bot):
    await bot.add_cog(Volume(bot))
