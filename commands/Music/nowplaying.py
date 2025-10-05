"""
Unified Now Playing command with interactive player controls.
Mimics Rythm bot's player interface with play/pause, skip, repeat, settings, and close buttons.
"""

import discord
from discord.ext import commands
from discord.ui import View, Button, Select
import asyncio
import logging
from typing import Optional
import pomice

LOG = logging.getLogger(__name__)


class SessionSettingsView(View):
    """Session settings dropdown menu for autoplay, repeat, volume, seek, and queue management."""

    def __init__(
        self, ctx: commands.Context, player, timeout=180, allow_everyone=False
    ):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.player = player
        self.guild_id = ctx.guild.id
        self.allow_everyone = allow_everyone  # If True, anyone can use (with voting)

    @discord.ui.select(
        placeholder="Session Settings",
        options=[
            discord.SelectOption(label="Toggle AutoPlay", value="autoplay", emoji="🤖"),
            discord.SelectOption(label="Repeat Mode", value="repeat", emoji="🔁"),
            discord.SelectOption(label="Volume", value="volume", emoji="🔊"),
            discord.SelectOption(label="Seek Position", value="seek", emoji="⏩"),
            discord.SelectOption(
                label="Manage Queue", value="manage_queue", emoji="📋"
            ),
        ],
    )
    async def settings_select(self, interaction: discord.Interaction, select: Select):
        # Allow everyone if this is announcement player, otherwise only author
        if not self.allow_everyone and interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your player control!", ephemeral=True
            )
            return

        choice = select.values[0]

        if choice == "autoplay":
            current = self.player.is_session_autoplay_enabled(self.guild_id)
            self.player.set_session_autoplay(self.guild_id, not current)
            status = "enabled" if not current else "disabled"
            await interaction.response.send_message(
                f"🤖 **{interaction.user.display_name}** {status} AutoPlay"
            )

        elif choice == "repeat":
            # Show repeat options
            view = RepeatOptionsView(self.ctx, self.player)
            await interaction.response.send_message(
                "🔁 Select repeat mode:", view=view, ephemeral=True
            )

        elif choice == "volume":
            # Open modal for volume input
            modal = VolumeModal(self.ctx, self.player)
            await interaction.response.send_modal(modal)

        elif choice == "seek":
            # Open modal for seek input
            modal = SeekModal(self.ctx, self.player)
            await interaction.response.send_modal(modal)

        elif choice == "manage_queue":
            view = QueueManagementView(self.ctx, self.player)
            await interaction.response.send_message(
                "📋 Queue Management:", view=view, ephemeral=True
            )


class RepeatOptionsView(View):
    """Granular repeat options dropdown."""

    def __init__(self, ctx: commands.Context, player):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.player = player

    @discord.ui.select(
        placeholder="Choose repeat mode",
        options=[
            discord.SelectOption(
                label="Off", value="off", description="Disable repeat", emoji="⏹️"
            ),
            discord.SelectOption(
                label="Track",
                value="track",
                description="Repeat current track",
                emoji="🔂",
            ),
            discord.SelectOption(
                label="Queue",
                value="queue",
                description="Repeat entire queue",
                emoji="🔁",
            ),
        ],
    )
    async def repeat_select(self, interaction: discord.Interaction, select: Select):
        from modules.music.player_actions import handle_repeat_action

        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your control!", ephemeral=True
            )
            return

        mode = select.values[0]
        result = await handle_repeat_action(
            self.player, self.ctx.guild, interaction.user, mode
        )

        if result["ephemeral"]:
            await interaction.response.send_message(result["message"], ephemeral=True)
        else:
            await interaction.response.send_message(result["message"])
            # Also send to the channel if not ephemeral
            if self.ctx.channel:
                await self.ctx.channel.send(result["message"])


