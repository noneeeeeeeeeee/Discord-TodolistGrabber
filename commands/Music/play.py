import re
import logging
import discord
from typing import Optional, List

from discord.ext import commands
from discord.ui import View, Button, Select

from modules.setconfig import check_guild_config_available

LOG = logging.getLogger(__name__)


class SearchResultsView(View):
    """Interactive search results with page navigation and track selection."""

    def __init__(self, ctx, results: List[dict], page: int = 0):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.results = results
        self.page = page
        self.max_pages = (len(results) - 1) // 10 + 1
        self.message = None
        self.selected_index = None

        # Add select menu with current page's tracks
        self._update_select_menu()

        # Add navigation buttons
        prev_button = Button(
            label="◀ Previous", style=discord.ButtonStyle.gray, disabled=page == 0
        )
        prev_button.callback = self.previous_page
        self.add_item(prev_button)

        next_button = Button(
            label="Next ▶",
            style=discord.ButtonStyle.gray,
            disabled=page >= self.max_pages - 1,
        )
        next_button.callback = self.next_page
        self.add_item(next_button)

        cancel_button = Button(label="❌ Cancel", style=discord.ButtonStyle.danger)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def _update_select_menu(self):
        """Update the select menu with tracks from current page."""
        start_idx = self.page * 10
        end_idx = min(start_idx + 10, len(self.results))
        page_results = self.results[start_idx:end_idx]

        options = []
        for i, track in enumerate(page_results, start=start_idx + 1):
            title = track.get("title", "Unknown")[:100]
            author = track.get("author", "Unknown")[:50]
            duration = track.get("duration", 0)

            mins, secs = divmod(duration // 1000, 60)
            duration_str = f"{mins}:{secs:02d}" if duration else "Live"

            options.append(
                discord.SelectOption(
                    label=f"{i}. {title}"[:100],
                    description=f"{author} • {duration_str}"[:100],
                    value=str(i - 1),
                )
            )

        select = Select(
            placeholder=f"Select a track (Page {self.page + 1}/{self.max_pages})",
            options=options,
            custom_id="track_select",
        )
        select.callback = self.on_select

        # Remove old select if exists and add new one at front
        for item in self.children[:]:
            if isinstance(item, Select):
                self.remove_item(item)

        self.children.insert(0, select)

    async def on_select(self, interaction: discord.Interaction):
        """Handle track selection."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This is not your search!", ephemeral=True
            )
            return

        self.selected_index = int(interaction.values[0])
        await interaction.response.defer()

        # Queue the selected track
        player = self.ctx.bot.get_cog("MusicPlayer")
        selected_track = self.results[self.selected_index]

        item = {
            "title": selected_track.get("title", "Unknown"),
            "requester": self.ctx.author.id,
            "source": selected_track.get("uri", selected_track.get("identifier")),
        }

        connection = await player.ensure_player_connected(
            self.ctx.guild, self.ctx.author.id
        )
        if not connection.player:
            await interaction.followup.send(":x: Could not connect to voice channel.")
            self.stop()
            return

        enqueue_result = await player.enqueue(
            self.ctx.guild, item, player=connection.player
        )

        if enqueue_result.success:
            title = selected_track.get("title", "Unknown").replace("`", "\\`")
            if enqueue_result.started_playback:
                await self.message.edit(
                    content=f"▶️ Now playing: **{title}**", view=None, embed=None
                )
            else:
                position = enqueue_result.queue_position or 1
                await self.message.edit(
                    content=f"✅ Added to queue: **{title}** (Position: {position})",
                    view=None,
                    embed=None,
                )
        else:
            await interaction.followup.send(
                f":x: {enqueue_result.error or 'Failed to queue track'}"
            )

        self.stop()

    async def previous_page(self, interaction: discord.Interaction):
        """Go to previous page."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This is not your search!", ephemeral=True
            )
            return

        if self.page > 0:
            self.page -= 1
            self._update_select_menu()

            # Update button states
            self.children[-3].disabled = self.page == 0
            self.children[-2].disabled = False

            embed = self._create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def next_page(self, interaction: discord.Interaction):
        """Go to next page."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This is not your search!", ephemeral=True
            )
            return

        if self.page < self.max_pages - 1:
            self.page += 1
            self._update_select_menu()

            # Update button states
            self.children[-3].disabled = False
            self.children[-2].disabled = self.page >= self.max_pages - 1

            embed = self._create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def cancel(self, interaction: discord.Interaction):
        """Cancel the search."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This is not your search!", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content="🚫 Search cancelled.", view=None, embed=None
        )
        self.stop()

    def _create_embed(self) -> discord.Embed:
        """Create embed showing current page of search results."""
        start_idx = self.page * 10
        end_idx = min(start_idx + 10, len(self.results))
        page_results = self.results[start_idx:end_idx]

        embed = discord.Embed(
            title="🔍 Search Results",
            description=f"Found {len(self.results)} results. Select a track from the menu below.",
            color=discord.Color.blue(),
        )

        for i, track in enumerate(page_results, start=start_idx + 1):
            title = track.get("title", "Unknown")
            author = track.get("author", "Unknown")
            duration = track.get("duration", 0)

            mins, secs = divmod(duration // 1000, 60)
            duration_str = f"{mins}:{secs:02d}" if duration else "Live"

            embed.add_field(
                name=f"{i}. {title}"[:256],
                value=f"**{author}** • `{duration_str}`",
                inline=False,
            )

        embed.set_footer(
            text=f"Page {self.page + 1}/{self.max_pages} • Select a track within 2 minutes"
        )
        return embed

    async def on_timeout(self):
        """Handle timeout."""
        if self.message:
            try:
                await self.message.edit(
                    content="⏱️ Search timed out.", view=None, embed=None
                )
            except:
                pass


class PlayCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _get_player(self):
        return self.bot.get_cog("MusicPlayer")

    @commands.hybrid_command(
        name="play", aliases=["p"], description="Play/search a track or playlist"
    )
    async def play(self, ctx: commands.Context, *, query: str = None):
        """
        Play a song or search for tracks.

        Usage:
          !play <search term>  - Search for tracks (shows top 10 results)
          !play <URL>          - Play directly from URL
          !p <search>          - Same as above
        """
        if not check_guild_config_available(ctx.guild.id):
            await ctx.send("Server not setup. Please run !setup or /setup.")
            return

        player = self._get_player()
        if not player:
            await ctx.send("Player backend not available.")
            return

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

        # Show now playing if no query
        if query is None or query.strip() == "":
            await self._show_now_playing(ctx)
            return

        q = query.strip()

        # Clean up common user mistakes - remove "search" prefix if user typed it
        if q.lower().startswith("search "):
            q = q[7:].strip()  # Remove "search " prefix

        escaped_query = q.replace("`", "\\`")

        # Check if URL or search term
        is_url = re.match(r"https?://", q) is not None
        is_search_prefix = re.match(r"^[a-z]+search\d*:", q) is not None

        # Direct URL playback
        if is_url or is_search_prefix:
            await self._play_direct(ctx, q, escaped_query, player)
            return

        # Search for tracks and show results
        await self._search_and_display(ctx, q, escaped_query, player)

    async def _show_now_playing(self, ctx: commands.Context):
        """Show currently playing track when no query provided."""
        player = self._get_player()
        current = player._current_entries.get(ctx.guild.id)

        if not current:
            await ctx.send(
                "Nothing is currently playing. Use `/play <query>` to search for music!"
            )
            return

        # Reuse the nowplaying command
        queue_cog = self.bot.get_cog("QueueCommands")
        if queue_cog and hasattr(queue_cog, "nowplaying"):
            await queue_cog.nowplaying(ctx)
        else:
            title = current.get("title", "Unknown").replace("`", "\\`")
            await ctx.send(f"▶️ Now playing: **{title}**")

    async def _play_direct(
        self, ctx: commands.Context, source: str, escaped_query: str, player
    ):
        """Play a direct URL without search results."""
        item = {"title": source, "requester": ctx.author.id, "source": source}

        connection = await player.ensure_player_connected(ctx.guild, ctx.author.id)
        if not connection.player:
            reason = (
                player.get_last_enqueue_error(ctx.guild.id)
                or connection.error
                or "Unable to join the voice channel."
            )
            await ctx.send(f":x: {reason}")
            return

        if connection.joined:
            joined_name: Optional[str]
            if connection.joined_channel:
                joined_name = connection.joined_channel
            else:
                author_channel = getattr(
                    getattr(ctx.author, "voice", None), "channel", None
                )
                joined_name = (
                    getattr(author_channel, "name", None) if author_channel else None
                )
                if not joined_name and author_channel:
                    joined_name = str(author_channel)
            if not joined_name:
                joined_name = "Voice Channel"
            await ctx.send(f":arrow_right: Joined `{joined_name}`")

        search_message = await ctx.send(f":mag_right: Loading `{escaped_query}`...")

        enqueue_result = await player.enqueue(ctx.guild, item, player=connection.player)
        if not enqueue_result.success:
            reason = (
                player.get_last_enqueue_error(ctx.guild.id)
                or enqueue_result.error
                or "Unable to queue the track right now."
            )
            await search_message.edit(content=f":x: {reason}")
            return

        added_title = (enqueue_result.track_title or source).replace("`", "\\`")
        if enqueue_result.started_playback:
            await search_message.edit(content=f"▶️ Now playing: **{added_title}**")
        else:
            position = enqueue_result.queue_position or connection.player.queue.count
            await search_message.edit(
                content=(
                    f":clock130: Queued `{added_title}`\n" f"-# Position: {position}"
                )
            )

    async def _search_and_display(
        self, ctx: commands.Context, query: str, escaped_query: str, player
    ):
        """Search for tracks and display interactive results."""
        # Connect to voice first
        connection = await player.ensure_player_connected(ctx.guild, ctx.author.id)
        if not connection.player:
            reason = (
                player.get_last_enqueue_error(ctx.guild.id)
                or connection.error
                or "Unable to join the voice channel."
            )
            await ctx.send(f":x: {reason}")
            return

        if connection.joined:
            joined_name = connection.joined_channel or "Voice Channel"
            await ctx.send(f":arrow_right: Joined `{joined_name}`")

        search_message = await ctx.send(
            f":mag_right: Searching for `{escaped_query}`..."
        )

        # Search using Lavalink
        try:
            vc = ctx.guild.voice_client
            if not vc:
                await search_message.edit(content=":x: Not connected to voice channel.")
                return

            all_tracks = []

            try:
                search_results = await vc.get_tracks(query=f"ytsearch:{query}")
                if search_results:
                    if hasattr(search_results, "tracks"):
                        all_tracks.extend(search_results.tracks)
                    elif isinstance(search_results, list):
                        all_tracks.extend(search_results)
                    else:
                        all_tracks.append(search_results)
            except Exception as e:
                LOG.debug(f"YouTube search failed: {e}")

            try:
                search_results = await vc.get_tracks(query=f"ytmsearch:{query}")
                if search_results:
                    if hasattr(search_results, "tracks"):
                        all_tracks.extend(search_results.tracks)
                    elif isinstance(search_results, list):
                        all_tracks.extend(search_results)
                    else:
                        all_tracks.append(search_results)
            except Exception as e:
                LOG.debug(f"YouTube Music search failed: {e}")

            if not all_tracks:
                await search_message.edit(
                    content=f":x: No results found for `{escaped_query}`."
                )
                return

            # Limit to 50 results max, remove duplicates by identifier
            seen_ids = set()
            unique_tracks = []
            for track in all_tracks:
                track_id = getattr(track, "identifier", None) or getattr(
                    track, "uri", str(track)
                )
                if track_id not in seen_ids:
                    seen_ids.add(track_id)
                    unique_tracks.append(track)
                    if len(unique_tracks) >= 50:
                        break

            # Convert to our format
            results = []
            for track in unique_tracks:
                results.append(
                    {
                        "title": getattr(track, "title", "Unknown"),
                        "author": getattr(track, "author", "Unknown"),
                        "duration": getattr(track, "length", 0),
                        "uri": getattr(track, "uri", ""),
                        "identifier": getattr(track, "identifier", ""),
                    }
                )

            # Create and show interactive search results
            view = SearchResultsView(ctx, results)
            embed = view._create_embed()

            await search_message.edit(content=None, embed=embed, view=view)
            view.message = search_message

        except Exception as e:
            LOG.error(f"Search failed with exception: {e}", exc_info=True)
            await search_message.edit(content=f":x: Search failed: {str(e)}")


async def setup(bot):
    await bot.add_cog(PlayCommands(bot))
