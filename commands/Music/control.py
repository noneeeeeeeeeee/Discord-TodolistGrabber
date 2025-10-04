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
        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        vc = ctx.guild.voice_client
        if not vc or not is_voice_playing(vc):
            await ctx.send(":x: Nothing is playing.")
            return

        # Check if user is in voice channel
        if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
            await ctx.send(":x: You must be in the voice channel!")
            return

        # Check DJ permissions
        is_dj = await player._check_dj(ctx.author, ctx.guild)

        if is_dj:
            # DJ bypass
            await vc.set_pause(True)
            await ctx.send("⏸️ Paused playback.")
        else:
            # Voting required
            vote_result = await player.handle_vote_action(
                ctx.guild.id, ctx.author.id, "pause", vc.channel
            )

            if vote_result["passed"]:
                await vc.set_pause(True)
                await ctx.send(
                    f"⏸️ Vote passed! Paused playback. ({vote_result['votes']}/{vote_result['needed']} votes)"
                )
            else:
                await ctx.send(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to pause. "
                    f"({vote_result['votes']}/{vote_result['needed']})"
                )

    @commands.hybrid_command(
        name="resume", description="Resume playback (requires voting or DJ)."
    )
    async def resume(self, ctx: commands.Context):
        """Resume playback with voting system."""
        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        vc = ctx.guild.voice_client
        if not vc or not is_voice_paused(vc):
            await ctx.send(":x: Nothing to resume.")
            return

        # Check if user is in voice channel
        if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
            await ctx.send(":x: You must be in the voice channel!")
            return

        # Check DJ permissions
        is_dj = await player._check_dj(ctx.author, ctx.guild)

        if is_dj:
            # DJ bypass
            await vc.set_pause(False)
            await ctx.send("▶️ Resumed playback.")
        else:
            # Voting required
            vote_result = await player.handle_vote_action(
                ctx.guild.id, ctx.author.id, "resume", vc.channel
            )

            if vote_result["passed"]:
                await vc.set_pause(False)
                await ctx.send(
                    f"▶️ Vote passed! Resumed playback. ({vote_result['votes']}/{vote_result['needed']} votes)"
                )
            else:
                await ctx.send(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to resume. "
                    f"({vote_result['votes']}/{vote_result['needed']})"
                )

    @commands.hybrid_command(
        name="stop", description="Stop and clear queue (DJ/Admin)."
    )
    async def stop(self, ctx: commands.Context):
        # permission check
        if not (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_guild
        ):
            await ctx.send(":x: Admin/DJ required.")
            return
        vc = ctx.guild.voice_client
        if vc:
            await vc.stop()
        player = self._get_player()
        player.queues[ctx.guild.id].clear()
        await ctx.send("Stopped and cleared queue.")

    @commands.hybrid_command(
        name="disconnect",
        aliases=["dc"],
        description="Disconnect the bot from the voice channel.",
    )
    async def disconnect(self, ctx: commands.Context):
        if not (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_guild
        ):
            await ctx.send(":x: Admin/DJ required.")
            return

        vc = ctx.guild.voice_client
        if not vc or not is_voice_connected(vc):
            await ctx.send("Bot is not connected to a voice channel.")
            return

        try:
            await vc.disconnect()
        except Exception as exc:
            await ctx.send(f":x: Failed to disconnect: {exc}")
            return

        player = self._get_player()
        if player:
            try:
                player.reset_session_state(ctx.guild.id)
            except Exception:
                pass
            await player._cancel_idle(ctx.guild.id)
            if await player.send_disconnect_message(
                ctx.guild, "bot_disconnected_by_user", channel=ctx.channel
            ):
                return
        await ctx.send(":wave: Disconnected from voice channel.")

    @commands.hybrid_command(
        name="volume", aliases=["vol"], description="Set default volume (0-200)."
    )
    async def volume(self, ctx: commands.Context, value: int):
        if value < 0 or value > 200:
            await ctx.send(":x: Volume must be 0-200.")
            return
        try:
            from modules.setconfig import edit_json_file

            edit_json_file(
                ctx.guild.id,
                "Music.Volume",
                float(value) / 100.0,
                actor_user_id=ctx.author.id,
            )
            await ctx.send(f"Default volume set to {value}%.")
        except Exception as e:
            await ctx.send(f":x: Failed: {e}")

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
        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        # If no mode specified, show current mode
        if mode is None:
            current = player.repeat_mode.get(ctx.guild.id, "off")
            mode_text = {"off": "disabled", "track": "track", "queue": "queue"}[current]
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

        # Check if user is in voice channel
        if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
            await ctx.send(":x: You must be in the voice channel!")
            return

        # Check DJ permissions
        is_dj = await player._check_dj(ctx.author, ctx.guild)

        if is_dj:
            # DJ bypass
            player.repeat_mode[ctx.guild.id] = mode
            mode_text = {"off": "disabled", "track": "track", "queue": "queue"}[mode]
            await ctx.send(f"🔁 Repeat mode: **{mode_text}**")
        else:
            # Voting required
            vote_result = await player.handle_vote_action(
                ctx.guild.id, ctx.author.id, f"repeat_{mode}", vc.channel
            )

            if vote_result["passed"]:
                player.repeat_mode[ctx.guild.id] = mode
                mode_text = {"off": "disabled", "track": "track", "queue": "queue"}[
                    mode
                ]
                await ctx.send(
                    f"🔁 Vote passed! Repeat mode: **{mode_text}** "
                    f"({vote_result['votes']}/{vote_result['needed']} votes)"
                )
            else:
                mode_text = {"off": "disable", "track": "track", "queue": "queue"}[mode]
                await ctx.send(
                    f"🗳️ Vote registered. Need {vote_result['needed']} votes to set repeat to {mode_text}. "
                    f"({vote_result['votes']}/{vote_result['needed']})"
                )

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
