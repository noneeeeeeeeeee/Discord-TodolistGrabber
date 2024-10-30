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
        guild = ctx_or_interaction.guild
        author = ctx_or_interaction.author if isinstance(ctx_or_interaction, commands.Context) else ctx_or_interaction.user

        if guild.voice_client is None or guild.voice_client.channel != voice_channel:
            await voice_channel.connect()
        await self.add_song_to_queue(ctx_or_interaction, guild, author, link_or_url, config, link_or_url)

    async def after_play(self, ctx_or_interaction, error):
        guild = ctx_or_interaction.guild
        guild_id = guild.id

        if error:
            print(f"Player error: {error}")
            await self.send_message(ctx_or_interaction, f":x: An error occurred: {error}")

        if self.music_queue[guild_id]:
            next_url, _, next_title, next_duration = self.music_queue[guild_id].pop(0)
            await self.play_song(ctx_or_interaction, guild.voice_client, next_url, next_title, next_duration, json_get(guild_id), ctx_or_interaction.author)
        else:
            await self.send_message(ctx_or_interaction, "The queue is now empty.")

    async def add_song_to_queue(self, ctx_or_interaction, guild, author, link_or_url, config, ogurl):
        try:
            info = await self.extract_info(link_or_url)
            if info is None or 'formats' not in info:
                await self.send_message(ctx_or_interaction, ":x: Could not extract info from the provided link.")
                return

            # Get the audio URL
            url = next(
                (f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'),
                None
            )
            if not url:
                await self.send_message(ctx_or_interaction, ":x: No audio stream found for the provided link.")
                return

            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)
            guild_id = guild.id

            self.music_queue.setdefault(guild_id, [])

            self.music_queue[guild_id].append((url, ogurl, title, duration))

            if guild.voice_client.is_playing():
                await self.send_message(ctx_or_interaction, f"Added **{title}** to the queue.")
            else:
                await self.play_song(ctx_or_interaction, guild.voice_client, url, ogurl, title, duration, config, author)

        except youtube_dl.utils.DownloadError as e:
            await self.send_message(ctx_or_interaction, f"Download error: {e}")
            print(f"Download error: {e}")
        except Exception as e:
            print(f"Error in add_song_to_queue: {e}")
            await self.send_message(ctx_or_interaction, f":x: An error occurred: {e}")





    


    async def play_link(self, interaction, voice_channel, link, config):
        await self.play_song_or_link(interaction, voice_channel, link, config)

    async def play_song(self, ctx_or_interaction, voice_client, url, ogurl, title, duration, config, author):
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }

        voice_client.play(
            discord.FFmpegPCMAudio(url, **ffmpeg_options, executable=config.get("FFmpegPath", "ffmpeg")),
            after=lambda e: self.bot.loop.create_task(self.after_play(ctx_or_interaction, e))
        )

        embed = discord.Embed(title="Now Playing (Beta)", description=f"[{title}]({ogurl})", color=discord.Color.green())
        embed.set_author(name=f"Requested by {author.display_name}", icon_url=author.avatar.url)
        embed.set_footer(text=f"Bot Version: {read_current_version()}")
        await self.send_message(ctx_or_interaction, embed=embed)

    
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

    async def extract_info(self, url):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.extract_info_sync, url)

    def extract_info_sync(self, url):
        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            return ydl.extract_info(url, download=False)
    
    
    async def play_next_in_queue(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            self.now_playing.pop(guild_id, None)
            return

        # Get the next song from the queue, now with ogurl
        url, ogurl, title, duration = self.music_queue[guild_id].pop(0)
        self.now_playing[guild_id] = {
            "url": url,
            "ogurl": ogurl,
            "title": title,
            "duration": duration
        }
        print(self.now_playing)

        # Define the after_playing callback
        def after_playing(error):
            if error:
                print(f"Player error: {error}")
                asyncio.run_coroutine_threadsafe(ctx.send(":x: An error occurred while playing the song."), self.bot.loop)
            asyncio.run_coroutine_threadsafe(self.play_next_in_queue(ctx, voice_channel, config), self.bot.loop)

        # Play the audio stream with FFmpeg options
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }

        if ctx.voice_client is None:
            await voice_channel.connect()
        else:
            await ctx.voice_client.move_to(voice_channel)

        ctx.voice_client.play(
            discord.FFmpegPCMAudio(url, **ffmpeg_options, executable=config.get("FFmpegPath", "ffmpeg")),
            after=after_playing
        )

        embed = discord.Embed(
            title="Now Playing (Beta)",
            description=f"[{title}]({ogurl})",
            color=discord.Color.green()
        )
        embed.set_author(name=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.avatar.url)
        embed.set_footer(text=f"Bot Version: {read_current_version()}")
        await self.send_message(ctx, embed=embed)



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

        try:
            playlist_items = await self.fetch_playlist_items(youtube, playlist_id)
            if not playlist_items:
                await ctx.send("No videos found in the playlist.")
                return

            for item in playlist_items:
                await self.process_video_entry(ctx, ctx.guild.id, item, config)

            await ctx.send(f"Added {len(playlist_items)} songs from the playlist to the queue.")

            if not ctx.voice_client.is_playing():
                await self.play_next_in_queue(ctx, voice_channel, config)
        except Exception as e:
            await ctx.send(f"An error occurred while retrieving the playlist: {str(e)}")

        added_videos = 0
        skipped_videos = 0

        tasks = []

        for video in playlist_items:
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
        return await asyncio.to_thread(self._fetch_playlist_items_sync, youtube, playlist_id, max_results)
    
    def _fetch_playlist_items_sync(self, youtube, playlist_id, max_results):
        playlist_items = []
        request = youtube.playlistItems().list(
            part="snippet",
            playlistId=playlist_id,
            maxResults=max_results
        )
    
        while request is not None:
            response = request.execute()
            playlist_items.extend(response["items"])
            request = youtube.playlistItems().list_next(request, response)
    
    async def process_video_entry(self, guild_id, video):
        try:
            video_id = video["snippet"]["resourceId"]["videoId"]
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            info = await self.extract_info(video_url)
    
            if info is None or 'formats' not in info:
                print(f"Could not extract info for video: {video_url}")
                return
    
            audio_url = next(
                (f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'),
                None
            )
    
            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)
    
            if audio_url:
                self.music_queue.setdefault(guild_id, []).append((audio_url, video_url, title, duration))
            else:
                print(f"Skipped video: {title}, no audio URL found.")
    
        except Exception as e:
            print(f"Error adding video to queue: {e}")
    
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
        await self.ctx.send("Time is up! You didn't select a song.", timeout=10)
        await self.ctx.edit(view=self)

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
