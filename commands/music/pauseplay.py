import discord
from discord.ext import commands

class PausePlay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="pauseplay", aliases=["pause", "resume", "pp"])
    async def pause_play(self, ctx):
        """Toggles between pausing and resuming the current song."""
        voice_client = ctx.guild.voice_client
        if voice_client:
            if voice_client.is_paused():
                voice_client.resume()
                await ctx.send(":arrow_forward: Resumed the music.")
            elif voice_client.is_playing():
                voice_client.pause()
                await ctx.send(":pause_button: Paused the music.")
            else:
                await ctx.send("Nothing is currently playing.")
        else:
            await ctx.send(":x: I'm not connected to a voice channel.")

async def setup(bot):
    await bot.add_cog(PausePlay(bot))