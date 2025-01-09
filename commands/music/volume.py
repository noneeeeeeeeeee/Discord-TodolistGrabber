import discord
from discord.ext import commands


class Volume(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.current_volume = 1.0

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx, volume: int = None):
        if volume is None:
            await ctx.send(
                f":speaker: Current volume is {int(self.current_volume * 100)}%."
            )
            return

        if 0 <= volume <= 200:
            voice_client = ctx.guild.voice_client
            if voice_client and voice_client.source:
                if not isinstance(voice_client.source, discord.PCMVolumeTransformer):
                    voice_client.source = discord.PCMVolumeTransformer(
                        voice_client.source
                    )
                if volume == 0:
                    voice_client.pause()
                else:
                    if voice_client.is_paused():
                        voice_client.resume()
                    voice_client.source.volume = volume / 100
                    self.current_volume = volume / 100  # Store the current volume level
                if volume == 0:
                    await ctx.send(":mute: Volume set to 0%. Music now paused.")
                else:
                    await ctx.send(f":speaker: Volume set to {volume}%.")
            else:
                await ctx.send(":x: I'm not connected to a voice channel.")
        else:
            await ctx.send(
                ":x: Invalid volume. Please enter a value between 0 and 200."
            )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id and after.channel is None:
            voice_client = before.channel.guild.voice_client
            if voice_client and voice_client.source:
                voice_client.source.volume = 1.0

    async def apply_volume(self, voice_client):
        """Applies the stored volume level to the voice client."""
        if voice_client and voice_client.source:
            if not isinstance(voice_client.source, discord.PCMVolumeTransformer):
                voice_client.source = discord.PCMVolumeTransformer(voice_client.source)
            voice_client.source.volume = self.current_volume

    @commands.Cog.listener()
    async def on_voice_client_update(self, voice_client):
        """Listener to apply volume when the voice client updates."""
        await self.apply_volume(voice_client)


async def setup(bot):
    await bot.add_cog(Volume(bot))
