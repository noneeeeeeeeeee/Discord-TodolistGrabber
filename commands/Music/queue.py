import discord
from discord.ext import commands
from discord.ui import View, Button
from collections import deque
import random
import re

from typing import Optional

from modules.music.music_player import is_voice_playing


class QueuePaginationView(View):
    """Pagination buttons for queue navigation."""

    def __init__(
        self, ctx: commands.Context, player, current_page: int, total_pages: int
    ):
        super().__init__(timeout=120)  # 2 minute timeout
        self.ctx = ctx
        self.player = player
        self.current_page = current_page
        self.total_pages = total_pages
        self.message = None

        # Disable buttons if only one page
        if total_pages <= 1:
            self.first_button.disabled = True
            self.prev_button.disabled = True
            self.next_button.disabled = True
            self.last_button.disabled = True
        else:
            # Update button states based on current page
            self._update_buttons()

    def _update_buttons(self):
        """Update button disabled states based on current page."""
        self.first_button.disabled = self.current_page == 1
        self.prev_button.disabled = self.current_page == 1
        self.next_button.disabled = self.current_page >= self.total_pages
        self.last_button.disabled = self.current_page >= self.total_pages

    async def _update_queue_display(
        self, interaction: discord.Interaction, new_page: int
    ):
        """Update the queue embed with new page."""
        self.current_page = new_page
        self._update_buttons()

        # Get queue commands cog to reuse the formatting logic
        queue_cog = self.ctx.bot.get_cog("QueueCommands")
        if not queue_cog:
            await interaction.response.send_message(
                "Queue system unavailable.", ephemeral=True
            )
            return

        current = self.player._current_entries.get(self.ctx.guild.id)
        upcoming = list(self.player.queues.get(self.ctx.guild.id, []))

        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blurple())

        if current:
            # Add thumbnail from current track
            track_obj = current.get("track")
            if track_obj:
                artwork_url = getattr(track_obj, "artwork_url", None)
                uri = current.get("uri")
                if artwork_url:
                    embed.set_thumbnail(url=artwork_url)
                elif uri and "youtube.com/watch?v=" in uri:
                    video_id = uri.split("v=")[1].split("&")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )
                elif uri and "youtu.be/" in uri:
                    video_id = uri.split("youtu.be/")[1].split("?")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )

            embed.add_field(
                name="▶️ Now Playing",
                value=queue_cog._format_entry(current, None, is_current=True),
                inline=False,
            )

        if upcoming:
            items_per_page = 10
            start_idx = (self.current_page - 1) * items_per_page
            end_idx = min(start_idx + items_per_page, len(upcoming))

            lines = []
            char_count = 0
            for idx in range(start_idx, end_idx):
                entry = upcoming[idx]
                line = queue_cog._format_entry(entry, idx + 1)
                if char_count + len(line) + 1 > 1000:
                    break
                lines.append(line)
                char_count += len(line) + 1

            queue_text = "\n".join(lines) if lines else "*Page is empty*"

            embed.add_field(
                name=f"⏭️ Up Next (Page {self.current_page}/{self.total_pages}) • {len(upcoming)} total",
                value=queue_text,
                inline=False,
            )
        else:
            embed.add_field(
                name="⏭️ Up Next",
                value="*Queue empty*",
                inline=False,
            )

        repeat_mode = self.player.repeat_mode.get(self.ctx.guild.id, "off")
        repeat_status = "On" if repeat_mode == "track" else "Off"
        embed.add_field(name="🔁 Repeat", value=repeat_status, inline=True)

        autoplay_enabled = self.player.is_session_autoplay_enabled(self.ctx.guild.id)
        autoplay_status = "Enabled" if autoplay_enabled else "Disabled (session)"
        embed.add_field(name="🤖 AutoPlay", value=autoplay_status, inline=True)

        # Add empty field for alignment
        embed.add_field(name="​", value="​", inline=True)

        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        """Disable all buttons when view times out."""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except:
                pass

    @discord.ui.button(
        emoji="⏪", style=discord.ButtonStyle.secondary, custom_id="queue:first"
    )
    async def first_button(self, interaction: discord.Interaction, button: Button):
        """Go to first page."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your queue view!", ephemeral=True
            )
            return
        await self._update_queue_display(interaction, 1)

    @discord.ui.button(
        emoji="◀️", style=discord.ButtonStyle.primary, custom_id="queue:prev"
    )
    async def prev_button(self, interaction: discord.Interaction, button: Button):
        """Go to previous page."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your queue view!", ephemeral=True
            )
            return
        await self._update_queue_display(interaction, max(1, self.current_page - 1))

    @discord.ui.button(
        emoji="▶️", style=discord.ButtonStyle.primary, custom_id="queue:next"
    )
    async def next_button(self, interaction: discord.Interaction, button: Button):
        """Go to next page."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your queue view!", ephemeral=True
            )
            return
        await self._update_queue_display(
            interaction, min(self.total_pages, self.current_page + 1)
        )

    @discord.ui.button(
        emoji="⏩", style=discord.ButtonStyle.secondary, custom_id="queue:last"
    )
    async def last_button(self, interaction: discord.Interaction, button: Button):
        """Go to last page."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your queue view!", ephemeral=True
            )
            return
        await self._update_queue_display(interaction, self.total_pages)

    @discord.ui.button(
        emoji="⚙️", style=discord.ButtonStyle.success, custom_id="queue:manage", row=1
    )
    async def manage_button(self, interaction: discord.Interaction, button: Button):
        """Open queue management menu."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your queue view!", ephemeral=True
            )
            return

        # Import QueueManagementView from nowplaying
        try:
            from commands.Music.nowplaying import QueueManagementView

            view = QueueManagementView(self.ctx, self.player)
            await interaction.response.send_message(
                "📋 Queue Management:", view=view, ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to open queue management: {e}", ephemeral=True
            )


class QueueCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="queue", description="Show current queue.", aliases=["q"]
    )
    async def queue(self, ctx: commands.Context, page: int = 1):
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend not available.")
            return

        is_valid, error_msg = player.check_user_in_bot_vc(ctx.author, ctx.guild)
        if not is_valid:
            await ctx.send(error_msg, delete_after=10)
            return

        current = player._current_entries.get(ctx.guild.id)
        upcoming = list(player.queues.get(ctx.guild.id, []))

        if not current and not upcoming:
            await ctx.send("Nothing is playing or queued.")
            return

        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blurple())

        if current:
            # Add thumbnail from current track
            track_obj = current.get("track")
            if track_obj:
                artwork_url = getattr(track_obj, "artwork_url", None)
                uri = current.get("uri")
                if artwork_url:
                    embed.set_thumbnail(url=artwork_url)
                elif uri and "youtube.com/watch?v=" in uri:
                    video_id = uri.split("v=")[1].split("&")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )
                elif uri and "youtu.be/" in uri:
                    video_id = uri.split("youtu.be/")[1].split("?")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )

            embed.add_field(
                name="▶️ Now Playing",
                value=self._format_entry(current, None, is_current=True),
                inline=False,
            )

        if upcoming:
            items_per_page = 10
            total_pages = max(1, (len(upcoming) + items_per_page - 1) // items_per_page)
            page = max(1, min(page, total_pages))

            start_idx = (page - 1) * items_per_page
            end_idx = min(start_idx + items_per_page, len(upcoming))

            lines = []
            char_count = 0
            for idx in range(start_idx, end_idx):
                entry = upcoming[idx]
                line = self._format_entry(entry, idx + 1)
                if char_count + len(line) + 1 > 1000:
                    break
                lines.append(line)
                char_count += len(line) + 1

            queue_text = "\n".join(lines) if lines else "*Page is empty*"

            embed.add_field(
                name=f"⏭️ Up Next (Page {page}/{total_pages}) • {len(upcoming)} total",
                value=queue_text,
                inline=False,
            )

            if total_pages > 1:
                embed.set_footer(text=f"Use /queue {page + 1} to see the next page")
        else:
            total_pages = 1
            embed.add_field(
                name="⏭️ Up Next",
                value="*Queue empty*",
                inline=False,
            )

        repeat_mode = player.repeat_mode.get(ctx.guild.id, "off")
        repeat_status = "On" if repeat_mode == "track" else "Off"
        embed.add_field(name="🔁 Repeat", value=repeat_status, inline=True)

        autoplay_enabled = player.is_session_autoplay_enabled(ctx.guild.id)
        autoplay_status = "Enabled" if autoplay_enabled else "Disabled (session)"
        embed.add_field(name="🤖 AutoPlay", value=autoplay_status, inline=True)

        embed.add_field(name="​", value="​", inline=True)

        if upcoming and total_pages > 1:
            view = QueuePaginationView(ctx, player, page, total_pages)
            message = await ctx.send(embed=embed, view=view)
            view.message = message
        else:
            await ctx.send(embed=embed)

    @discord.app_commands.command(name="q", description="Show current queue.")
    async def queue_slash_alias(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        ctx.command = self.queue
        await self.queue.callback(self, ctx)

    def _format_entry(
        self, entry: dict, index: Optional[int], is_current: bool = False
    ) -> str:
        title = entry.get("title") or "Unknown"
        uri = entry.get("uri")

        # Truncate title if too long
        max_title_length = 80
        if len(title) > max_title_length:
            title = title[: max_title_length - 3] + "..."

        # Format duration if available
        track_obj = entry.get("track")
        duration_str = ""
        if track_obj:
            dur = getattr(track_obj, "length", None)
            if dur:
                minutes, seconds = divmod(int(dur / 1000), 60)
                duration_str = f" `[{minutes}:{seconds:02d}]`"

        if uri:
            title = f"[{title}]({uri})"

        requester = entry.get("requester")
        if requester:
            who = f"<@{requester}>"
        else:
            who = "AutoPlay"

        marker = ":sparkles:" if entry.get("autoplay") else "👤"

        if is_current:
            return f"**{title}**{duration_str}\nRequested by: {who}"
        else:
            prefix = f"`{index}.` " if index is not None else ""
            return f"{prefix}{marker} {title}{duration_str}"

    @commands.hybrid_command(
        name="shuffle",
        description="Shuffle the queue order (reorders tracks, doesn't affect playback pattern).",
    )
    async def shuffle(self, ctx: commands.Context):
        """
        Shuffle the queue order.

        Note: This reorders the tracks in the queue, but playback still follows
        the queue order sequentially. It does NOT play tracks in random order.
        """
        if not (
            ctx.author.guild_permissions.manage_guild
            or ctx.author.guild_permissions.administrator
        ):
            await ctx.send(":x: Admin or DJ required.")
            return
        player = self.bot.get_cog("MusicPlayer")
        q = list(player.queues.get(ctx.guild.id, []))
        if not q:
            await ctx.send("❌ Queue is empty.")
            return
        random.shuffle(q)
        player.queues[ctx.guild.id] = deque(q)
        player.shuffle_flags[ctx.guild.id] = True
        await ctx.send(f"🔀 Queue shuffled! {len(q)} tracks reordered.")

    @commands.hybrid_command(name="reverse", description="Reverse queue order.")
    async def reverse(self, ctx: commands.Context):
        player = self.bot.get_cog("MusicPlayer")
        q = list(player.queues.get(ctx.guild.id, []))[::-1]
        player.queues[ctx.guild.id] = deque(q)
        await ctx.send("Queue reversed.")

    @commands.hybrid_command(
        name="clearqueue",
        description="Clear queue tracks. Supports ranges (1-5), lists (1,2,3), or clear all.",
        aliases=["cq"],
    )
    async def clearqueue(self, ctx: commands.Context, *, positions: str = None):
        """
        Clear tracks from queue with flexible syntax.

        Usage:
          /cq           - Clear entire queue
          /cq 1         - Remove track 1
          /cq 1,2,3     - Remove tracks 1, 2, and 3
          /cq 1-5       - Remove tracks 1 through 5
          /cq 1-5,8,10  - Remove tracks 1-5, 8, and 10
          /cq 1,2,2,2   - Duplicates ignored (only removes track 1 and 2)
        """
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend not available.")
            return

        is_valid, error_msg = player.check_user_in_bot_vc(ctx.author, ctx.guild)
        if not is_valid:
            await ctx.send(error_msg, delete_after=10)
            return

        queue = player.queues.get(ctx.guild.id, deque())
        if not queue:
            await ctx.send("Queue is already empty.")
            return

        queue_size = len(queue)

        # Clear entire queue if no positions specified
        if positions is None or positions.strip() == "":
            player.queues[ctx.guild.id] = deque()
            await ctx.send(f"🗑️ Cleared entire queue ({queue_size} tracks removed).")
            return

        # Parse positions
        try:
            positions_to_remove = self._parse_positions(positions, queue_size)
        except ValueError as e:
            await ctx.send(f":x: {str(e)}")
            return

        if not positions_to_remove:
            await ctx.send(":x: No valid positions specified.")
            return

        # Remove duplicates and sort in descending order
        positions_to_remove = sorted(set(positions_to_remove), reverse=True)

        # Validate all positions are within range
        invalid = [p for p in positions_to_remove if p < 1 or p > queue_size]
        if invalid:
            await ctx.send(
                f":x: Invalid position(s): {', '.join(map(str, invalid))}. "
                f"Queue has {queue_size} tracks (1-{queue_size})."
            )
            return

        # Remove tracks (in reverse order to maintain correct indices)
        queue_list = list(queue)
        removed_titles = []
        for pos in positions_to_remove:
            idx = pos - 1  # Convert to 0-based index
            if 0 <= idx < len(queue_list):
                removed = queue_list.pop(idx)
                title = removed.get("title", "Unknown")
                # Truncate long titles
                if len(title) > 50:
                    title = title[:47] + "..."
                removed_titles.append(f"`{pos}.` {title}")

        player.queues[ctx.guild.id] = deque(queue_list)

        # Format response
        count = len(positions_to_remove)
        if count == 1:
            await ctx.send(f"🗑️ Removed from queue:\n{removed_titles[0]}")
        elif count <= 5:
            await ctx.send(
                f"🗑️ Removed {count} tracks from queue:\n" + "\n".join(removed_titles)
            )
        else:
            # Show first 3 and last 2 if more than 5
            preview = removed_titles[:3] + ["..."] + removed_titles[-2:]
            await ctx.send(
                f"🗑️ Removed {count} tracks from queue:\n" + "\n".join(preview)
            )

    def _parse_positions(self, positions_str: str, max_position: int) -> list[int]:
        """
        Parse position string into list of integers.
        Supports: 1,2,3 or 1-5 or 1-5,8,10
        """
        result = []

        # Remove whitespace and trailing commas
        positions_str = positions_str.strip().rstrip(",")

        # Split by comma
        parts = positions_str.split(",")

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Check for range (e.g., 1-5)
            if "-" in part:
                range_match = re.match(r"^(\d+)-(\d+)$", part)
                if not range_match:
                    raise ValueError(
                        f"Invalid range format: '{part}'. Use format like '1-5'."
                    )

                start = int(range_match.group(1))
                end = int(range_match.group(2))

                if start > end:
                    raise ValueError(
                        f"Invalid range '{part}': start ({start}) is greater than end ({end})."
                    )

                result.extend(range(start, end + 1))
            else:
                # Single number
                if not part.isdigit():
                    raise ValueError(f"Invalid position: '{part}'. Must be a number.")
                result.append(int(part))

        return result

    @commands.hybrid_command(
        name="managequeue",
        description="Open queue management menu for sorting, removing duplicates, etc.",
        aliases=["mq"],
    )
    async def managequeue(self, ctx: commands.Context):
        """
        Open interactive queue management menu.

        Options:
        - Remove Duplicates: Remove duplicate songs from queue
        - Reverse Queue: Reverse the order of the queue
        - Remove Absent Users' Songs: Remove tracks from users not in voice
        - Sort by Title: Sort queue alphabetically by song title
        - Sort by Requester: Sort queue by who requested each song
        """
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend not available.")
            return

        is_valid, error_msg = player.check_user_in_bot_vc(ctx.author, ctx.guild)
        if not is_valid:
            await ctx.send(error_msg, delete_after=10)
            return

        queue = player.queues.get(ctx.guild.id, deque())
        if not queue:
            await ctx.send("❌ Queue is empty. Nothing to manage.")
            return

        # Import QueueManagementView from nowplaying
        try:
            from commands.Music.nowplaying import QueueManagementView

            view = QueueManagementView(ctx, player)
            await ctx.send("📋 Queue Management:", view=view, ephemeral=True)
        except Exception as e:
            await ctx.send(f"❌ Failed to open queue management: {e}")
            return


async def setup(bot):
    await bot.add_cog(QueueCommands(bot))
