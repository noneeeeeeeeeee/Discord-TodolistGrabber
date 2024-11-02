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
            await ctx.send(":fast_forward: Skipped the current song.")

            # Play the next song in the queue
            await music_player.play_next_song(ctx, ctx.author.voice.channel, json_get(ctx.guild.id))
            
            guild_id = ctx.guild.id
            if guild_id in music_player.now_playing:
                current_song_info = music_player.now_playing[guild_id]
                # await ctx.send(f"Now playing: **{current_song_info[1]}**")
            else:
                await ctx.send(":x: No more songs in the queue.")

async def setup(bot):
    await bot.add_cog(Skip(bot))
