import discord
from discord.ext import commands
import re

class Seek(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="seek")
    async def seek(self, ctx, time: str):
        """Seeks to a specific time in the current song. Format: HH:MM:SS or MM:SS"""
        voice_client = ctx.guild.voice_client
        music_player = self.bot.get_cog("MusicPlayer")

        if voice_client and voice_client.is_playing() and music_player and music_player.now_playing.get(ctx.guild.id):
            try:
                # Access the current song info directly from now_playing
                current_song_info = music_player.now_playing[ctx.guild.id]
                print(f"Current song info: {current_song_info}")  # Debugging line

                # Validate the structure (this check might be redundant now)
                if len(current_song_info) != 3:
                    await ctx.send(":x: Unable to get current song information. Invalid structure.")
                    return

                song_url, song_title, song_duration = current_song_info

                # Parse the time string
                if match := re.match(r"(?:(\d+):)?(\d+):(\d+)", time):
                    hours, minutes, seconds = match.groups()
                    hours = int(hours) if hours else 0
                    minutes = int(minutes)
                    seconds = int(seconds)
                    new_time = hours * 3600 + minutes * 60 + seconds

                    # Check if the new time is within the song's duration
                    if 0 <= new_time <= song_duration:
                        voice_client.source.pause()  # Pause while seeking
                        voice_client.source.seek(new_time)
                        voice_client.source.resume()
                        await ctx.send(f":fast_forward: Seeked to {time}.")
                    else:
                        await ctx.send(":x: Invalid seek time. It's outside the song's duration.")
                else:
                    await ctx.send(":x: Invalid time format. Use HH:MM:SS or MM:SS")

            except Exception as e:
                await ctx.send(":x: Unable to get current song information. Please try again.")
                print(f"Seek error: {str(e)}")
        else:
            await ctx.send(":x: Nothing is currently playing.")

async def setup(bot):
    await bot.add_cog(Seek(bot))