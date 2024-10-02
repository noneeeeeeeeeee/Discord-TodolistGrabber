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


    async def play_song_or_link(self, ctx_or_interaction, voice_channel, link_or_url, config):
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
            info = await asyncio.to_thread(self.extract_info_from_ytdlp, link_or_url)

            if info is None:
                await self.send_message(ctx_or_interaction, "Failed to extract information.")
                return

            print(f"Extracted info: {info}") 

            url = None
            if 'formats' in info:
                # 1. Prioritize formats with both audio and video codecs
                url = next((f['url'] for f in info['formats'] 
                            if f.get('acodec') and f.get('acodec') != 'none' 
                            and f.get('vcodec') and f.get('vcodec') != 'none'), None)

                if url is None:
                    # 2. If no combined audio/video, try MP3
                    url = next((f['url'] for f in info['formats'] if f.get('acodec') == 'mp3'), None)

                if url is None:
                    # 3. Try other common audio formats
                    url = next((f['url'] for f in info['formats'] if f.get('ext') in ('m4a', 'webm', 'ogg', 'aac')), None)

                if url is None:
                    # 4. Fallback to any format with an audio codec
                    url = next((f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'), None)

                if url is None: 
                    # 5. Final fallback: Use the first available format
                    url = info['formats'][0]['url']

            # Check for common invalid URL patterns (refined)
            if url is None or 'videoplayback' in url: 
                await self.send_message(ctx_or_interaction, "Could not find a playable audio format or found an invalid format.")
                return


            # Check for common invalid URL patterns
            if url is None or 'videoplayback' in url:
                await self.send_message(ctx_or_interaction, "Could not find a playable audio format or found an invalid format.")
                return

            title = info.get("title", "Unknown Song")
            duration = info.get("duration", 0)

            # Add to the music queue
            guild_id = guild.id
            if guild_id not in self.music_queue:
                self.music_queue[guild_id] = []

            if config.get("MusicQueueLimitEnabled", False) and len(self.music_queue[guild_id]) >= config["MusicQueueLimit"]:
                if any(role.id == config["DefaultAdmin"] for role in author.roles):
                    await self.send_message(ctx_or_interaction, "Bypassing queue limit as admin.")
                else:
                    await self.send_message(ctx_or_interaction, f"Queue limit reached! Only {config['MusicQueueLimit']} songs allowed.")
                    return

            self.music_queue[guild_id].append((url, title, duration))
            await self.send_message(ctx_or_interaction, f"Added {title} to the queue.")

            if not guild.voice_client.is_playing():
                await self.play_next_in_queue(ctx_or_interaction, voice_channel, config)

        except youtube_dl.utils.DownloadError as e:
            await self.send_message(ctx_or_interaction, f"Could not download the song: {e}")
            print(f"Download error: {e}")
        except Exception as e:
            await self.send_message(ctx_or_interaction, f"An error occurred: {e}")
            print(f"General error in play_song_or_link: {e}") 
    
    





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
        else:  # For discord.Interaction
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
            return info


    async def play_next_in_queue(self, ctx, voice_channel, config):
        guild_id = ctx.guild.id

        # Check if there are songs in the queue
        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            await self.send_message(ctx, "The queue is empty.")
            self.now_playing.pop(guild_id, None)
            return

        # Get the next song from the queue
        url, title, duration = self.music_queue[guild_id].pop(0)

        try:
            # Connect to the voice channel if not already connected
            if ctx.voice_client is None:
                await voice_channel.connect()
            else:
                await ctx.voice_client.move_to(voice_channel)

            # Play the next song using play_song_or_link
            await self.play_song_or_link(ctx, voice_channel, url, config)

        except Exception as e:
            await self.send_message(ctx, f"An error occurred while playing the song: {e}")
            print(f"Error in play_next_in_queue: {e}")







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
        playlist_add_limit = config.get("PlaylistAddLimit", 10)

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
