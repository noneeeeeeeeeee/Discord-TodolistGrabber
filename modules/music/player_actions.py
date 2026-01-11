"""
Shared music player actions for unified command and button handling.
This module provides common functions used by both slash commands and player button interactions.
"""

import discord
from typing import Optional, Dict, Any
import logging

LOG = logging.getLogger(__name__)


async def _save_volume_to_config(player, guild_id: int, volume: int):
    """
    Save volume to config if RememberLastVolume is enabled.

    Args:
        player: MusicPlayer cog instance
        guild_id: Guild ID
        volume: Volume level (0-200 range)
    """
    try:
        # Get music config
        cfg = player._get_music_config(guild_id)
        remember_volume = cfg.get("RememberLastVolume", False)

        if remember_volume:
            # Convert from 0-200 range to 0.0-2.0 range for config
            volume_config = volume / 100.0

            # Import here to avoid circular imports
            from modules.setconfig import edit_json_file

            # Save to config (access level 2 = hidden, no permissions needed)
            edit_json_file(
                guild_id,
                "Music.RememberLastVolumeBetweenSessions",
                volume_config,
                actor_user_id=None,  # System action, no user needed
            )
            LOG.debug(
                f"Saved volume {volume}% ({volume_config}) to config for guild {guild_id}"
            )
    except Exception as e:
        LOG.warning(f"Failed to save volume to config for guild {guild_id}: {e}")


async def handle_pause_action(
    player,
    guild: discord.Guild,
    user: discord.Member,
    vc,
    announce_channel: Optional[discord.TextChannel] = None,
) -> Dict[str, Any]:
    """
    Handle pause action with DJ check and voting.

    Returns dict with:
    - success: bool
    - message: str (announcement message)
    - ephemeral: bool (whether message should be ephemeral)
    """
    # Check DJ permissions
    is_dj = await player._check_dj(user, guild)

    if is_dj:
        await vc.set_pause(True)
        return {
            "success": True,
            "message": f"⏸️ **{user.display_name}** paused the player. `/resume` to continue.",
            "ephemeral": False,
        }
    else:
        vote_result = await player.handle_vote_action(
            guild.id, user.id, "pause", vc.channel
        )

        if vote_result.get("already_voted"):
            return {
                "success": False,
                "message": "You already voted to pause.",
                "ephemeral": True,
            }

        if vote_result["passed"]:
            await vc.set_pause(True)
            remaining = 0
            return {
                "success": True,
                "message": "",  # Will be replaced with embed
                "embed": discord.Embed(
                    title="Pause Vote",
                    description=f":white_check_mark: **Threshold reached** – {user.display_name} paused the player!",
                    color=discord.Color.green(),
                ),
                "ephemeral": False,
            }
        else:
            remaining = vote_result["needed"] - vote_result["votes"]
            return {
                "success": False,
                "message": f"**{user.display_name}** wants to pause the player",
                "embed": discord.Embed(
                    title="Pause Vote",
                    description=f"**Votes:** {vote_result['votes']}/{vote_result['needed']}\n**Remaining:** {remaining} more vote(s) needed",
                    color=discord.Color.orange(),
                ),
                "ephemeral": True,
            }


async def handle_resume_action(
    player,
    guild: discord.Guild,
    user: discord.Member,
    vc,
    announce_channel: Optional[discord.TextChannel] = None,
) -> Dict[str, Any]:
    """
    Handle resume action with DJ check and voting.

    Returns dict with:
    - success: bool
    - message: str (announcement message)
    - ephemeral: bool (whether message should be ephemeral)
    """
    # Check DJ permissions
    is_dj = await player._check_dj(user, guild)

    if is_dj:
        await vc.set_pause(False)
        return {
            "success": True,
            "message": f"▶️ **{user.display_name}** resumed the player. `/pause` to pause.",
            "ephemeral": False,
        }
    else:
        vote_result = await player.handle_vote_action(
            guild.id, user.id, "resume", vc.channel
        )

        if vote_result.get("already_voted"):
            return {
                "success": False,
                "message": "You already voted to resume.",
                "ephemeral": True,
            }

        if vote_result["passed"]:
            await vc.set_pause(False)
            return {
                "success": True,
                "message": "",  # Will be replaced with embed
                "embed": discord.Embed(
                    title="Resume Vote",
                    description=f":white_check_mark: **Threshold reached** – {user.display_name} resumed the player!",
                    color=discord.Color.green(),
                ),
                "ephemeral": False,
            }
        else:
            remaining = vote_result["needed"] - vote_result["votes"]
            return {
                "success": False,
                "message": f"**{user.display_name}** wants to resume the player",
                "embed": discord.Embed(
                    title="Resume Vote",
                    description=f"**Votes:** {vote_result['votes']}/{vote_result['needed']}\n**Remaining:** {remaining} more vote(s) needed",
                    color=discord.Color.orange(),
                ),
                "ephemeral": True,
            }


