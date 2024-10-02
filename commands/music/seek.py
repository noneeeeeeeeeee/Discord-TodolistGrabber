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

        # Check if something is playing and song info exists
        if voice_client and voice_client.is_playing() and music_player and music_player.now_playing.get(ctx.guild.id):
            try:
                # Get the current song's info
                current_song_info = music_player.now_playing[ctx.guild.id]
                if len(current_song_info) != 3:
                    await ctx.send(":x: Unable to get current song information. Invalid structure.")
                    return

                song_url, song_title, song_duration = current_song_info

                # Parse the time string for HH:MM:SS or MM:SS formats
                if match := re.match(r"(?:(\d+):)?(\d+):(\d+)", time):
                    hours, minutes, seconds = match.groups()
                    hours = int(hours) if hours else 0
                    minutes = int(minutes)
                    seconds = int(seconds)
                    new_time = hours * 3600 + minutes * 60 + seconds

                    # Check if the seek time is valid within the song's duration
                    if 0 <= new_time <= song_duration:
                        # Pause the current song to prevent skipping
                        voice_client.pause()

                        # Use FFmpeg to seek to the new time
                        ffmpeg_options = {
                            'before_options': f'-ss {new_time} -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                            'options': '-vn'
                        }

                        # Restart the current song from the new seek time
                        source = discord.FFmpegPCMAudio(song_url, **ffmpeg_options)

                        # Clear the after callback to prevent skipping to the next song in the queue
                        voice_client.play(source, after=None)

                        await ctx.send(f":fast_forward: Seeked to {time} in **{song_title}**.")
                    else:
                        await ctx.send(":x: Invalid seek time. It's outside the song's duration.")
                else:
                    await ctx.send(":x: Invalid time format. Use HH:MM:SS or MM:SS")

            except Exception as e:
                await ctx.send(":x: Unable to seek in the current song. Please try again.")
                print(f"Seek error: {str(e)}")
        else:
            await ctx.send(":x: Nothing is currently playing.")

async def setup(bot):
    await bot.add_cog(Seek(bot))
