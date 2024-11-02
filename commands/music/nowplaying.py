import discord
from discord.ext import commands
from modules.readversion import read_current_version

class NowPlaying(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="nowplaying", aliases=["np"])
    async def now_playing(self, ctx):
        guild_id = ctx.guild.id
        
        version = read_current_version()
        music_player = self.bot.get_cog("MusicPlayer")
        if music_player:
            current_song = music_player.now_playing.get(guild_id)
            if current_song:
                title = current_song['title']
                duration = current_song['duration']
                url = current_song['ogurl']
                author = current_song['requester']
            
                formatted_duration = f"{duration // 60}:{duration % 60:02d}"
                
                embed = discord.Embed(
                    title="Now Playing (Beta)",
                    description=f"[{title}]({url})",
                    color=discord.Color.blue()
                )
                embed.set_author(name=f"Requested by {author.display_name}", icon_url=author.avatar.url)
                embed.set_footer(text=f"Bot Version: {read_current_version()}")
                await ctx.send(embed=embed)
            else:
                await ctx.send(":x: Nothing is currently playing.")
        else:
            await ctx.send(":x: Music player is not loaded.")

async def setup(bot):
    await bot.add_cog(NowPlaying(bot))
