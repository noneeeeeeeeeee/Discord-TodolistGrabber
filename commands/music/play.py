import asyncio
from datetime import datetime
import discord
from discord.ext import commands
from discord.ui import View, Button
from modules.music.youtubefetch import YouTubeFetcher
from modules.setconfig import json_get, check_guild_config_available
from modules.music.linksidentifier import LinksIdentifier
import yt_dlp as youtube_dl
import urllib.parse


class MusicPlayer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.youtube_fetcher = YouTubeFetcher()
        self.music_queue = {}
        self.now_playing = {}

    @commands.hybrid_command(
        name="play", aliases=["p"], description="Play a song or add to queue"
    )
    @discord.app_commands.describe(
        input="Input can be a YouTube link, file, or search query"
    )
    async def play(self, ctx: commands.Context, *, input: str = None):
        try:
            guild_id = ctx.guild.id
            # Check if guild is configured
            if not check_guild_config_available(guild_id):
                await ctx.send(
                    ":x: The server is not configured yet. Please run the !setup command."
                )
                return
            config = json_get(guild_id)
            # Check if music is enabled
            if not config.get("MusicEnabled", False):
                await ctx.send(":x: Music is disabled on this server.")
                return
            # Check if DJ role is required
            if config.get("MusicDJRoleRequired", False) and not any(
                role.id == config["MusicDJRole"] or role.id == config["DefaultAdmin"]
                for role in ctx.author.roles
            ):
                await ctx.send(":x: You don't have the required role to play music.")
                return
            # Check if user is in a voice channel
            if not ctx.author.voice:
                await ctx.send(":x: You need to be in a voice channel to play music.")
                return

            linkType = LinksIdentifier.identify_link(input)
            print(
                f"Link Type (Soon, the music bot should support more link types): {linkType}"
            )
            voice_channel = ctx.author.voice.channel
            if "https://www.youtube.com" in input and not "list=" in input:
                await self.add_to_queue(ctx, voice_channel, input, config)
            elif "list=" in input:
                await self.handle_playlist(ctx, voice_channel, input, config)
            elif input.lower().startswith("search"):
                query = input[len("search") :].strip()
                await self.search_youtube(ctx, query, top_n=10)
            elif input:
                await self.search_youtube(ctx, input, top_n=1)
            else:
                await ctx.send(
                    ":x: Please provide a valid input. Example: !play <YouTube link, search query, or playlist> or !play search <search query> \n More sources coming soon!"
                )
        except commands.errors.MissingRequiredArgument as e:
            await ctx.send(f":x: Missing required argument: {e.param.name}")
        except Exception as e:
            await ctx.send(f":x: An unexpected error occurred: {str(e)}")
            print(f"Error in play command: {e}")

    async def add_to_queue(
        self, ctx_or_interaction, voice_channel, link_or_url, config
    ):
        guild = ctx_or_interaction.guild
        author = (
            ctx_or_interaction.author
            if isinstance(ctx_or_interaction, commands.Context)
            else ctx_or_interaction.user
        )

        if guild.voice_client is None or guild.voice_client.channel != voice_channel:
            await voice_channel.connect()

        try:
            info = await self.youtube_fetcher.extract_info(link_or_url)
            if info is None or "formats" not in info:
                await self.send_message(
                    ctx_or_interaction,
                    ":x: Could not extract info from the provided link.",
                )
                return
            # Get the audio URL
            url = next(
                (
                    f["url"]
                    for f in info["formats"]
                    if f.get("acodec") and f["acodec"] != "none"
                ),
                None,
            )
            if not url:
                await self.send_message(
                    ctx_or_interaction,
                    ":x: No audio stream found for the provided link.",
                )
                return
            title = info.get("title", "Unknown Title")
            duration = info.get("duration", 600)
            guild_id = guild.id
            self.music_queue.setdefault(guild_id, [])
            self.music_queue[guild_id].append(
                (author, url, link_or_url, title, duration)
            )

            if not guild.voice_client.is_playing():
                await self.play_now(ctx_or_interaction, guild.voice_client, config)
            else:
                await self.send_message(
                    ctx_or_interaction, f"Added **{title}** to the queue."
                )
        except youtube_dl.utils.DownloadError as e:
            await self.send_message(ctx_or_interaction, f"Download error: {e}")
            print(f"Download error: {e}")
        except Exception as e:
            print(f"Error in add_to_queue: {e}")
            await self.send_message(ctx_or_interaction, f":x: An error occurred: {e}")

    async def play_now(self, ctx, voice_client, config):
        guild_id = ctx.guild.id

        if guild_id not in self.music_queue or not self.music_queue[guild_id]:
            self.now_playing.pop(guild_id, None)
            await ctx.send(":x: No more songs in the queue.")
            return

        next_song = self.music_queue[guild_id].pop(0)
        author, url, ogurl, title, duration = next_song
        self.now_playing[guild_id] = {
            "requester": author,
            "url": url,
            "ogurl": ogurl,
            "title": title,
            "duration": duration,
            "start_time": datetime.now(),
        }

        def after_playing(error):
            if error:
                print(f"Player error: {error}")
                asyncio.run_coroutine_threadsafe(
                    ctx.send(":x: An error occurred while playing the song."),
                    self.bot.loop,
                )
            asyncio.run_coroutine_threadsafe(
                self.play_now(ctx, voice_client, config),
                self.bot.loop,
            )

        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": "-vn ",
        }
        voice_client.play(
            discord.FFmpegPCMAudio(url, **ffmpeg_options),
            after=after_playing,
        )

        # Apply the stored volume level
        volume_cog = self.bot.get_cog("Volume")
        if volume_cog:
            await volume_cog.apply_volume(voice_client)

        now_playing_cog = self.bot.get_cog("NowPlaying")
        if now_playing_cog:
            elapsed = (
                datetime.now() - self.now_playing[guild_id]["start_time"]
            ).seconds
            embed = now_playing_cog.now_playing_embed(
                title, ogurl, author, discord.Color.green(), elapsed, duration
            )
            await self.send_message(ctx, embed=embed)

    def get_current_duration(self, guild_id):
        if guild_id in self.now_playing:
            start_time = self.now_playing[guild_id]["start_time"]
            return (datetime.now() - start_time).seconds
        return 0

    async def handle_playlist(self, ctx, voice_channel, playlist_url, config):
        progress_message = await ctx.reply(":hourglass: Processing playlist...")
        author = ctx.author

        try:
            parsed_url = urllib.parse.urlparse(playlist_url)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            playlist_id = query_params.get("list", [None])[0]

            if not playlist_id:
                await progress_message.edit(content=":x: Invalid playlist URL.")
                return

            playlist_items = await self.youtube_fetcher.fetch_playlist_items(
                playlist_id
            )
            if not playlist_items:
                await progress_message.edit(
                    content=":x: No videos found in the playlist."
                )
                return

            total_videos = len(playlist_items)
            failed_videos = []
            processed_count = 0
            last_update = 0

            for item in playlist_items:
                result = await self.youtube_fetcher.process_video_entry(
                    item,
                    self.music_queue,
                    ctx.guild.id,
                    config.get("TrackMaxDuration", 360),
                    author,
                )

                processed_count += 1
                if result:
                    failed_videos.append(result)

                if (
                    processed_count - last_update >= 5
                    or processed_count == total_videos
                ):
                    await progress_message.edit(
                        content=f":hourglass: Adding playlist to queue: {processed_count}/{total_videos} videos processed..."
                    )
                    last_update = processed_count

            if failed_videos:
                error_msg = "\n- ".join(failed_videos)
                await progress_message.edit(
                    content=f":white_check_mark: Playlist processing complete!\nAdded {total_videos - len(failed_videos)}/{total_videos} songs to queue\n"
                    f":x: {len(failed_videos)} failed:\n- {error_msg}"
                )
            else:
                await progress_message.edit(
                    content=f":white_check_mark: Successfully added all {total_videos} songs to queue!"
                )

            # Connect to the voice channel if not already connected
            if not ctx.voice_client:
                await voice_channel.connect()

            # Play the music if not already playing
            if not ctx.voice_client.is_playing():
                await self.play_now(ctx, ctx.voice_client, config)

        except Exception as e:
            await progress_message.edit(content=f":x: Playlist error: {str(e)}")

    async def search_youtube(self, ctx, query, top_n=1):
        try:
            search_results = await self.youtube_fetcher.search_youtube(query, top_n)

            if top_n == 1:
                if search_results:
                    await self.add_to_queue(
                        ctx,
                        ctx.author.voice.channel,
                        search_results[0][0],
                        json_get(ctx.guild.id),
                    )
                else:
                    await ctx.send("No results found.")
            else:
                embed = discord.Embed(
                    title=f"Top {top_n} search results for '{query}'",
                    description="",
                    color=discord.Color.blue(),
                )

                for idx, (url, title) in enumerate(search_results, start=1):
                    embed.description += f"{idx}. [{title}]({url})\n"

                await ctx.send(embed=embed)

                view = SongSelectionView(ctx, search_results, self)
                await ctx.send("Select a song:", view=view)

        except Exception as e:
            await ctx.send(f"An error occurred while searching YouTube: {str(e)}")
            print(f"Error: {e}")

    async def send_message(self, ctx_or_interaction, message=None, embed=None):
        """Helper function to send a message or an embed in both Context and Interaction."""
        try:
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
        except discord.errors.NotFound:
            print("Interaction not found or has expired.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")


