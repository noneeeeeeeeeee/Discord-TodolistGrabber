import os
import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View
import yt_dlp as youtube_dl
from googleapiclient.discovery import build
from modules.setconfig import check_guild_config_available, json_get
from modules.enviromentfilegenerator import check_and_load_env_file
import asyncio
from typing import List
from .disconnect import intentional_disconnect



class MusicPlayer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.music_queue = {}
        self.now_playing = {}

        # Define yt-dlp options
        self.youtube_dl_options = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True
        }

        # Load YouTube Data API key from environment file
        check_and_load_env_file()
        self.youtube_api_key = os.getenv("YOUTUBE_API_KEY")

        # Initialize YouTube Data API client
        self.youtube_service = build("youtube", "v3", developerKey=self.youtube_api_key)

    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song or add to queue")
    @app_commands.describe(input="Input can be a YouTube link, file, or search query")
    async def play(self, ctx: commands.Context, *, input: str):
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
            # Play from YouTube link
            await self.play_link(ctx, voice_channel, input, config)
        elif input.lower().startswith("search"):
            # Search for the top 10 results
            query = input[len("search"):].strip()
            await self.search_youtube(ctx, query, top_n=10)
        elif input:
            await self.search_youtube(ctx, input, top_n=1)
        else:
            print("Please provide a valid input. Example: !play <YouTube link or search query> or !play search <search query>")

    async def play_link(self, ctx, voice_channel, link, config):
        # Play the song using a YouTube link
        await self.play_song(ctx, voice_channel, link, config)

    async def play_song(self, ctx, voice_channel, link, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue:
            self.music_queue[guild_id] = []

        # Check if there's a queue limit and it's reached
        if config.get("MusicQueueLimitEnabled", False) and len(self.music_queue[guild_id]) >= config["MusicQueueLimit"]:
            if any(role.id == config["DefaultAdmin"] for role in ctx.author.roles):
                await self.send_message(ctx, "Bypassing queue limit as admin.")
            else:
                await self.send_message(ctx, f"Queue limit reached! Only {config['MusicQueueLimit']} songs allowed.")
                return

        try:
            info = await asyncio.to_thread(self.extract_info_from_ytdlp, link)

            url = next((f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'), None)
            if url is None:
                url = next((f['url'] for f in info['formats'] if 'audio' in f['ext']), None)

            title = info.get("title", "Unknown Song")

            if url is None:
                await self.send_message(ctx, "Could not find a playable audio format.")
                return

            self.music_queue[guild_id].append((url, title))
            await self.send_message(ctx, f"Added {title} to the queue.")

            if guild_id not in self.now_playing or self.now_playing[guild_id] is None:
                await self.play_next_in_queue(ctx, voice_channel, config)
        except Exception as e:
            await self.send_message(ctx, f"An error occurred while trying to play the song: {str(e)}")
            print(f"Error: {e}")


    def extract_info_from_ytdlp(self, link):
        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            return ydl.extract_info(link, download=False)



    async def send_message(self, ctx, message):
        # Check if ctx is an interaction and respond accordingly
        if isinstance(ctx, discord.Interaction):
            if not ctx.response.is_done():
                await ctx.response.send_message(message, ephemeral=True)
            else:
                await ctx.followup.send(message)  # Use followup if response is already done
        else:
            await ctx.send(message)





    async def play_next_in_queue(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id

        global intentional_disconnect
        if intentional_disconnect:
            return

        # Check if the music queue is empty
        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await self.send_message(ctx, "The queue is empty.")
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

            FFMPEG_OPTIONS = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                'options': '-vn'
            }

            vc.play(discord.FFmpegPCMAudio(source=url, executable=ffmpeg_path, **FFMPEG_OPTIONS),
                    after=lambda e: self.bot.loop.create_task(self.play_next_in_queue(ctx, voice_channel, config)))

            await self.send_message(ctx, f"Now playing: {title}")
            self.now_playing[guild_id] = title
        except Exception as e:
            await self.send_message(ctx, f"An error occurred while trying to play the next song: {str(e)}")
            print(f"Error: {e}")
            await self.handle_playback_error(ctx, voice_channel, config)




    async def handle_playback_error(self, ctx, voice_channel, config):
        global intentional_disconnect 

        # Check if the disconnect was intentional
        if intentional_disconnect:
            intentional_disconnect = False
            return  

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

    async def search_youtube(self, ctx, query, top_n=1):
        try:
            request = self.youtube_service.search().list(
                q=query,
                part="snippet",
                maxResults=top_n,
                type="video",
                videoCategoryId="10",
                videoEmbeddable="true"
            )
            response = request.execute()

            search_results = []
            for item in response["items"]:
                video_id = item["id"]["videoId"]
                video_title = item["snippet"]["title"]
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                search_results.append((video_url, video_title))

            if top_n == 1:
                if search_results:
                    await self.play_link(ctx, ctx.author.voice.channel, search_results[0][0], json_get(ctx.guild.id))
                else:
                    await ctx.send("No results found.")
            else:
                embed = discord.Embed(title=f"Top {top_n} search results for '{query}'", description="", color=discord.Color.blue())

                for idx, (url, title) in enumerate(search_results, start=1):
                    embed.description += f"{idx}. [{title}]({url})\n"

                await ctx.send(embed=embed)

                # Add buttons for song selection
                view = SongSelectionView(ctx, search_results, self)
                await ctx.send("Select a song:", view=view)

        except Exception as e:
            await ctx.send(f"An error occurred while searching YouTube: {str(e)}")
            print(f"Error: {e}")

class SongSelectionView(View):
    def __init__(self, ctx, search_results: List[tuple], music_player: MusicPlayer):
        super().__init__(timeout=30)  # Timeout for the entire view
        self.ctx = ctx
        self.search_results = search_results
        self.music_player = music_player

        for i in range(1, len(self.search_results) + 1):
            button = SongSelectionButton(i, self.search_results[i - 1], self.music_player, ctx.author.id)  # Pass the original user ID
            self.add_item(button)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True  # Disable all buttons
        await self.ctx.send("Time is up! You didn't select a song.", ephemeral=True)  # Notify the original user
        await self.stop()  # Stop the view

class SongSelectionButton(Button):
    def __init__(self, index, song_info, music_player, original_user_id):
        super().__init__(label=str(index), style=discord.ButtonStyle.primary)
        self.song_info = song_info
        self.music_player = music_player
        self.original_user_id = original_user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.original_user_id:
            await interaction.response.send_message("This isn't your message! You cannot select a song.", ephemeral=True)
            return

        await interaction.response.send_message(f"You selected {self.song_info[1]}. Playing it now!")

        for button in self.view.children:
            button.disabled = True
        self.view.stop()

        await self.music_player.play_link(interaction, interaction.user.voice.channel, self.song_info[0], json_get(interaction.guild_id))

        await interaction.message.edit(view=self.view)

        asyncio.create_task(self.disable_buttons_after_timeout(interaction.message))

    async def disable_buttons_after_timeout(self, message):
        await asyncio.sleep(30)
        for button in self.view.children:
            button.disabled = True
        await message.edit(view=self.view)

async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
