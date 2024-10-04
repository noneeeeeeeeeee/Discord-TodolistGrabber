import os
import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View
import yt_dlp as youtube_dl
import googleapiclient.discovery
from modules.setconfig import check_guild_config_available, json_get
from modules.enviromentfilegenerator import check_and_load_env_file
from modules.readversion import read_current_version
import asyncio
from typing import List
import urllib.parse
from .disconnect_state import DisconnectState




class MusicPlayer(commands.Cog):
    def __init__(self, bot, disconnect_state: DisconnectState):
        self.bot = bot
        self.disconnect_state = disconnect_state
        self.music_queue = {}
        self.now_playing = {}
        self.seek_flag = {}

        self.youtube_dl_options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
            }]
        }

        check_and_load_env_file()
        self.youtube_api_key = os.getenv("YOUTUBE_API_KEY")

        if not self.youtube_api_key:
            raise ValueError("YouTube API key is missing. Ensure it's set in the environment variables.")

        self.youtube_service = googleapiclient.discovery.build(
            "youtube", "v3", developerKey=self.youtube_api_key
        )

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

        if "https://www.youtube.com" in input and not "list=" in input:
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

            
    async def play_song_or_link(self, ctx_or_interaction, voice_channel, link_or_url, config):
        global intentional_disconnect

        if self.disconnect_state.is_intentional():
            return

        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            author = ctx_or_interaction.author
        else:
            guild = ctx_or_interaction.guild
            author = ctx_or_interaction.user

        if guild.voice_client is None:
            await voice_channel.connect()
        else:
            await guild.voice_client.move_to(voice_channel)

        try:
            # Extract the info from yt-dlp
            info = await asyncio.to_thread(self.extract_info_from_ytdlp, link_or_url)

            if info is None or 'formats' not in info:
                await self.send_message(ctx_or_interaction, "Failed to extract playable audio URL or no formats available.")
                return

            # Prioritize the highest quality audio format
            url = next((f['url'] for f in sorted(info['formats'], key=lambda x: (x.get('abr') or 0), reverse=True)
                         if f.get('acodec') and f['acodec'] != 'none'), None)

            if url is None:
                await self.send_message(ctx_or_interaction, "Could not find a playable audio format.")
                return

            title = info.get("title", "Unknown Song")
            duration = info.get("duration", 0)

            # Set now_playing for the current guild
            guild_id = guild.id

            # Ensure the music queue is initialized for this guild
            if guild_id not in self.music_queue:
                self.music_queue[guild_id] = []  # Initialize the queue if it doesn't exist

            # Check if a song is already playing
            if guild.voice_client.is_playing():
                # Add the song to the queue instead of playing it immediately
                self.music_queue[guild_id].append((url, title, duration))
                await self.send_message(ctx_or_interaction, f"Added **{title}** to the queue.")
            else:
                # If no song is playing, play this song
                self.now_playing[guild_id] = (url, title, duration)
                self.seek_flag[guild_id] = False  # Reset the seek flag

                def after_playing(error):
                    if error:
                        print(f"Player error: {error}")
                    if not self.seek_flag.get(guild_id, False):
                        asyncio.run_coroutine_threadsafe(self.play_next_in_queue(ctx_or_interaction, voice_channel, config), self.bot.loop)

                # Optimized FFmpeg options for streaming
                ffmpeg_options = {
                    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                    'options': '-vn'  # Ensure that no video is processed
                }

                # Play the audio stream with FFmpeg options
                guild.voice_client.play(
                    discord.FFmpegPCMAudio(url, **ffmpeg_options, executable=config.get("FFmpegPath", "ffmpeg")),
                    after=after_playing
                )

                embed = discord.Embed(
                    title="Now Playing",
                    description=f"[{title}]({url})",
                    color=discord.Color.green()
                )
                embed.set_author(name=f"Requested by {author.display_name}", icon_url=author.avatar.url)
                embed.set_footer(text=f"Bot Version: {read_current_version()}")
                await self.send_message(ctx_or_interaction, embed=embed)

        except youtube_dl.utils.DownloadError as e:
            await self.send_message(ctx_or_interaction, f"Could not download the song: {e}")
            print(f"Download error: {e}")
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"General error in play_song_or_link: {error_trace}")
            await self.send_message(ctx_or_interaction, f"An error occurred: {str(e)}\nFull Traceback: ```{error_trace}```")



    


    async def play_link(self, interaction, voice_channel, link, config):
        await self.play_song_or_link(interaction, voice_channel, link, config)

    async def play_song(self, ctx, voice_channel, url, config):
        await self.play_song_or_link(ctx, voice_channel, url, config)
    
    async def send_message(self, ctx_or_interaction, message=None, embed=None):
        """Helper function to send a message or an embed in both Context and Interaction."""
        if isinstance(ctx_or_interaction, commands.Context):
            if embed:
                await ctx_or_interaction.send(embed=embed)
            else:
                await ctx_or_interaction.send(message)
        else:
            if not ctx_or_interaction.response.is_done():
                if embed:
                    await ctx_or_interaction.response.send_message(embed=embed)
                else:
                    await ctx_or_interaction.response.send_message(message)
            else:
                if embed:
                    await ctx_or_interaction.followup.send(embed=embed)
                else:
                    await ctx_or_interaction.followup.send(message)

 


    def extract_info_from_ytdlp(self, url):
        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            info = ydl.extract_info(url, download=False)

            if 'formats' not in info:
                return None 
            return info

    
    



    async def play_next_in_queue(self, ctx, voice_channel, config):
        global intentional_disconnect

        if self.disconnect_state.is_intentional():
            return
    
        guild_id = ctx.guild.id

        # Check if the queue is empty
        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await self.send_message(ctx, "The queue is empty.")
            self.now_playing.pop(guild_id, None)
            return

        # Get the next song from the queue
        url, title, duration = self.music_queue[guild_id].pop(0)
        self.now_playing[guild_id] = (url, title, duration)

        def after_playing(error):
            if error:
                print(f"Player error: {error}")
                asyncio.run_coroutine_threadsafe(ctx.send(":x: An error occurred while playing the song."), self.bot.loop)
            # Move to the next song in the queue
            asyncio.run_coroutine_threadsafe(self.play_next_in_queue(ctx, voice_channel, config), self.bot.loop)

        try:
            # Connect to the voice channel if not already connected
            if ctx.voice_client is None:
                await voice_channel.connect()
            else:
                await ctx.voice_client.move_to(voice_channel)

            # Optimized FFmpeg options for streaming
            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                'options': '-vn'  # Exclude video, only process audio
            }

            # Play the audio stream with FFmpeg options
            ctx.voice_client.play(
                discord.FFmpegPCMAudio(url, **ffmpeg_options, executable=config.get("FFmpegPath", "ffmpeg")),
                after=after_playing
            )

            # Send now-playing embed message
            embed = discord.Embed(
                title="Now Playing",
                description=f"[{title}]({url})",
                color=discord.Color.green()
            )
            await self.send_message(ctx, embed=embed)

        except Exception as e:
            # Handle exceptions, particularly if audio is already playing
            if str(e) == "Already playing audio.":
                return
            await self.send_message(ctx, f"An error occurred while playing the song: {e}")
            print(f"Error in play_next_in_queue: {e}")

    
    








    async def handle_playlist(self, ctx, voice_channel, playlist_url, config):
        global intentional_disconnect

        if self.disconnect_state.is_intentional():
            return

        await ctx.reply(":hourglass: Adding playlist to the queue... Larger playlists may take longer to add. (Beta)")

        guild_id = ctx.guild.id

        if guild_id not in self.music_queue:
            self.music_queue[guild_id] = []

        # Parse the playlist URL to extract the playlist ID
        parsed_url = urllib.parse.urlparse(playlist_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        playlist_id = query_params.get("list", [None])[0]

        if not playlist_id:
            await ctx.send(":x: Invalid playlist URL. Could not extract the playlist ID.")
            return

        youtube = self.youtube_service

        # Fetch playlist items from YouTube
        try:
            playlist_videos = await self.fetch_playlist_items(youtube, playlist_id, max_results=config.get("PlaylistAddLimit", 50))
            total_videos = len(playlist_videos)
            print(f"Retrieved {total_videos} videos from playlist {playlist_id}")
        except Exception as e:
            await ctx.send(f"An error occurred while retrieving the playlist: {str(e)}")
            print(f"Error retrieving playlist: {e}")
            return

        added_videos = 0
        skipped_videos = 0

        tasks = []

        for video in playlist_videos:
            tasks.append(self.process_video_entry(ctx, guild_id, video, config))
            added_videos += 1

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                skipped_videos += 1

        if skipped_videos > 0:
            await ctx.send(f":white_check_mark: Added {added_videos} videos to the queue. Skipped {skipped_videos} videos due to errors.")
        else:
            await ctx.send(f":white_check_mark: Added {added_videos} videos to the queue.")

        if added_videos > 0:
            await self.play_next_in_queue(ctx, voice_channel, config)

    
    async def fetch_playlist_items(self, youtube, playlist_id, max_results=50):
        """Fetches videos from a YouTube playlist using the YouTube Data API v3."""
        videos = []
        next_page_token = None
    
        while len(videos) < max_results:
            request = self.youtube_service.playlistItems().list(
                part="snippet",
                maxResults=min(max_results - len(videos), 50), 
                playlistId=playlist_id,
                pageToken=next_page_token
            )
            response = request.execute()
    
            for item in response["items"]:
                video_id = item["snippet"]["resourceId"]["videoId"]
                title = item["snippet"]["title"]
                videos.append({"id": video_id, "title": title})
    
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
            
        return videos
    

    async def process_video_entry(self, ctx, guild_id, video, config):
        try:
            video_id = video["id"]
            title = video["title"]

            if config.get("MusicQueueLimitEnabled", False) and len(self.music_queue[guild_id]) >= config["MusicQueueLimit"]:
                await ctx.send(f"Queue limit reached! Only {config['MusicQueueLimit']} songs allowed.")
                return

            # Use yt-dlp to extract audio URL
            url = f"https://www.youtube.com/watch?v={video_id}"
            video_info = await asyncio.to_thread(self.extract_info_from_ytdlp, url)

            audio_url = next((f['url'] for f in video_info['formats'] if f.get('acodec') and f['acodec'] != 'none'), None)

            if audio_url:
                self.music_queue[guild_id].append((audio_url, title, video_info.get("duration", 0)))
            else:
                print(f"Skipped video: {title}, no audio URL found.")

        except Exception as e:
            print(f"Error adding video to queue: {e}")
            raise e



    def extract_playlist_info(self, link):
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

        # Access voice_client via interaction.guild.voice_client
        voice_channel = interaction.user.voice.channel
        if not interaction.guild.voice_client:
            await voice_channel.connect()

        # Play the selected song
        await self.music_player.play_link(interaction, voice_channel, self.song_info[0], json_get(interaction.guild_id))

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