class SongSelectionView(View):
    def __init__(self, ctx, search_results: list, music_player):
        super().__init__(timeout=30)
        self.ctx = ctx
        self.search_results = search_results
        self.music_player = music_player

        for i in range(1, len(self.search_results) + 1):
            button = SongSelectionButton(
                i, self.search_results[i - 1], self.music_player, ctx.author.id
            )
            self.add_item(button)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        await self.ctx.send("Time is up! You didn't select a song.")
        await self.ctx.edit(view=self)


class SongSelectionButton(Button):
    def __init__(self, index, song_info, music_player, original_user_id):
        super().__init__(label=str(index), style=discord.ButtonStyle.primary)
        self.song_info = song_info
        self.music_player = music_player
        self.original_user_id = original_user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.original_user_id:
            await interaction.response.send_message(
                ":x: This isn't your message! You cannot select a song.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f":arrow_right: You selected {self.song_info[1]}."
        )

        for button in self.view.children:
            button.disabled = True
        self.view.stop()

        voice_channel = interaction.user.voice.channel
        if not interaction.guild.voice_client:
            await voice_channel.connect()

        await self.music_player.add_to_queue(
            interaction,
            voice_channel,
            self.song_info[0],
            json_get(interaction.guild_id),
        )

        await interaction.message.edit(view=self.view)

        asyncio.create_task(self.disable_buttons_after_timeout(interaction.message))

    async def disable_buttons_after_timeout(self, message):
        await asyncio.sleep(30)
        for button in self.view.children:
            button.disabled = True
        await message.edit(view=self.view)


async def setup(bot):
    await bot.add_cog(MusicPlayer(bot))
