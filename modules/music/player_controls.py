"""
Interactive player controls and unified Now Playing embed.
"""

import discord
from discord.ui import View, Button
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pomice
    from modules.music.music_player import MusicPlayer


class PlayerControlView(View):
    """Interactive control buttons for Now Playing embeds."""

    def __init__(self, guild_id: int, player_cog: "MusicPlayer"):
        super().__init__(timeout=None)  # Persistent view
        self.guild_id = guild_id
        self.player_cog = player_cog

        # Get current player state
        player = self._get_player()
        if not player:
            return

        # Play/Pause button
        is_paused = player.is_paused
        play_pause = Button(
            label="⏸️ Pause" if not is_paused else "▶️ Resume",
            style=(
                discord.ButtonStyle.primary
                if not is_paused
                else discord.ButtonStyle.success
            ),
            custom_id=f"player_pause_{guild_id}",
        )
        play_pause.callback = self.toggle_pause
        self.add_item(play_pause)

        # Skip button
        skip_btn = Button(
            label="⏭️ Skip",
            style=discord.ButtonStyle.gray,
            custom_id=f"player_skip_{guild_id}",
        )
        skip_btn.callback = self.skip
        self.add_item(skip_btn)

        # Repeat button
        repeat_mode = self.player_cog._repeat_mode.get(guild_id, "off")
        repeat_emoji = (
            "🔁" if repeat_mode == "queue" else "🔂" if repeat_mode == "track" else "➡️"
        )
        repeat_btn = Button(
            label=f"{repeat_emoji} Repeat",
            style=discord.ButtonStyle.gray,
            custom_id=f"player_repeat_{guild_id}",
        )
        repeat_btn.callback = self.cycle_repeat
        self.add_item(repeat_btn)

        # AutoPlay button
        autoplay = self.player_cog._autoplay_enabled.get(guild_id, False)
        autoplay_btn = Button(
            label="🎲 AutoPlay" if autoplay else "🎲 AutoPlay",
            style=discord.ButtonStyle.success if autoplay else discord.ButtonStyle.gray,
            custom_id=f"player_autoplay_{guild_id}",
        )
        autoplay_btn.callback = self.toggle_autoplay
        self.add_item(autoplay_btn)

    def _get_player(self) -> Optional["pomice.Player"]:
        """Get the player for this guild."""
        import pomice

        guild = self.player_cog.bot.get_guild(self.guild_id)
        if not guild:
            return None

        player = guild.voice_client
        if isinstance(player, pomice.Player):
            return player
        return None

    async def toggle_pause(self, interaction: discord.Interaction):
        """Handle pause/resume with voting."""
        player = self._get_player()
        if not player:
            await interaction.response.send_message(
                "❌ No active player.", ephemeral=True
            )
            return

        # Check if user is in voice channel
        if (
            not interaction.user.voice
            or interaction.user.voice.channel.id != player.channel.id
        ):
            await interaction.response.send_message(
                "❌ You must be in the voice channel!", ephemeral=True
            )
            return

        # Check if DJ bypass
        is_dj = await self.player_cog._check_dj(interaction.user, interaction.guild)

        action = "resume" if player.is_paused else "pause"

        if is_dj:
            # DJ can bypass voting
            if player.is_paused:
                await player.set_pause(False)
                await interaction.response.send_message(
                    "▶️ Resumed playback.", ephemeral=False
                )
            else:
                await player.set_pause(True)
                await interaction.response.send_message(
                    "⏸️ Paused playback.", ephemeral=False
                )
        else:
            # Require voting
            vote_result = await self.player_cog.handle_vote_action(
                interaction.guild.id, interaction.user.id, action, player.channel
            )

            if vote_result["passed"]:
                if player.is_paused:
                    await player.set_pause(False)
                    await interaction.response.send_message(
                        f"▶️ Vote passed! Resumed playback. ({vote_result['votes']}/{vote_result['needed']} votes)",
                        ephemeral=False,
                    )
                else:
                    await player.set_pause(True)
                    await interaction.response.send_message(
                        f"⏸️ Vote passed! Paused playback. ({vote_result['votes']}/{vote_result['needed']} votes)",
                        ephemeral=False,
                    )
            else:
                await interaction.response.send_message(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to {action}. ({vote_result['votes']}/{vote_result['needed']})",
                    ephemeral=True,
                )

    async def skip(self, interaction: discord.Interaction):
        """Handle skip with voting."""
        player = self._get_player()
        if not player:
            await interaction.response.send_message(
                "❌ No active player.", ephemeral=True
            )
            return

        # Check if user is in voice channel
        if (
            not interaction.user.voice
            or interaction.user.voice.channel.id != player.channel.id
        ):
            await interaction.response.send_message(
                "❌ You must be in the voice channel!", ephemeral=True
            )
            return

        # Check if DJ bypass
        is_dj = await self.player_cog._check_dj(interaction.user, interaction.guild)

        if is_dj:
            # DJ can bypass voting
            await player.stop()
            await interaction.response.send_message("⏭️ Skipped by DJ.", ephemeral=False)
        else:
            # Use existing voteskip system
            vote_result = await self.player_cog.handle_vote_skip(
                interaction.guild.id, interaction.user.id, player.channel
            )

            if vote_result["passed"]:
                await player.stop()
                await interaction.response.send_message(
                    f"⏭️ Vote passed! Skipped track. ({vote_result['votes']}/{vote_result['needed']} votes)",
                    ephemeral=False,
                )
            else:
                await interaction.response.send_message(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to skip. ({vote_result['votes']}/{vote_result['needed']})",
                    ephemeral=True,
                )

    async def cycle_repeat(self, interaction: discord.Interaction):
        """Handle repeat mode cycling with voting."""
        player = self._get_player()
        if not player:
            await interaction.response.send_message(
                "❌ No active player.", ephemeral=True
            )
            return

        # Check if user is in voice channel
        if (
            not interaction.user.voice
            or interaction.user.voice.channel.id != player.channel.id
        ):
            await interaction.response.send_message(
                "❌ You must be in the voice channel!", ephemeral=True
            )
            return

        # Check if DJ bypass
        is_dj = await self.player_cog._check_dj(interaction.user, interaction.guild)

        current_mode = self.player_cog._repeat_mode.get(self.guild_id, "off")
        modes = ["off", "track", "queue"]
        next_mode = modes[(modes.index(current_mode) + 1) % len(modes)]

        if is_dj:
            # DJ can bypass voting
            self.player_cog._repeat_mode[self.guild_id] = next_mode
            mode_text = {"off": "disabled", "track": "track", "queue": "queue"}[
                next_mode
            ]
            await interaction.response.send_message(
                f"🔁 Repeat mode: **{mode_text}**", ephemeral=False
            )
        else:
            # Require voting
            vote_result = await self.player_cog.handle_vote_action(
                interaction.guild.id,
                interaction.user.id,
                f"repeat_{next_mode}",
                player.channel,
            )

            if vote_result["passed"]:
                self.player_cog._repeat_mode[self.guild_id] = next_mode
                mode_text = {"off": "disabled", "track": "track", "queue": "queue"}[
                    next_mode
                ]
                await interaction.response.send_message(
                    f"🔁 Vote passed! Repeat mode: **{mode_text}** ({vote_result['votes']}/{vote_result['needed']} votes)",
                    ephemeral=False,
                )
            else:
                mode_text = {"off": "disable", "track": "track", "queue": "queue"}[
                    next_mode
                ]
                await interaction.response.send_message(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to set repeat to {mode_text}. ({vote_result['votes']}/{vote_result['needed']})",
                    ephemeral=True,
                )

    async def toggle_autoplay(self, interaction: discord.Interaction):
        """Handle autoplay toggle with voting."""
        player = self._get_player()
        if not player:
            await interaction.response.send_message(
                "❌ No active player.", ephemeral=True
            )
            return

        # Check if user is in voice channel
        if (
            not interaction.user.voice
            or interaction.user.voice.channel.id != player.channel.id
        ):
            await interaction.response.send_message(
                "❌ You must be in the voice channel!", ephemeral=True
            )
            return

        # Check if DJ bypass
        is_dj = await self.player_cog._check_dj(interaction.user, interaction.guild)

        current = self.player_cog._autoplay_enabled.get(self.guild_id, False)
        new_state = not current

        if is_dj:
            # DJ can bypass voting
            self.player_cog._autoplay_enabled[self.guild_id] = new_state
            state_text = "enabled" if new_state else "disabled"
            await interaction.response.send_message(
                f"🎲 AutoPlay **{state_text}**", ephemeral=False
            )
        else:
            # Require voting
            action = "autoplay_on" if new_state else "autoplay_off"
            vote_result = await self.player_cog.handle_vote_action(
                interaction.guild.id, interaction.user.id, action, player.channel
            )

            if vote_result["passed"]:
                self.player_cog._autoplay_enabled[self.guild_id] = new_state
                state_text = "enabled" if new_state else "disabled"
                await interaction.response.send_message(
                    f"🎲 Vote passed! AutoPlay **{state_text}** ({vote_result['votes']}/{vote_result['needed']} votes)",
                    ephemeral=False,
                )
            else:
                state_text = "enable" if new_state else "disable"
                await interaction.response.send_message(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to {state_text} autoplay. ({vote_result['votes']}/{vote_result['needed']})",
                    ephemeral=True,
                )


