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
        name="volume", aliases=["vol"], description="Set playback volume (0-200)."
    )
    async def volume(self, ctx: commands.Context, value: int = None):
        """Set volume with voting system. Range: 0-200"""
        player = self._get_player()
        if not player:
            await ctx.send(":x: Player backend not available.")
            return

        vc = ctx.guild.voice_client
        if not vc:
            await ctx.send(":x: Not connected to voice channel.")
            return

        if value is None:
            await ctx.send(f":speaker: Current volume is {vc.volume}%")
            return

        if value < 0 or value > 200:
            await ctx.send(":x: Volume must be between 0 and 200.")
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
        description="Enable, disable, or view status of AutoPlay.",
    )
    @discord.app_commands.describe(state="Enable, disable, or view status of AutoPlay")
    @discord.app_commands.choices(
        state=[
            discord.app_commands.Choice(name="Enable", value="enable"),
            discord.app_commands.Choice(name="Disable", value="disable"),
            discord.app_commands.Choice(name="Status", value="status"),
        ]
    )
    async def autoplay(
        self,
        ctx: commands.Context,
        state: str = None,
    ):
        """Toggle AutoPlay or view status."""
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

        # Handle status command
        if state == "status":
            await self._show_autoplay_status(ctx, player)
            return

        # Validate state for enable/disable
        if state not in ["enable", "disable"]:
            await ctx.send(":x: State must be: `enable`, `disable`, or `status`")
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

    async def _show_autoplay_status(self, ctx: commands.Context, player):
        """Display detailed autoplay recommendation system status."""
        # Get LastFM autoplay instance
        lastfm_autoplay = getattr(player, "_lastfm_autoplay", None)
        if not lastfm_autoplay or not lastfm_autoplay.is_available():
            await ctx.send(":x: AutoPlay system not available.")
            return

        guild_id = ctx.guild.id

        # Check if autoplay is enabled
        autoplay_enabled = player.is_session_autoplay_enabled(guild_id)

        # Get exploration state
        explore_state = lastfm_autoplay._get_exploration_state(guild_id)
        epsilon = float(explore_state.get("epsilon", 0.08))
        autoplay_count = int(explore_state.get("autoplay_count", 0))
        consecutive_skips = explore_state.get("consecutive_genre_skips", {})

        # Get progressive diversity factor
        diversity_factor = lastfm_autoplay._compute_progressive_diversity_factor(
            guild_id
        )

        # Get genre sentiment data
        sentiment_profile = lastfm_autoplay._session_genre_sentiment.get(guild_id)
        liked_genres = []
        disliked_genres = []

        if sentiment_profile:
            lastfm_autoplay._decay_session_genre_sentiment(guild_id)
            tags_map = sentiment_profile.get("tags", {})

            for tag, entry in tags_map.items():
                pos = float(entry.get("pos", 0.0))
                neg = float(entry.get("neg", 0.0))
                total = pos + neg
                if total <= 0.0:
                    continue
                sentiment = (pos - neg) / total

                if sentiment > 0.2:
                    liked_genres.append((tag, sentiment))
                elif sentiment < -0.2:
                    disliked_genres.append((tag, sentiment))

        # Sort by sentiment strength
        liked_genres.sort(key=lambda x: x[1], reverse=True)
        disliked_genres.sort(key=lambda x: x[1])

        # Get genre history for recent trends
        genre_history = lastfm_autoplay._genre_history.get(guild_id, [])
        recent_genres = (
            genre_history[-12:] if len(genre_history) >= 12 else genre_history
        )

        # Calculate genre dominance
        dominant_genre = None
        dominant_ratio = 0.0
        if recent_genres:
            from collections import Counter

            genre_counts = Counter(recent_genres)
            most_common = genre_counts.most_common(1)[0]
            dominant_genre = most_common[0]
            dominant_ratio = most_common[1] / len(recent_genres)

        # Get disliked tag info
        disliked_tag, disliked_sentiment = lastfm_autoplay._get_session_disliked_tag(
            guild_id
        )

        # Build embed
        embed = discord.Embed(
            title="🎲 AutoPlay Status",
            description=f"**Status:** {'✅ Enabled' if autoplay_enabled else '❌ Disabled'}",
            color=(
                discord.Color.blue() if autoplay_enabled else discord.Color.grayed_out()
            ),
        )

        # Diversity metrics
        effective_epsilon = epsilon * diversity_factor
        diversity_info = f"**Base Epsilon:** `{epsilon:.3f}`\n"
        diversity_info += f"**Progressive Multiplier:** `{diversity_factor:.2f}x`\n"
        diversity_info += f"**Effective Diversity:** `{effective_epsilon:.3f}`\n"
        diversity_info += f"**Autoplay Count:** `{autoplay_count}` tracks"

        if autoplay_count < 10:
            diversity_info += " (similarity phase)"
        elif autoplay_count < 18:
            diversity_info += " (ramping diversity)"
        else:
            diversity_info += " (full diversity)"

        embed.add_field(name="📊 Diversity Factor", value=diversity_info, inline=False)

        # Liked genres
        if liked_genres:
            liked_text = "\n".join(
                [
                    f"• **{tag}** ({sentiment:+.2f})"
                    for tag, sentiment in liked_genres[:5]
                ]
            )
        else:
            liked_text = "*No preference data yet*"
        embed.add_field(name="💚 Preferred Genres", value=liked_text, inline=True)

        # Disliked genres
        if disliked_genres:
            disliked_text = "\n".join(
                [
                    f"• **{tag}** ({sentiment:+.2f})"
                    for tag, sentiment in disliked_genres[:5]
                ]
            )
        else:
            disliked_text = "*No dislikes detected*"
        embed.add_field(name="💔 Disliked Genres", value=disliked_text, inline=True)

        # Consecutive skip tracking
        if consecutive_skips:
            skip_text = "\n".join(
                [
                    f"• **{genre}**: `{count}` consecutive skips"
                    for genre, count in sorted(
                        consecutive_skips.items(), key=lambda x: x[1], reverse=True
                    )[:3]
                ]
            )
            embed.add_field(name="⏭️ Skip Patterns", value=skip_text, inline=False)

        # Next candidate pool requirements
        requirements = "**Next Recommendation Strategy:**\n"

        if effective_epsilon < 0.10:
            requirements += "🎯 **High Similarity** - Sticking close to current vibe\n"
        elif effective_epsilon < 0.20:
            requirements += (
                "🔀 **Balanced Mix** - Similar tracks with occasional variety\n"
            )
        else:
            requirements += "🌈 **High Diversity** - Exploring new territory\n"

        if dominant_genre and dominant_ratio > 0.55:
            requirements += f"⚠️ **Genre Dominance Alert:** *{dominant_genre}* ({dominant_ratio:.0%})\n"
            requirements += "→ Boosting diversity to break repetition\n"

        if disliked_tag and disliked_sentiment < -0.35:
            requirements += f"🚫 **Avoiding:** *{disliked_tag}* (sentiment: {disliked_sentiment:.2f})\n"

        if consecutive_skips:
            escape_genres = [g for g, c in consecutive_skips.items() if c >= 4]
            if escape_genres:
                requirements += f"🔴 **Escape Mode:** Heavily penalizing {', '.join(escape_genres)}\n"

        if liked_genres:
            top_liked = ", ".join([tag for tag, _ in liked_genres[:3]])
            requirements += f"✨ **Boosting:** {top_liked}\n"

        embed.add_field(
            name="🎯 Recommendation Profile", value=requirements, inline=False
        )

        # Footer
        embed.set_footer(
            text=f"Session data decays over 45 minutes • {len(genre_history)} tracks in history"
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ControlCommands(bot))
