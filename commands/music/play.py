import os
import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View
import yt_dlp as youtube_dl
from typing import List
from modules.setconfig import check_guild_config_available, json_get
from modules.enviromentfilegenerator import check_and_load_env_file
import asyncio

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


        if "https://" in input:
            await self.play_link(ctx, voice_channel, input, config)
        elif input.lower().startswith("search"):
            query = input[len("search"):].strip()
            await self.search_youtube(ctx, query)
        elif input.lower().startswith("file"):
            await self.play_file(ctx, input[len("file "):].strip(), config)
        else:
            print(input)
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

                url = next((f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'), None)
                if url is None:
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
            await ctx.send(f"An error occurred while trying to play the song: {str(e)}")
            print(f"Error: {e}")

    async def play_next_in_queue(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await ctx.send("The queue is empty.")
            self.now_playing[guild_id] = None
            return

        url, title = self.music_queue[guild_id].pop(0)

        try:
            if ctx.guild.voice_client is None or not ctx.guild.voice_client.is_connected():
                vc = await voice_channel.connect()
            else:
                vc = ctx.guild.voice_client

            check_and_load_env_file()
            ffmpeg_path = os.getenv('FFMPEG_PATH')

            FFMPEG_OPTIONS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5','options': '-vn'}
            vc.play(discord.FFmpegPCMAudio(source=url, executable=ffmpeg_path, **FFMPEG_OPTIONS), after=lambda e: self.bot.loop.create_task(self.play_next_in_queue(ctx, voice_channel, config)))

            await ctx.send(f"Now playing: {title}")
            self.now_playing[guild_id] = title
        except Exception as e:
            await ctx.send(f"An error occurred while trying to play the next song: {str(e)}")
            print(f"Error: {e}")
            await self.handle_playback_error(ctx, voice_channel, config)

    async def handle_playback_error(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id
        await ctx.send("An error occurred during playback. Attempting to play the next song...")
        
        if ctx.guild.voice_client:
            await ctx.guild.voice_client.disconnect()
        
        await asyncio.sleep(2)
        
        try:
            await voice_channel.connect()
        except Exception as e:
            await ctx.send(f"Failed to reconnect to the voice channel: {str(e)}")
            return

        await self.play_next_in_queue(ctx, voice_channel, config)


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
            info = ydl.extract_info(f"{query}", download=False)['entries']

        search_results = [(entry["url"], entry["title"]) for entry in info]
        embed = discord.Embed(title=f"Top 10 search results for '{query}'", description="", color=discord.Color.blue())

        for idx, (url, title) in enumerate(search_results, start=1):
            embed.description += f"{idx}. [{title}]({url})\n"

        await ctx.send(embed=embed)

        # Add buttons for song selection
        view = SongSelectionView(ctx, search_results, self)
        await ctx.send("Select a song:", view=view)

    @commands.command(name="clear")
    async def stop(self, ctx):
        guild_id = ctx.guild.id
        if ctx.guild.voice_client:
            ctx.guild.voice_client.stop()
            await ctx.guild.voice_client.disconnect()
            self.music_queue[guild_id] = []
            self.now_playing[guild_id] = None
            await ctx.send("Stopped playback and cleared the queue. (Beta Command)")
        else:
            await ctx.send("The bot is not connected to a voice channel. (Beta Command)")

    @commands.command(name="skip")
    async def skip(self, ctx):
        guild_id = ctx.guild.id
        if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
            ctx.guild.voice_client.stop()
            await ctx.send("Skipped the current song. (Beta Command)")
            await self.play_next_in_queue(ctx, ctx.author.voice.channel, json_get(guild_id))
        else:
            await ctx.send("There's no song playing to skip. (Beta Command)")

    @commands.command(name="queue")
    async def queue(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await ctx.send("The queue is empty.")
        else:
            queue_list = "\n".join([f"{i+1}. {song[1]}" for i, song in enumerate(self.music_queue[guild_id])])
            await ctx.send(f"Current queue (Beta Command):\n{queue_list}")

    @commands.command(name="nowplaying")
    async def nowplaying(self, ctx):
        guild_id = ctx.guild.id
        if guild_id in self.now_playing and self.now_playing[guild_id]:
            await ctx.send(f"Now playing (Beta Command): {self.now_playing[guild_id]}")
        else:
            await ctx.send("No song is currently playing. (Beta Command)")
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