class VolumeModal(discord.ui.Modal, title="Set Volume"):
    """Modal for setting volume."""

    volume_input = discord.ui.TextInput(
        label="Volume (0-200)",
        placeholder="e.g., 100",
        required=True,
        max_length=3,
    )

    def __init__(self, ctx: commands.Context, player):
        super().__init__()
        self.ctx = ctx
        self.player = player

    async def on_submit(self, interaction: discord.Interaction):
        try:
            volume = int(self.volume_input.value.strip())
            if not (0 <= volume <= 200):
                await interaction.response.send_message(
                    "❌ Volume must be between 0 and 200!", ephemeral=True
                )
                return

            vc = self.ctx.guild.voice_client
            if vc:
                is_dj = await self.player._check_dj(interaction.user, self.ctx.guild)

                if is_dj:
                    await vc.set_volume(volume)
                    from modules.music.player_actions import _save_volume_to_config

                    await _save_volume_to_config(self.player, self.ctx.guild.id, volume)

                    await interaction.response.send_message(
                        f"🔊 **{interaction.user.display_name}** set volume to {volume}%"
                    )
                else:
                    vote_result = await self.player.handle_vote_action(
                        self.ctx.guild.id,
                        interaction.user.id,
                        f"volume_{volume}",
                        vc.channel,
                    )
                    if vote_result["passed"]:
                        await vc.set_volume(volume)
                        from modules.music.player_actions import _save_volume_to_config

                        await _save_volume_to_config(
                            self.player, self.ctx.guild.id, volume
                        )

                        await interaction.response.send_message(
                            f"🔊 **{interaction.user.display_name}** set volume to {volume}% ({vote_result['votes']}/{vote_result['needed']} votes)"
                        )
                    else:
                        await interaction.response.send_message(
                            f"🗳️ **{interaction.user.display_name}** voted to set volume to {volume}% ({vote_result['votes']}/{vote_result['needed']} needed)",
                            ephemeral=True,
                        )
            else:
                await interaction.response.send_message(
                    "❌ Not connected to voice", ephemeral=True
                )
        except ValueError:
            await interaction.response.send_message(
                "❌ Please enter a valid number (0-100)!", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to set volume: {e}", ephemeral=True
            )


class VolumeView(View):
    """Volume adjustment controls."""

    def __init__(self, ctx: commands.Context, player):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.player = player

        # Get current volume
        vc = ctx.guild.voice_client
        current_vol = int(vc.volume * 100) if vc and hasattr(vc, "volume") else 50

        # Add volume buttons (reasonable preset levels)
        for vol in [50, 100, 150, 200]:
            btn = Button(
                label=f"{vol}%",
                style=(
                    discord.ButtonStyle.primary
                    if vol == current_vol
                    else discord.ButtonStyle.secondary
                ),
            )
            btn.callback = self._create_volume_callback(vol)
            self.add_item(btn)

    def _create_volume_callback(self, volume: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message(
                    "This isn't your control!", ephemeral=True
                )
                return

            vc = self.ctx.guild.voice_client
            if vc:
                try:
                    # Set volume (volume is already in 0-200 range)
                    await vc.set_volume(volume)
                    # Save volume to config if RememberLastVolume is enabled
                    from modules.music.player_actions import _save_volume_to_config

                    player_cog = self.ctx.bot.get_cog("MusicPlayer")
                    if player_cog:
                        await _save_volume_to_config(
                            player_cog, self.ctx.guild.id, volume
                        )

                    await interaction.response.send_message(
                        f"🔊 **{interaction.user.display_name}** set volume to {volume}%"
                    )
                except Exception as e:
                    await interaction.response.send_message(
                        f"❌ Failed to set volume: {e}", ephemeral=True
                    )
            else:
                await interaction.response.send_message(
                    "❌ Not connected to voice", ephemeral=True
                )

        return callback


class SeekModal(discord.ui.Modal, title="Seek Position"):
    """Modal for seeking to a specific time."""

    time_input = discord.ui.TextInput(
        label="Time (HH:MM:SS or M:SS)",
        placeholder="e.g., 1:30 or 0:01:30",
        required=True,
        max_length=8,
    )

    def __init__(self, ctx: commands.Context, player):
        super().__init__()
        self.ctx = ctx
        self.player = player

    async def on_submit(self, interaction: discord.Interaction):
        vc = self.ctx.guild.voice_client
        if not vc:
            await interaction.response.send_message(
                "❌ Not connected to voice", ephemeral=True
            )
            return

        # Parse time string
        try:
            time_str = self.time_input.value.strip()
            parts = time_str.split(":")

            if len(parts) == 2:  # M:SS
                minutes, seconds = map(int, parts)
                hours = 0
            elif len(parts) == 3:  # HH:MM:SS
                hours, minutes, seconds = map(int, parts)
            else:
                await interaction.response.send_message(
                    "❌ Invalid format! Use HH:MM:SS or M:SS", ephemeral=True
                )
                return

            position_ms = (hours * 3600 + minutes * 60 + seconds) * 1000

            # Check if position is valid
            current_entry = self.player._current_entries.get(self.ctx.guild.id, {})
            track = current_entry.get("track")
            if track and hasattr(track, "length"):
                if position_ms > track.length:
                    await interaction.response.send_message(
                        f"❌ Position too large! Track is {self._format_duration(track.length)}",
                        ephemeral=True,
                    )
                    return

            # Seek to position
            await vc.seek(position_ms)
            await interaction.response.send_message(
                f"⏩ **{interaction.user.display_name}** seeked to {time_str}"
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid time format! Use numbers only (e.g., 1:30)", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to seek: {e}", ephemeral=True
            )

    def _format_duration(self, ms: int) -> str:
        """Format milliseconds as MM:SS."""
        total_seconds = int(ms / 1000)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}:{seconds:02d}"


