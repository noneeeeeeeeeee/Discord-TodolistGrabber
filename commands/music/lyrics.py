import discord
from discord.ext import commands
from modules.readversion import read_current_version
import aiohttp


class Lyrics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
    # Broken Implementation
    @commands.command(name="ly", aliases=["lyrics"])
    async def display_lyrics(self, ctx):
        music_player = self.bot.get_cog("MusicPlayer")
        guild_id = ctx.guild.id

        if music_player and guild_id in music_player.now_playing:
            current_song = music_player.now_playing[guild_id]
            if current_song is not None:
                title = current_song["title"]
                author = current_song["requester"]
                lyrics = await self.fetch_lyrics(title)

                embed = discord.Embed(
                    title=f"Lyrics: {title}",
                    description=lyrics,
                    color=discord.Color.blue(),
                )
                embed.set_author(
                    name=f"Song Requested by {author.display_name}",
                    icon_url=author.avatar.url,
                )
                embed.set_footer(text=f"Bot Version: {read_current_version()}")
                await ctx.send(embed=embed)
            else:
                await ctx.send(":x: No song is currently playing.")
        else:
            await ctx.send(":x: No song is currently playing.")

    async def fetch_lyrics(self, title):
        api_url = f"https://api.lyrics.ovh/v1/{title}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("lyrics", "Lyrics not found.")
                else:
                    return "Lyrics not found."


async def setup(bot):
    await bot.add_cog(Lyrics(bot))
