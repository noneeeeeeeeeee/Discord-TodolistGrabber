import discord
from discord.ext import commands
from modules.readversion import read_current_version


class NowPlaying(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def now_playing_embed(self, title, url, author, color, elapsed, duration):
        progress_bar_length = 15
        progress = int((elapsed / duration) * progress_bar_length)
        progress_bar = "▬" * progress + "🔘" + "▬" * (progress_bar_length - progress)

        embed = discord.Embed(
            title="Now Playing",
            description=f"[{title}]({url})",
            color=color,
        )
        embed.set_author(
            name=f"Requested by {author.display_name}", icon_url=author.avatar.url
        )
        embed.add_field(
            name="Duration",
            value=f"{self.format_time(elapsed)} {progress_bar} {self.format_time(duration)}",
            inline=False,
        )
        embed.set_footer(text=f"Bot Version: {read_current_version()}")
        return embed

    def format_time(self, seconds):
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02}:{minutes:02}:{seconds:02}"
        else:
            return f"{minutes:02}:{seconds:02}"

    @commands.hybrid_command(
        name="nowplaying", aliases=["np"], description="Show the currently playing song"
    )
    async def now_playing(self, ctx: commands.Context):
        guild_id = ctx.guild.id
        music_player = self.bot.get_cog("MusicPlayer")
        if music_player and guild_id in music_player.now_playing:
            current_song = music_player.now_playing[guild_id]
            title = current_song["title"]
            url = current_song["ogurl"]
            author = current_song["requester"]
            duration = current_song["duration"]

            elapsed = music_player.get_current_duration(guild_id)
            elapsed = min(elapsed, duration)

            embed = self.now_playing_embed(
                title, url, author, discord.Color.blue(), int(elapsed), duration
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(":x: No song is currently playing.")


async def setup(bot):
    await bot.add_cog(NowPlaying(bot))
