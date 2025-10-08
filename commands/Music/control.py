import discord
from discord.ext import commands

from modules.music.music_player import (
    is_voice_connected,
    is_voice_paused,
    is_voice_playing,
)


class ControlCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _get_player(self):
        return self.bot.get_cog("MusicPlayer")

    @commands.hybrid_command(
        name="pause", description="Pause playback (requires voting or DJ)."
    )
    async def pause(self, ctx: commands.Context):
        """Pause playback with voting system."""
        from modules.music.player_actions import (
            handle_pause_action,
            validate_user_in_voice,
            validate_same_voice_channel,
            validate_playing,
        )

        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        vc = ctx.guild.voice_client

        # Validation checks
        error = validate_user_in_voice(ctx.author)
        if error:
            await ctx.send(error)
            return

        error = validate_same_voice_channel(ctx.author, vc)
        if error:
            await ctx.send(error)
            return

        error = validate_playing(vc)
        if error:
            await ctx.send(error)
            return

        result = await handle_pause_action(
            player, ctx.guild, ctx.author, vc, ctx.channel
        )
        await ctx.send(result["message"])

    @commands.hybrid_command(
        name="resume", description="Resume playback (requires voting or DJ)."
    )
    async def resume(self, ctx: commands.Context):
        """Resume playback with voting system."""
        from modules.music.player_actions import (
            handle_resume_action,
            validate_user_in_voice,
            validate_same_voice_channel,
            validate_paused,
        )

        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        vc = ctx.guild.voice_client

        # Validation checks
        error = validate_user_in_voice(ctx.author)
        if error:
            await ctx.send(error)
            return

        error = validate_same_voice_channel(ctx.author, vc)
        if error:
            await ctx.send(error)
            return

        error = validate_paused(vc)
        if error:
            await ctx.send(error)
            return

        # Handle resume action
        result = await handle_resume_action(
            player, ctx.guild, ctx.author, vc, ctx.channel
        )
        await ctx.send(result["message"])

    @commands.hybrid_command(
        name="disconnect",
        aliases=["dc"],
        description="Disconnect the bot from the voice channel.",
    )
    async def disconnect(self, ctx: commands.Context):
        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        # Check DJ mode permissions
        permission = await player._check_dj_mode_permission(
            ctx.author, ctx.guild, "disconnect"
        )

        if not permission["allowed"]:
            await ctx.send(permission["error"])
            return

        if permission["needs_vote"]:
            vc = ctx.guild.voice_client
            if not vc:
                await ctx.send("❌ Bot is not connected to a voice channel.")
                return

            if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
                await ctx.send("❌ You must be in the same voice channel!")
                return

            vote_result = await player.handle_vote_action(
                ctx.guild.id, ctx.author.id, "disconnect", vc.channel
            )

            if not vote_result["passed"]:
                await ctx.send(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to disconnect. "
                    f"({vote_result['votes']}/{vote_result['needed']})"
                )
                return

        vc = ctx.guild.voice_client
        if not vc or not is_voice_connected(vc):
            await ctx.send("❌ Bot is not connected to a voice channel.")
            return

        try:
            await vc.disconnect()
        except Exception as exc:
            await ctx.send(f":x: Failed to disconnect: {exc}")
            return

        if player:
            try:
                # Clean up state (on_voice_state_update will also handle this)
                player._playing_flags[ctx.guild.id] = False
                player.queues[ctx.guild.id].clear()
                player.reset_session_state(ctx.guild.id)
                lastfm_autoplay = getattr(player, "_lastfm_autoplay", None)
                if lastfm_autoplay:
                    lastfm_autoplay.clear_history(ctx.guild.id)
            except Exception:
                pass
            await player._cancel_idle(ctx.guild.id)
            if await player.send_disconnect_message(
                ctx.guild, "bot_disconnected_by_user", channel=ctx.channel
            ):
                return
        await ctx.send(":wave: Disconnected from voice channel.")

    @commands.hybrid_command(
        name="volume", aliases=["vol"], description="Set playback volume ."
    )
    async def volume(self, ctx: commands.Context, value: int):
        """Set volume with voting system. Range: 0-200"""
        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return
        if value is None:
            await ctx.send(
                f":speaker: Current volume is {ctx.guild.voice_client.volume}%"
            )

        if value < 0 or value > 200:
            await ctx.send(":x: Volume must be between 0 and 200.")
            return

        vc = ctx.guild.voice_client
        if not vc:
            await ctx.send(":x: Not connected to voice channel.")
            return

        if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
            await ctx.send(":x: You must be in the same voice channel!")
            return

        # Check DJ mode permissions
        permission = await player._check_dj_mode_permission(
            ctx.author, ctx.guild, "volume"
        )

        if not permission["allowed"]:
            await ctx.send(permission["error"])
            return

        if permission["needs_vote"]:
            vote_result = await player.handle_vote_action(
                ctx.guild.id, ctx.author.id, f"volume_{value}", vc.channel
            )

            if not vote_result["passed"]:
                await ctx.send(
                    f"🗳️ **{ctx.author.display_name}** voted to set volume to {value}% "
                    f"({vote_result['votes']}/{vote_result['needed']} needed)"
                )
                return

        # Set volume (Pomice uses 0-1000 range, we cap at 200 for safety)
        try:
            await vc.set_volume(value)

            # Save volume to config if RememberLastVolume is enabled
            from modules.music.player_actions import _save_volume_to_config

            await _save_volume_to_config(player, ctx.guild.id, value)

            # Announce publicly - include vote count if vote passed
            if permission.get("needs_vote") and vote_result.get("passed"):
                await ctx.send(
                    f"🔊 **{ctx.author.display_name}** set volume to {value}% ({vote_result['votes']}/{vote_result['needed']} votes)"
                )
            else:
                await ctx.send(
                    f"🔊 **{ctx.author.display_name}** set volume to {value}%"
                )
        except Exception as e:
            await ctx.send(f":x: Failed to set volume: {e}")

    @commands.hybrid_command(
        name="repeat", description="Set repeat mode (requires voting or DJ)."
    )
    @discord.app_commands.describe(mode="Repeat mode: off, track, or queue")
    @discord.app_commands.choices(
        mode=[
            discord.app_commands.Choice(name="Off", value="off"),
            discord.app_commands.Choice(name="Track", value="track"),
            discord.app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def repeat(self, ctx: commands.Context, mode: str = None):
        """Set repeat mode with voting system."""
        from modules.music.player_actions import (
            handle_repeat_action,
            validate_user_in_voice,
            validate_same_voice_channel,
        )

        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        # If no mode specified, show current mode
        if mode is None:
            current = player.repeat_mode.get(ctx.guild.id, "off")
            mode_text = {"off": "Off", "track": "Track", "queue": "Queue"}[current]
            await ctx.send(f"🔁 Current repeat mode: **{mode_text}**")
            return

        # Validate mode
        if mode not in ["off", "track", "queue"]:
            await ctx.send(":x: Mode must be: `off`, `track`, or `queue`")
            return

        vc = ctx.guild.voice_client
        if not vc:
            await ctx.send(":x: Not connected to voice channel.")
            return

        # Validation checks
        error = validate_user_in_voice(ctx.author)
        if error:
            await ctx.send(error)
            return

        error = validate_same_voice_channel(ctx.author, vc)
        if error:
            await ctx.send(error)
            return

        # Handle repeat action using unified function
        result = await handle_repeat_action(player, ctx.guild, ctx.author, mode)
        await ctx.send(result["message"])

    @commands.hybrid_command(
        name="autoplay",
        aliases=["ap"],
        description="Enable or disable AutoPlay (requires voting or DJ).",
    )
    @discord.app_commands.describe(state="Enable or disable AutoPlay")
    @discord.app_commands.choices(
        state=[
            discord.app_commands.Choice(name="Enable", value="enable"),
            discord.app_commands.Choice(name="Disable", value="disable"),
        ]
    )
    async def autoplay(
        self,
        ctx: commands.Context,
        state: str = None,
    ):
        """Toggle AutoPlay with voting system."""
        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        # If no state specified, show current state
        if state is None:
            current = player.is_session_autoplay_enabled(ctx.guild.id)
            state_text = "enabled" if current else "disabled"
            await ctx.send(f"🎲 AutoPlay is currently **{state_text}**")
            return

        # Validate state
        if state not in ["enable", "disable"]:
            await ctx.send(":x: State must be: `enable` or `disable`")
            return

        vc = ctx.guild.voice_client
        if not vc:
            await ctx.send(":x: Not connected to voice channel.")
            return

        # Check if user is in voice channel
        if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
            await ctx.send(":x: You must be in the voice channel!")
            return

        # Check DJ permissions
        is_dj = await player._check_dj(ctx.author, ctx.guild)

        enabled = state == "enable"

        if is_dj:
            # DJ bypass
            player.set_session_autoplay(ctx.guild.id, enabled)
            state_text = "enabled" if enabled else "disabled"
            await ctx.send(f"🎲 AutoPlay **{state_text}**")
        else:
            # Voting required
            action = "autoplay_on" if enabled else "autoplay_off"
            vote_result = await player.handle_vote_action(
                ctx.guild.id, ctx.author.id, action, vc.channel
            )

            if vote_result["passed"]:
                player.set_session_autoplay(ctx.guild.id, enabled)
                state_text = "enabled" if enabled else "disabled"
                await ctx.send(
                    f"🎲 Vote passed! AutoPlay **{state_text}** "
                    f"({vote_result['votes']}/{vote_result['needed']} votes)"
                )
            else:
                state_text = "enable" if enabled else "disable"
                await ctx.send(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to {state_text} autoplay. "
                    f"({vote_result['votes']}/{vote_result['needed']})"
                )


async def setup(bot):
    await bot.add_cog(ControlCommands(bot))