def create_now_playing_embed(
    track: "pomice.Track",
    guild: discord.Guild,
    current_entry: dict,
    player: "pomice.Player",
) -> discord.Embed:
    """
    Create a unified Now Playing embed.

    This function centralizes the embed creation logic used by both:
    - Auto-announce when track starts (_announce_now_playing)
    - Manual nowplaying command

    Args:
        track: The Pomice track object
        guild: Discord guild
        current_entry: Dict with requester info
        player: Pomice player instance

    Returns:
        Discord embed with track info
    """
    title = getattr(track, "title", "Unknown")
    author = getattr(track, "author", "Unknown")
    uri = getattr(track, "uri", None)

    # Create clickable title if URI exists
    if uri:
        embed = discord.Embed(
            title=f"🎵 {title}",
            url=uri,
            color=discord.Color.blurple(),
        )
    else:
        embed = discord.Embed(
            title=f"🎵 {title}",
            color=discord.Color.blurple(),
        )

    # Add thumbnail from artwork
    artwork_url = getattr(track, "artwork_url", None)
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

    # Create visual seekbar with duration
    dur = getattr(track, "length", None)
    if dur:
        total_seconds = int(dur / 1000)
        minutes, seconds = divmod(total_seconds, 60)
        duration_str = f"{minutes:02d}:{seconds:02d}"

        # Visual seekbar (at start of track)
        seekbar_length = 16
        seekbar = "▰" + "▱" * (seekbar_length - 1)

        embed.add_field(
            name="Duration",
            value=f"`00:00` {seekbar} `{duration_str}`",
            inline=False,
        )
    else:
        duration_str = "Live"
        embed.add_field(name="Duration", value="🔴 Live Stream", inline=False)

    # Add requester footer
    requester_id = current_entry.get("requester")
    if requester_id:
        try:
            requester = guild.get_member(requester_id)
            if requester:
                embed.set_footer(
                    text=f"Requested by {requester.display_name}",
                    icon_url=requester.display_avatar.url,
                )
            else:
                embed.set_footer(text=f"Requested by User#{requester_id}")
        except Exception:
            embed.set_footer(text=f"Requested by <@{requester_id}>")

    # Add voice channel info
    voice_channel = getattr(player, "channel", None)
    channel_name = getattr(voice_channel, "name", "Unknown channel")
    embed.add_field(
        name="Voice Channel",
        value=channel_name,
        inline=False,
    )

    return embed