async def handle_skip_action(
    player,
    guild: discord.Guild,
    user: discord.Member,
    vc,
    announce_channel: Optional[discord.TextChannel] = None,
) -> Dict[str, Any]:
    """
    Handle skip action with DJ check and voting.

    Returns dict with:
    - success: bool
    - message: str (announcement message)
    - ephemeral: bool (whether message should be ephemeral)
    """
    # Check DJ permissions
    is_dj = await player._check_dj(user, guild)

    if is_dj:
        try:
            await player.note_skip(vc, guild.id, user.id, primary_listener_bias=True)
            await vc.stop()
            return {
                "success": True,
                "message": f":fast_forward: **{user.display_name}** skipped the track",
                "ephemeral": False,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"❌ Failed to skip: {e}",
                "ephemeral": True,
            }
    else:
        vote_result = await player.handle_vote_action(
            guild.id, user.id, "skip", vc.channel
        )

        if vote_result["passed"]:
            try:
                await player.note_skip(
                    vc, guild.id, user.id, primary_listener_bias=False
                )
                await vc.stop()
                return {
                    "success": True,
                    "message": f":fast_forward: **{user.display_name}** skipped the track ({vote_result['votes']}/{vote_result['needed']} votes)",
                    "ephemeral": False,
                }
            except Exception as e:
                return {
                    "success": False,
                    "message": f"❌ Failed to skip: {e}",
                    "ephemeral": True,
                }
        else:
            return {
                "success": False,
                "message": f"🗳️ **{user.display_name}** voted to skip ({vote_result['votes']}/{vote_result['needed']} needed)",
                "ephemeral": True,
            }


async def handle_repeat_action(
    player,
    guild: discord.Guild,
    user: discord.Member,
    mode: str,
    announce_channel: Optional[discord.TextChannel] = None,
) -> Dict[str, Any]:
    """
    Handle repeat mode change with DJ check and voting.

    Args:
        mode: "off" or "track"

    Returns dict with:
    - success: bool
    - message: str (announcement message)
    - ephemeral: bool (whether message should be ephemeral)
    """
    # Check DJ permissions
    is_dj = await player._check_dj(user, guild)

    mode_text = "On" if mode == "track" else "Off"

    if is_dj:
        player.repeat_mode[guild.id] = mode
        return {
            "success": True,
            "message": f"🔁 **{user.display_name}** set repeat to **{mode_text}**",
            "ephemeral": False,
        }
    else:
        vote_result = await player.handle_vote_action(
            guild.id, user.id, f"repeat_{mode}", None
        )

        if vote_result.get("already_voted"):
            return {
                "success": False,
                "message": f"You already voted to set repeat to {mode_text}.",
                "ephemeral": True,
            }

        if vote_result["passed"]:
            player.repeat_mode[guild.id] = mode
            return {
                "success": True,
                "message": "",  # Will be replaced with embed
                "embed": discord.Embed(
                    title="Repeat Vote",
                    description=f":white_check_mark: **Threshold reached** – {user.display_name} set repeat to **{mode_text}**!",
                    color=discord.Color.green(),
                ),
                "ephemeral": False,
            }
        else:
            remaining = vote_result["needed"] - vote_result["votes"]
            return {
                "success": False,
                "message": f"**{user.display_name}** wants to set repeat to **{mode_text}**",
                "embed": discord.Embed(
                    title="Repeat Vote",
                    description=f"**Votes:** {vote_result['votes']}/{vote_result['needed']}\n**Remaining:** {remaining} more vote(s) needed",
                    color=discord.Color.orange(),
                ),
                "ephemeral": True,
            }


