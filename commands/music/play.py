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
from .disconnect_state import DisconnectState




class MusicPlayer(commands.Cog):
    def __init__(self, bot, disconnect_state: DisconnectState):
        self.bot = bot
        self.disconnect_state = disconnect_state  
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
            await ctx.send(":x: The server is not configured yet. Please run the !setup command.")
            return

        config = json_get(guild_id)

        # Check if music is enabled
        if not config.get("MusicEnabled", False):
            await ctx.send(":x: Music is disabled on this server.")
            return

        # Check if DJ role is required
        if config.get("MusicDJRoleRequired", False) and not any(role.id == config["MusicDJRole"] or role.id == config["DefaultAdmin"] for role in ctx.author.roles):
            await ctx.send(":x: You don't have the required role to play music.")
            return

        # Check if user is in a voice channel
        if not ctx.author.voice:
            await ctx.send(":x: You need to be in a voice channel to play music.")
            return

        voice_channel = ctx.author.voice.channel

        if "https://" in input and not "list=" in input:
            await self.play_link(ctx, voice_channel, input, config)
        elif "list=" in input:
            await self.handle_playlist(ctx, voice_channel, input, config)
        elif input.lower().startswith("search"):
            query = input[len("search"):].strip()
            await self.search_youtube(ctx, query, top_n=10)
        elif input:
            await self.search_youtube(ctx, input, top_n=1)
        else:
            print(":x: Please provide a valid input. Example: !play <YouTube link, search query, or playlist> or !play search <search query>")

    async def play_link(self, ctx, voice_channel, link, config):
        await self.play_song(ctx, voice_channel, link, config)

    async def play_song(self, ctx, voice_channel, url, config):
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
            # Extract song info using yt-dlp in a separate thread
            info = await asyncio.to_thread(self.extract_info_from_ytdlp, url)

            # Get URL, title, and duration
            url = next((f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'), None)
            if url is None:
                url = next((f['url'] for f in info['formats'] if 'audio' in f['ext']), None)
            

            title = info.get("title", "Unknown Song")
            duration = info.get("duration", 0)

            if url is None:
                await self.send_message(ctx, "Could not find a playable audio format.")
                return

            # Add to the music queue
            self.music_queue[guild_id].append((url, title, duration)) 
            await self.send_message(ctx, f"Added {title} to the queue.")

            # Set now_playing for the current song (store the URL, title, and duration)
            self.now_playing[guild_id] = (url, title, duration)
            print(f"Now playing set: {self.now_playing[guild_id]}")  # Debugging line

            # Attempt to play the next song
            await self.play_next_in_queue(ctx, voice_channel, config)
        except Exception as e:
            await self.send_message(ctx, f"An error occurred while trying to play the song: {str(e)}")
            print(f"Error in play_song: {e}")








    def extract_info_from_ytdlp(self, link):
        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            return ydl.extract_info(link, download=False)



    async def send_message(self, ctx, message):
        if isinstance(ctx, discord.Interaction):
            if not ctx.response.is_done():
                await ctx.response.send_message(message, ephemeral=True)
            else:
                await ctx.followup.send(message) 
        else:
            await ctx.send(message)


    async def play_next_in_queue(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await self.send_message(ctx, "The queue is empty.")
            self.now_playing.pop(guild_id, None)  # Remove the current song from now_playing
            return

        try:
            url, title, duration = self.music_queue[guild_id].pop(0)  # Get the next song
        except ValueError as e:
            print(f"Error unpacking song info: {e}")
            await self.send_message(ctx, ":x: An error occurred while trying to get the next song.")
            return


        try:
            # Connect to the voice channel if not already connected
            if ctx.voice_client is None:
                await voice_channel.connect()
            else:
                await ctx.voice_client.move_to(voice_channel)

            # Use yt-dlp to extract audio from the URL
            with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
                info = ydl.extract_info(url, download=False)
                source = discord.FFmpegPCMAudio(info['url'])

            ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(self.play_next_in_queue(ctx, voice_channel, config), self.bot.loop))

            embed = discord.Embed(title="Now Playing", description=f"[{title}]({url})", color=discord.Color.green())
            await self.send_message(ctx, embed=embed)

            self.now_playing[guild_id] = (url, title, duration)  # Set now_playing

        except Exception as e:
            await self.send_message(ctx, f"An error occurred while playing the song: {e}")









    async def handle_playback_error(self, ctx, voice_channel, config):
        global intentional_disconnect 

        if self.disconnect_state.is_intentional():
            self.disconnect_state.clear()
            return

        await ctx.send(":x: An error occurred during playback. Attempting to play the next song...")

        if ctx.guild.voice_client:
            await ctx.guild.voice_client.disconnect()

        await asyncio.sleep(2)

        try:
            await voice_channel.connect()
        except Exception as e:
            await ctx.send(f":x: Failed to reconnect to the voice channel: {str(e)}")
            return

        await self.play_next_in_queue(ctx, voice_channel, config)


    async def handle_playlist(self, ctx, voice_channel, link, config):
        global intentional_disconnect

        if self.disconnect_state.is_intentional():
            return
        await ctx.reply(":hourglass: Adding playlist to the queue... Larger playlists may take longer to add. (Beta) \n Adding a playlist will hopefully be optimized in the next patch.")
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue:
            self.music_queue[guild_id] = []

        try:
            playlist_info = await asyncio.to_thread(self.extract_playlist_info, link)
            print(f"Playlist info retrieved: {playlist_info['title']} with {len(playlist_info['entries'])} entries.")
        except Exception as e:
            await ctx.send(f"An error occurred while retrieving the playlist: {str(e)}")
            print(f"Error retrieving playlist: {e}")
            return

        added_videos = 0
        skipped_videos = 0
        playlist_add_limit = config.get("PlaylistAddLimit", 10)  # Default limit of 10 if not set

        for entry in playlist_info["entries"]:
            if added_videos >= playlist_add_limit:
                await ctx.reply(f"Playlist add limit reached. Only {playlist_add_limit} videos added.")
                break

            try:
                # Skip private/unavailable videos
                if entry is None or entry.get("title") in ["[Private video]", "[Deleted video]"]:
                    skipped_videos += 1
                    continue

                if config.get("MusicQueueLimitEnabled", False) and len(self.music_queue[guild_id]) >= config["MusicQueueLimit"]:
                    await ctx.send(f"Queue limit reached! Only {config['MusicQueueLimit']} songs allowed.")
                    break

                url = entry.get("url")
                title = entry.get("title", "Unknown Title")

                if url:
                    self.music_queue[guild_id].append((url, title))
                    added_videos += 1
                    print(f"Added video to queue: {title} ({url})")
                else:
                    skipped_videos += 1
                    print(f"Skipped video with no URL: {entry}")

            except Exception as e:
                # Log the error but continue to the next entry
                print(f"Error adding video to queue: {e}")
                skipped_videos += 1
                continue
        if skipped_videos > 0:
            await ctx.send(f":white_check_mark: Added {added_videos} videos to the queue. Skipped {skipped_videos} videos due to errors.")
        else:
            await ctx.send(f":white_check_mark: Added {added_videos} videos to the queue.")

        if added_videos > 0:
            await self.play_next_in_queue(ctx, voice_channel, config)


    # Helper function to extract playlist info in a separate thread
    def extract_playlist_info(self, link):
        """Extract the playlist information using yt-dlp."""
        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            print(f"Extracting playlist info for link: {link}")
            return ydl.extract_info(link, download=False)


    async def search_youtube(self, ctx, query, top_n=1):
        try:
            response = await asyncio.to_thread(self.youtube_search_handler, query, top_n)

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

                view = SongSelectionView(ctx, search_results, self)
                await ctx.send("Select a song:", view=view)

        except Exception as e:
            await ctx.send(f"An error occurred while searching YouTube: {str(e)}")
            print(f"Error: {e}")


    def youtube_search_handler(self, query, top_n):
        request = self.youtube_service.search().list(
            q=query,
            part="snippet",
            maxResults=top_n,
            type="video",
            videoCategoryId="10",
            videoEmbeddable="true"
        )
        return request.execute()

class SongSelectionView(View):
    def __init__(self, ctx, search_results: List[tuple], music_player: MusicPlayer):
        super().__init__(timeout=30) 
        self.ctx = ctx
        self.search_results = search_results
        self.music_player = music_player

        for i in range(1, len(self.search_results) + 1):
            button = SongSelectionButton(i, self.search_results[i - 1], self.music_player, ctx.author.id) 
            self.add_item(button)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True 
        await self.ctx.send("Time is up! You didn't select a song.", ephemeral=True)  
        await self.stop() 

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
    disconnect_state = DisconnectState()
    await bot.add_cog(MusicPlayer(bot, disconnect_state))
