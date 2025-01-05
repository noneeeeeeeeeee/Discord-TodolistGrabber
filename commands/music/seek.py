import discord
from discord.ext import commands
import re


class Seek(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="seek")
    async def seek(self, ctx, time: str = None):
        if time is None:
            await ctx.send(
                ":x: Please provide a time to seek to. Use the format HH:MM:SS or MM:SS."
            )
            return

        voice_client = ctx.guild.voice_client
        music_player = self.bot.get_cog("MusicPlayer")

        if (
            voice_client
            and (voice_client.is_playing() or voice_client.is_paused())
            and music_player
            and music_player.now_playing.get(ctx.guild.id)
        ):
            try:
                current_song_info = music_player.now_playing[ctx.guild.id]
                if not all(
                    key in current_song_info for key in ("url", "title", "duration")
                ):
                    await ctx.send(
                        ":x: Unable to get current song information. Invalid structure."
                    )
                    return

                song_url = current_song_info["url"]
                song_title = current_song_info["title"]
                song_duration = current_song_info["duration"]

                match = re.match(r"(?:(\d+):)?(\d+):(\d+)", time)
                if match:
                    hours, minutes, seconds = match.groups()
                    hours = int(hours) if hours else 0
                    minutes = int(minutes)
                    seconds = int(seconds)
                    new_time = hours * 3600 + minutes * 60 + seconds

                    if 0 <= new_time <= song_duration:
                        voice_client.pause()

                        ffmpeg_options = {
                            "before_options": f"-ss {new_time}",
                            "options": "-vn",
                        }

                        source = discord.FFmpegPCMAudio(song_url, **ffmpeg_options)

                        def after_playing(error):
                            if error:
                                print(f"Player error after seek: {error}")

                        voice_client.play(source, after=after_playing)

                        await ctx.send(
                            f":fast_forward: Seeked to {time} in **{song_title}**."
                        )
                    else:
                        await ctx.send(
                            ":x: Invalid time format. Use HH:MM:SS or MM:SS."
                        )
                else:
                    await ctx.send(":x: Invalid time format. Use HH:MM:SS or MM:SS")

            except Exception as e:
                await ctx.send(
                    ":x: Unable to seek in the current song. Please try again."
                )
                print(f"Seek error: {str(e)}")
        else:
            await ctx.send(":x: Nothing is currently playing.")


async def setup(bot):
    await bot.add_cog(Seek(bot))