async def handle_volume_action(
    player,
    guild: discord.Guild,
    user: discord.Member,
    vc,
    volume: int,
    announce_channel: Optional[discord.TextChannel] = None,
) -> Dict[str, Any]:
    """
    Handle volume change with DJ check and voting.

    Args:
        volume: Volume level (0-200)

    Returns dict with:
    - success: bool
    - message: str (announcement message)
    - ephemeral: bool (whether message should be ephemeral)
    """
    # Check DJ permissions
    is_dj = await player._check_dj(user, guild)

    if is_dj:
        try:
            await vc.set_volume(volume)
            # Save volume to config if RememberLastVolume is enabled
            await _save_volume_to_config(player, guild.id, volume)
            return {
                "success": True,
                "message": f"🔊 **{user.display_name}** set volume to {volume}%",
                "ephemeral": False,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"❌ Failed to set volume: {e}",
                "ephemeral": True,
            }
    else:
        vote_result = await player.handle_vote_action(
            guild.id, user.id, f"volume_{volume}", vc.channel
        )

        if vote_result["passed"]:
            try:
                await vc.set_volume(volume)
                # Save volume to config if RememberLastVolume is enabled
                await _save_volume_to_config(player, guild.id, volume)
                return {
                    "success": True,
                    "message": f"🔊 **{user.display_name}** set volume to {volume}% ({vote_result['votes']}/{vote_result['needed']} votes)",
                    "ephemeral": False,
                }
            except Exception as e:
                return {
                    "success": False,
                    "message": f"❌ Failed to set volume: {e}",
                    "ephemeral": True,
                }
        else:
            return {
                "success": False,
                "message": f"🗳️ **{user.display_name}** voted to set volume to {volume}% ({vote_result['votes']}/{vote_result['needed']} needed)",
                "ephemeral": True,
            }


async def handle_seek_action(
    player,
    guild: discord.Guild,
    user: discord.Member,
    vc,
    position_ms: int,
    time_str: str,
    announce_channel: Optional[discord.TextChannel] = None,
) -> Dict[str, Any]:
    """
    Handle seek action with DJ check and voting.

    Args:
        position_ms: Position in milliseconds
        time_str: Formatted time string (e.g., "1:30")

    Returns dict with:
    - success: bool
    - message: str (announcement message)
    - ephemeral: bool (whether message should be ephemeral)
    """
    # Check DJ permissions
    is_dj = await player._check_dj(user, guild)

    if is_dj:
        try:
            await vc.seek(position_ms)
            player.note_seek(guild.id, position_ms)
            return {
                "success": True,
                "message": f"⏩ **{user.display_name}** seeked to {time_str}",
                "ephemeral": False,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"❌ Failed to seek: {e}",
                "ephemeral": True,
            }
    else:
        vote_result = await player.handle_vote_action(
            guild.id, user.id, f"seek_{time_str}", vc.channel
        )

        if vote_result["passed"]:
            try:
                await vc.seek(position_ms)
                player.note_seek(guild.id, position_ms)
                return {
                    "success": True,
                    "message": f"⏩ **{user.display_name}** seeked to {time_str} ({vote_result['votes']}/{vote_result['needed']} votes)",
                    "ephemeral": False,
                }
            except Exception as e:
                return {
                    "success": False,
                    "message": f"❌ Failed to seek: {e}",
                    "ephemeral": True,
                }
        else:
            return {
                "success": False,
                "message": f"🗳️ **{user.display_name}** voted to seek to {time_str} ({vote_result['votes']}/{vote_result['needed']} needed)",
                "ephemeral": True,
            }


async def handle_autoplay_action(
    player,
    guild: discord.Guild,
    user: discord.Member,
    enable: bool,
    announce_channel: Optional[discord.TextChannel] = None,
) -> Dict[str, Any]:
    """
    Handle autoplay toggle (no voting - instant action).

    Args:
        enable: True to enable, False to disable

    Returns dict with:
    - success: bool
    - message: str (announcement message)
    - ephemeral: bool (whether message should be ephemeral)
    """
    player.set_session_autoplay(guild.id, enable)
    status = "enabled" if enable else "disabled"
    return {
        "success": True,
        "message": f"🤖 **{user.display_name}** {status} AutoPlay",
        "ephemeral": False,
    }


# Voice channel validation helpers
def validate_user_in_voice(user: discord.Member) -> Optional[str]:
    """Check if user is in a voice channel. Returns error message if not, None if valid."""
    if not user.voice or not user.voice.channel:
        return "❌ You must be in a voice channel!"
    return None


def validate_same_voice_channel(user: discord.Member, vc) -> Optional[str]:
    """Check if user is in same voice channel as bot. Returns error message if not, None if valid."""
    if not vc:
        return "❌ Bot is not connected to a voice channel."
    if not user.voice or user.voice.channel.id != vc.channel.id:
        return "❌ You must be in the same voice channel as the bot!"
    return None


def validate_playing(vc) -> Optional[str]:
    """Check if something is playing. Returns error message if not, None if valid."""
    if not vc or not vc.is_playing:
        return "❌ Nothing is currently playing."
    return None


def validate_paused(vc) -> Optional[str]:
    """Check if playback is paused. Returns error message if not, None if valid."""
    if not vc or not vc.is_paused:
        return "❌ Nothing is paused."
    return None