class SeekView(View):
    """Seek button that opens a modal."""

    def __init__(self, ctx: commands.Context, player):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.player = player

        # Get current track duration
        vc = ctx.guild.voice_client
        current_entry = player._current_entries.get(ctx.guild.id, {})
        track = current_entry.get("track")
        duration_ms = getattr(track, "length", 0) if track else 0

        if duration_ms > 0:
            total_seconds = int(duration_ms / 1000)
            # Add seek buttons for 25%, 50%, 75% positions
            positions = [
                ("⏪ Start", 0),
                ("25%", total_seconds // 4),
                ("50%", total_seconds // 2),
                ("75%", (total_seconds * 3) // 4),
            ]

            for label, seconds in positions:
                btn = Button(label=label, style=discord.ButtonStyle.secondary)
                btn.callback = self._create_seek_callback(seconds)
                self.add_item(btn)

    def _create_seek_callback(self, position_seconds: int):
        async def callback(interaction: discord.Interaction):
            vc = self.ctx.guild.voice_client
            if not vc or not vc.is_playing:
                await interaction.response.send_message(
                    "❌ Nothing is playing", ephemeral=True
                )
                return

            try:
                await vc.seek(position_seconds * 1000)  # Convert to milliseconds
                mins, secs = divmod(position_seconds, 60)
                username = interaction.user.display_name
                message = f"⏩ **{username}** seeked to {mins:02d}:{secs:02d}"
                await interaction.response.send_message(message)
                if self.ctx.channel:
                    await self.ctx.channel.send(message)
            except Exception as e:
                await interaction.response.send_message(
                    f"❌ Failed to seek: {e}", ephemeral=True
                )

        return callback


class QueueManagementView(View):
    """Queue management options."""

    def __init__(self, ctx: commands.Context, player):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.player = player

    @discord.ui.select(
        placeholder="Choose queue action",
        options=[
            discord.SelectOption(
                label="Remove Duplicates", value="remove_dupes", emoji="🗑️"
            ),
            discord.SelectOption(label="Reverse Queue", value="reverse", emoji="🔄"),
            discord.SelectOption(
                label="Remove Absent Users' Songs", value="remove_absent", emoji="👤"
            ),
            discord.SelectOption(label="Sort by Title", value="sort_title", emoji="🔤"),
            discord.SelectOption(
                label="Sort by Requester", value="sort_requester", emoji="👥"
            ),
        ],
    )
    async def queue_action_select(
        self, interaction: discord.Interaction, select: Select
    ):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This isn't your control!", ephemeral=True
            )
            return

        action = select.values[0]
        guild_id = self.ctx.guild.id
        queue = self.player.queues.get(guild_id, [])

        if not queue:
            await interaction.response.send_message("❌ Queue is empty", ephemeral=True)
            return

        username = interaction.user.display_name

        if action == "remove_dupes":
            from collections import deque

            seen = set()
            new_queue = deque()
            removed = 0
            for item in queue:
                key = item.get("title", "").lower()
                if key not in seen:
                    seen.add(key)
                    new_queue.append(item)
                else:
                    removed += 1
            self.player.queues[guild_id] = new_queue
            message = f"🗑️ **{username}** removed {removed} duplicate song(s) from queue"
            await interaction.response.send_message(message)
            if self.ctx.channel:
                await self.ctx.channel.send(message)

        elif action == "reverse":
            from collections import deque

            self.player.queues[guild_id] = deque(list(queue)[::-1])
            message = f"🔄 **{username}** reversed the queue"
            await interaction.response.send_message(message)
            if self.ctx.channel:
                await self.ctx.channel.send(message)

        elif action == "remove_absent":
            from collections import deque

            vc = self.ctx.guild.voice_client
            if not vc:
                await interaction.response.send_message(
                    "❌ Not in voice channel", ephemeral=True
                )
                return

            present_ids = {m.id for m in vc.channel.members if not m.bot}
            new_queue = deque(
                [item for item in queue if item.get("requester") in present_ids]
            )
            removed = len(queue) - len(new_queue)
            self.player.queues[guild_id] = new_queue
            message = f"👤 **{username}** removed {removed} song(s) from absent users"
            await interaction.response.send_message(message)
            if self.ctx.channel:
                await self.ctx.channel.send(message)

        elif action == "sort_title":
            from collections import deque

            sorted_queue = sorted(queue, key=lambda x: x.get("title", "").lower())
            self.player.queues[guild_id] = deque(sorted_queue)
            message = f"🔤 **{username}** sorted queue by title"
            await interaction.response.send_message(message)
            if self.ctx.channel:
                await self.ctx.channel.send(message)

        elif action == "sort_requester":
            from collections import deque

            sorted_queue = sorted(queue, key=lambda x: x.get("requester", 0))
            self.player.queues[guild_id] = deque(sorted_queue)
            message = f"👥 **{username}** sorted queue by requester"
            await interaction.response.send_message(message)
            if self.ctx.channel:
                await self.ctx.channel.send(message)


class PlayerControlView(View):
    """Main player control interface with play/pause, skip, repeat, settings, and close buttons."""

    def __init__(
        self,
        ctx: commands.Context,
        player,
        message: discord.Message = None,
        persistent=False,
        is_announcement=False,
    ):
        # For !np command: 2 minute timeout. For auto-announcement: persistent (no timeout)
        super().__init__(timeout=None if persistent else 120)
        self.ctx = ctx
        self.player = player
        self.guild_id = ctx.guild.id
        self.message = message
        self.persistent = persistent
        self.is_announcement = (
            is_announcement  # True for announcement player, False for !np
        )

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
        emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="player:playpause"
    )
    async def play_pause_button(self, interaction: discord.Interaction, button: Button):
        """Toggle play/pause."""
        # Check if user is in voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ You must be in a voice channel!", ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("❌ Not connected", ephemeral=True)
            return

        # Check if user is in same channel as bot
        if interaction.user.voice.channel.id != vc.channel.id:
            await interaction.response.send_message(
                "❌ You must be in the same voice channel as the bot!", ephemeral=True
            )
            return

        # Check DJ or initiate vote
        is_dj = await self.player._check_dj(interaction.user, interaction.guild)

        if vc.is_paused:
            if is_dj:
                await vc.set_pause(False)
                await interaction.response.send_message(
                    f"▶️ **{interaction.user.display_name}** resumed the player"
                )
            else:
                vote_result = await self.player.handle_vote_action(
                    self.guild_id, interaction.user.id, "resume", vc.channel
                )
                if vote_result["passed"]:
                    await vc.set_pause(False)
                    await interaction.response.send_message(
                        f"▶️ **{interaction.user.display_name}** resumed the player ({vote_result['votes']}/{vote_result['needed']} votes)"
                    )
                else:
                    await interaction.response.send_message(
                        f"🗳️ **{interaction.user.display_name}** voted to resume ({vote_result['votes']}/{vote_result['needed']} needed)",
                        ephemeral=True,
                    )
        else:
            if is_dj:
                await vc.set_pause(True)
                await interaction.response.send_message(
                    f"⏸️ **{interaction.user.display_name}** paused the player"
                )
            else:
                vote_result = await self.player.handle_vote_action(
                    self.guild_id, interaction.user.id, "pause", vc.channel
                )
                if vote_result["passed"]:
                    await vc.set_pause(True)
                    await interaction.response.send_message(
                        f"⏸️ **{interaction.user.display_name}** paused the player ({vote_result['votes']}/{vote_result['needed']} votes)"
                    )
                else:
                    await interaction.response.send_message(
                        f"🗳️ **{interaction.user.display_name}** voted to pause ({vote_result['votes']}/{vote_result['needed']} needed)",
                        ephemeral=True,
                    )

    @discord.ui.button(
        emoji="⏭️", style=discord.ButtonStyle.primary, custom_id="player:skip"
    )
    async def skip_button(self, interaction: discord.Interaction, button: Button):
        """Skip current track."""
        # Check if user is in voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ You must be in a voice channel!", ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing:
            await interaction.response.send_message(
                "❌ Nothing playing", ephemeral=True
            )
            return

        # Check if user is in same channel as bot
        if interaction.user.voice.channel.id != vc.channel.id:
            await interaction.response.send_message(
                "❌ You must be in the same voice channel as the bot!", ephemeral=True
            )
            return

        is_dj = await self.player._check_dj(interaction.user, interaction.guild)

        if is_dj:
            # Actually stop the track
            try:
                await vc.stop()
                await interaction.response.send_message(
                    f"⏭️ **{interaction.user.display_name}** skipped the track"
                )
            except Exception as e:
                await interaction.response.send_message(
                    f"❌ Failed to skip: {e}", ephemeral=True
                )
        else:
            vote_result = await self.player.handle_vote_action(
                self.guild_id, interaction.user.id, "skip", vc.channel
            )
            if vote_result["passed"]:
                try:
                    await vc.stop()
                    await interaction.response.send_message(
                        f"⏭️ **{interaction.user.display_name}** skipped the track ({vote_result['votes']}/{vote_result['needed']} votes)"
                    )
                except Exception as e:
                    await interaction.response.send_message(
                        f"❌ Failed to skip: {e}", ephemeral=True
                    )
            else:
                await interaction.response.send_message(
                    f"🗳️ **{interaction.user.display_name}** voted to skip ({vote_result['votes']}/{vote_result['needed']} needed)",
                    ephemeral=True,
                )

    @discord.ui.button(
        emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="player:repeat"
    )
    async def repeat_button(self, interaction: discord.Interaction, button: Button):
        """Cycle through repeat modes."""
        from modules.music.player_actions import (
            handle_repeat_action,
            validate_user_in_voice,
            validate_same_voice_channel,
        )

        # Validation checks
        error = validate_user_in_voice(interaction.user)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message(
                "❌ Not connected to voice channel.", ephemeral=True
            )
            return

        error = validate_same_voice_channel(interaction.user, vc)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        # Cycle to next mode
        current = self.player.repeat_mode.get(self.guild_id, "off")
        modes = ["off", "track", "queue"]
        next_mode = modes[(modes.index(current) + 1) % len(modes)]

        # Handle repeat action using unified function
        result = await handle_repeat_action(
            self.player, interaction.guild, interaction.user, next_mode
        )

        if result["ephemeral"]:
            await interaction.response.send_message(result["message"], ephemeral=True)
        else:
            await interaction.response.send_message(result["message"])

    @discord.ui.button(
        emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="player:settings"
    )
    async def settings_button(self, interaction: discord.Interaction, button: Button):
        """Open session settings."""
        # Allow everyone to use settings on persistent (announcement) players
        view = SessionSettingsView(
            self.ctx, self.player, allow_everyone=self.persistent
        )
        await interaction.response.send_message(
            "⚙️ Session Settings:", view=view, ephemeral=True
        )

    @discord.ui.button(
        emoji="❌", style=discord.ButtonStyle.secondary, custom_id="player:close"
    )
    async def close_button(self, interaction: discord.Interaction, button: Button):
        """Close the player display (does not stop playback)."""
        username = interaction.user.display_name

        # Delete or clear the message
        if self.message:
            try:
                await self.message.delete()
            except:
                await self.message.edit(view=None)

        # For announcement player: public message with username
        # For !np player: ephemeral message without username
        if self.is_announcement:
            await interaction.response.send_message(
                f"🗑️ **{username}** closed the player"
            )
        else:
            await interaction.response.send_message("🗑️ Player closed", ephemeral=True)


class NowPlayingCommands(commands.Cog):
    """Now Playing command with unified player controls."""

    def __init__(self, bot):
        self.bot = bot

    def _get_player(self):
        return self.bot.get_cog("MusicPlayer")

    def _format_duration(self, ms: int) -> str:
        """Format duration in milliseconds to MM:SS."""
        total_seconds = int(ms / 1000)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"

    def _create_seekbar(
        self, position_ms: int, duration_ms: int, length: int = 15
    ) -> str:
        """Create a visual seekbar."""
        if duration_ms <= 0:
            return "▱" * length

        progress = min(1.0, position_ms / duration_ms)
        filled = int(progress * length)
        empty = length - filled

        return "▰" * filled + "▱" * empty

    async def create_now_playing_embed(
        self, ctx: commands.Context, current: dict, track
    ) -> discord.Embed:
        """Create the Now Playing embed similar to Rythm bot."""
        title = current.get("title", "Unknown")
        uri = current.get("uri")
        requester_id = current.get("requester")

        # Create embed with title as link if URI available
        if uri:
            embed = discord.Embed(
                title="Now Playing",
                description=f"[{title}]({uri})",
                color=0x510F7B,  # Rythm's purple color
            )
        else:
            embed = discord.Embed(
                title="Now Playing", description=title, color=0x510F7B
            )

        # Add thumbnail
        if track:
            artwork_url = getattr(track, "artwork_url", None)
            if artwork_url:
                embed.set_thumbnail(url=artwork_url)
            elif uri:
                if "youtube.com/watch?v=" in uri:
                    video_id = uri.split("v=")[1].split("&")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )
                elif "youtu.be/" in uri:
                    video_id = uri.split("youtu.be/")[1].split("?")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )

        # Add duration with seekbar
        vc = ctx.guild.voice_client
        if track and hasattr(track, "length"):
            duration_ms = track.length
            position_ms = vc.position if vc and hasattr(vc, "position") else 0

            current_time = self._format_duration(position_ms)
            total_time = self._format_duration(duration_ms)
            seekbar = self._create_seekbar(position_ms, duration_ms)

            embed.description += (
                f"\n\n**Duration**\n{current_time} {seekbar} {total_time}\n"
            )

        # Add requester as author
        if requester_id:
            try:
                requester = ctx.guild.get_member(requester_id)
                if requester:
                    embed.set_author(
                        name=f"Requested By: {requester.display_name}",
                        icon_url=requester.display_avatar.url,
                    )
                else:
                    embed.set_author(name=f"Requested By: User#{requester_id}")
            except:
                embed.set_author(name=f"Requested By: <@{requester_id}>")

        # Add voice channel location
        if vc and hasattr(vc, "channel"):
            embed.add_field(
                name=" ", value=f":speaker: {vc.channel.name}", inline=False
            )

        # Add source footer
        if track and hasattr(track, "source_name"):
            source_name = track.source_name
            source_icons = {
                "youtube": "🎥 YouTube",
                "spotify": "🎵 Spotify",
                "soundcloud": "🔊 SoundCloud",
                "twitch": "🎮 Twitch",
                "bandcamp": "🎸 Bandcamp",
                "vimeo": "📹 Vimeo",
                "http": "🌐 Direct URL",
            }
            source_text = source_icons.get(
                source_name.lower(), f"🎵 {source_name.title()}"
            )
            embed.set_footer(text=source_text)
        elif uri:
            # Fallback: detect from URI
            if "youtube.com" in uri or "youtu.be" in uri:
                embed.set_footer(text="🎥 YouTube")
            elif "spotify.com" in uri:
                embed.set_footer(text="🎵 Spotify")
            elif "soundcloud.com" in uri:
                embed.set_footer(text="🔊 SoundCloud")

        return embed

    @commands.hybrid_command(
        name="nowplaying",
        description="Show what's currently playing with player controls.",
        aliases=["np", "current"],
    )
    async def nowplaying(self, ctx: commands.Context):
        """Display now playing with interactive player controls."""
        player = self._get_player()
        if not player:
            await ctx.send("❌ Player backend not available.")
            return

        current = player._current_entries.get(ctx.guild.id)
        if not current:
            await ctx.send("❌ Nothing is currently playing.")
            return

        track = current.get("track")
        embed = await self.create_now_playing_embed(ctx, current, track)

        # Create player controls with 2 minute timeout
        view = PlayerControlView(ctx, player, persistent=False)
        message = await ctx.send(embed=embed, view=view)
        view.message = message

        # Don't store !np message in _now_playing_messages (only auto-announcements)

    @commands.hybrid_command(
        name="seek",
        description="Seek to a specific time in the current track.",
        aliases=["scrub"],
    )
    async def seek_command(self, ctx: commands.Context, *, time: str = None):
        """Seek to a specific time (HH:MM:SS or M:SS) or open a modal."""
        player = self._get_player()
        if not player:
            await ctx.send("❌ Player backend not available.")
            return

        vc = ctx.guild.voice_client
        if not vc:
            await ctx.send("❌ Not connected to a voice channel.")
            return

        if not vc.is_playing:
            await ctx.send("❌ Nothing is currently playing.")
            return

        # Check if user is in voice channel
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ You must be in a voice channel!")
            return

        # Check if user is in same channel as bot
        if ctx.author.voice.channel.id != vc.channel.id:
            await ctx.send("❌ You must be in the same voice channel as the bot!")
            return

        # If no time provided, send modal
        if not time:
            modal = SeekModal(ctx, player)
            # For slash commands, send modal directly
            if hasattr(ctx, "interaction") and ctx.interaction:
                await ctx.interaction.response.send_modal(modal)
            else:
                # For prefix commands, send a message to use interaction
                await ctx.send(
                    "⏩ Please use the `/seek` command to open the seek modal, or use `/seek <time>` (e.g., `/seek 1:30`)"
                )
            return

        # Parse time string
        try:
            parts = time.strip().split(":")

            if len(parts) == 2:  # M:SS
                minutes, seconds = map(int, parts)
                hours = 0
            elif len(parts) == 3:  # HH:MM:SS
                hours, minutes, seconds = map(int, parts)
            else:
                await ctx.send("❌ Invalid format! Use HH:MM:SS or M:SS (e.g., 1:30)")
                return

            position_ms = (hours * 3600 + minutes * 60 + seconds) * 1000

            # Check if position is valid
            current_entry = player._current_entries.get(ctx.guild.id, {})
            track = current_entry.get("track")
            if track and hasattr(track, "length"):
                if position_ms > track.length:
                    mins, secs = divmod(int(track.length / 1000), 60)
                    await ctx.send(f"❌ Position too large! Track is {mins}:{secs:02d}")
                    return

            # Check DJ or initiate vote
            is_dj = await player._check_dj(ctx.author, ctx.guild)

            if is_dj:
                await vc.seek(position_ms)
                await ctx.send(f"⏩ **{ctx.author.display_name}** seeked to {time}")
            else:
                # For non-DJs, require vote
                vote_result = await player.handle_vote_action(
                    ctx.guild.id, ctx.author.id, f"seek_{time}", vc.channel
                )
                if vote_result["passed"]:
                    await vc.seek(position_ms)
                    await ctx.send(
                        f"⏩ **{ctx.author.display_name}** seeked to {time} ({vote_result['votes']}/{vote_result['needed']} votes)"
                    )
                else:
                    await ctx.send(
                        f"🗳️ **{ctx.author.display_name}** voted to seek to {time} ({vote_result['votes']}/{vote_result['needed']} needed)"
                    )

        except ValueError:
            await ctx.send("❌ Invalid time format! Use numbers only (e.g., 1:30)")
        except Exception as e:
            await ctx.send(f"❌ Failed to seek: {e}")


async def setup(bot):
    await bot.add_cog(NowPlayingCommands(bot))
