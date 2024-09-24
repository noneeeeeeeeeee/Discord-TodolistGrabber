import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View
import yt_dlp as youtube_dl
from typing import List
from modules.setconfig import check_guild_config_available, json_get

class MusicPlayer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.music_queue = {}
        self.now_playing = {}
        self.youtube_dl_options = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True
        }
        youtube_dl.utils.bug_reports_message = lambda: ''

    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song or add to queue")
    @app_commands.describe(input="Input can be a YouTube link, file, or search query")
    async def play(self, ctx: commands.Context, input: str):
        guild_id = ctx.guild.id

        # Check if guild is configured
        if not check_guild_config_available(guild_id):
            await ctx.send("The server is not configured yet. Please run the !setup command.")
            return

        config = json_get(guild_id)

        # Check if music is enabled
        if not config.get("MusicEnabled", False):
            await ctx.send("Music is disabled on this server.")
            return

        # Check if DJ role is required
        if config.get("MusicDJRoleRequired", False) and not any(role.id == config["MusicDJRole"] or role.id == config["DefaultAdmin"] for role in ctx.author.roles):
            await ctx.send("You don't have the required role to play music.")
            return

        # Check if user is in a voice channel
        if not ctx.author.voice:
            await ctx.send("You need to be in a voice channel to play music.")
            return

        voice_channel = ctx.author.voice.channel

        # If a link is provided (YouTube, file, or playlist)
        if "https://" in input:
            await self.play_link(ctx, voice_channel, input, config)
        elif input.lower().startswith("search "):
            query = input[len("search "):].strip()
            await self.search_youtube(ctx, query)
        elif input.lower().startswith("file"):
            await self.play_file(ctx, config)
        else:
            await ctx.send("Invalid input. Please provide a YouTube link, search query, or file.")

    async def play_link(self, ctx, voice_channel, link, config):
        # If playlist, handle queue limit if enabled
        if "playlist" in link:
            await self.handle_playlist(ctx, voice_channel, link, config)
        else:
            await self.play_song(ctx, voice_channel, link, config)

    async def play_song(self, ctx, voice_channel, link, config):
        guild_id = ctx.guild.id
    
        if guild_id not in self.music_queue:
            self.music_queue[guild_id] = []
    
        # Check if there's a queue limit and it's reached
        if config.get("MusicQueueLimitEnabled", False) and len(self.music_queue[guild_id]) >= config["MusicQueueLimit"]:
            if any(role.id == config["DefaultAdmin"] for role in ctx.author.roles):
                await ctx.send("Bypassing queue limit as admin.")
            else:
                await ctx.send(f"Queue limit reached! Only {config['MusicQueueLimit']} songs allowed.")
                return
    
        try:
            with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
                info = ydl.extract_info(link, download=False)
                print(f"Available formats for {link}: {[f['format_id'] for f in info['formats']]}")
                
                url = next((f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'), None)
    
                if url is None:
                    # Fallback to get the first available audio format
                    url = next((f['url'] for f in info['formats'] if 'audio' in f['ext']), None)
    
                title = info.get("title", "Unknown Song")
    
            if url is None:
                await ctx.send("Could not find a playable audio format.")
                return
    
            self.music_queue[guild_id].append((url, title))
            await ctx.send(f"Added {title} to the queue.")
    
            if guild_id not in self.now_playing or self.now_playing[guild_id] is None:
                await self.play_next_in_queue(ctx, voice_channel, config)
        except Exception as e:
            await ctx.send("An error occurred while trying to play the song.")
            print(f"Error: {e}")
    

    async def play_next_in_queue(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await ctx.send("The queue is empty.")
            return

        url, title = self.music_queue[guild_id].pop(0)

        # Connect to the voice channel if not connected
        if ctx.guild.voice_client is None:
            vc = await voice_channel.connect()
        else:
            vc = ctx.guild.voice_client

        # Ensure that ffmpeg executable path is correct
        ffmpeg_path = "ffmpeg"  # Replace with your system path if necessary
        print(f"Playing URL: {url}")  # Log the URL

        # Configure FFmpeg with additional options
        vc.play(discord.FFmpegPCMAudio(
            executable=ffmpeg_path,
            source=url,
            options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel verbose'
        ), after=lambda e: self.after_play(ctx, e))

        await ctx.send(f"Now playing: {title}")
        self.now_playing[guild_id] = title


    def after_play(self, ctx, error):
        if error:
            print(f"Error during playback: {error}")
        # Automatically play the next song in the queue
        self.bot.loop.create_task(self.play_next_in_queue(ctx, ctx.guild.voice_client.channel, json_get(ctx.guild.id)))


    async def handle_playlist(self, ctx, voice_channel, link, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue:
            self.music_queue[guild_id] = []

        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            info = ydl.extract_info(link, download=False)

        for entry in info["entries"]:
            if config.get("MusicQueueLimitEnabled", False) and len(self.music_queue[guild_id]) >= config["MusicQueueLimit"]:
                await ctx.send(f"Queue limit reached! Only {config['MusicQueueLimit']} songs allowed.")
                break
            self.music_queue[guild_id].append((entry["url"], entry["title"]))

        await ctx.send(f"Added playlist with {len(info['entries'])} videos to the queue.")
        await self.play_next_in_queue(ctx, voice_channel, config)

    async def search_youtube(self, ctx, query):
        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)['entries']

        search_results = [(entry["url"], entry["title"]) for entry in info]
        embed = discord.Embed(title=f"Top 10 search results for '{query}'", description="", color=discord.Color.blue())

        for idx, (url, title) in enumerate(search_results, start=1):
            embed.description += f"{idx}. [{title}]({url})\n"

        await ctx.send(embed=embed)

        # Add buttons for song selection
        view = SongSelectionView(ctx, search_results, self)
        await ctx.send("Select a song:", view=view)

    async def play_next_in_queue(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await ctx.send("The queue is empty.")
            return

        url, title = self.music_queue[guild_id].pop(0)

        # Connect to the voice channel if not connected
        if ctx.guild.voice_client is None:
            vc = await voice_channel.connect()
        else:
            vc = ctx.guild.voice_client

        # Ensure that ffmpeg executable path is correct
        ffmpeg_path = "ffmpeg"  # Replace with your system path if necessary
        vc.play(discord.FFmpegPCMAudio(executable=ffmpeg_path, source=url))
        await ctx.send(f"Now playing: {title}")

        self.now_playing[guild_id] = title


class SongSelectionView(View):
    def __init__(self, ctx, search_results: List[tuple], music_player: MusicPlayer):
        super().__init__()
        self.ctx = ctx
        self.search_results = search_results
        self.music_player = music_player
        for i in range(1, 11):
            button = SongSelectionButton(i, self.search_results[i - 1], self.music_player)
            self.add_item(button)


class SongSelectionButton(Button):
    def __init__(self, index, song_info, music_player):
        super().__init__(label=str(index), style=discord.ButtonStyle.primary)
        self.song_info = song_info
        self.music_player = music_player

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"You selected {self.song_info[1]}. Playing it now!")
        await self.music_player.play_song(interaction, interaction.user.voice.channel, self.song_info[0], json_get(interaction.guild_id))


async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
