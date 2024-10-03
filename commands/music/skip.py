import discord
from discord.ext import commands
from modules.setconfig import json_get

class Skip(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="skip", aliases=["next", "s"])
    async def skip(self, ctx):
        """Skips the currently playing song."""
        voice_client = ctx.guild.voice_client
        music_player = self.bot.get_cog("MusicPlayer")

        if voice_client and voice_client.is_playing():
            # Stop the current song
            voice_client.stop()
            
            await music_player.play_next_in_queue(ctx, ctx.author.voice.channel, json_get(ctx.guild.id))
            
            await ctx.send(":fast_forward: Skipped the current song.")
        else:
            await ctx.send(":x: Nothing is currently playing.")

async def setup(bot):
    await bot.add_cog(Skip(bot))
