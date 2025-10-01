import re
import discord
from discord.ext import commands
from modules.setconfig import check_guild_config_available, json_get
from typing import Optional


class PlayCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _get_player(self):
        return self.bot.get_cog("MusicPlayer")

    @commands.hybrid_command(name="p", description="Play/search a track or playlist")
    async def p(self, ctx: commands.Context, *, query: str):
        if not check_guild_config_available(ctx.guild.id):
            await ctx.send("Server not setup. Please run !setup or /setup.")
            return
        player = self._get_player()
        if not player:
            await ctx.send("Player backend not available.")
            return
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()
        q = query.strip()
        if re.match(r"https?://(www\.)?youtu", q) or re.match(r"^[a-z]+search\d*:", q):
            source = q
        else:
            source = f"ytsearch:{q}"
        item = {"title": q, "requester": ctx.author.id, "source": source}

        # Check if currently playing before enqueueing
        was_playing = player.is_playback_active(ctx.guild.id)

        ok = await player.enqueue(ctx.guild, item)
        if not ok:
            reason = player.get_last_enqueue_error(ctx.guild.id)
            message = (
                f":x: {reason}"
                if reason
                else ":x: Unable to queue the track right now."
            )
            await ctx.send(message)
            return

        # If music was already playing, just show success message
        # The "Now Playing" embed will show via event listener when track actually starts
        if was_playing:
            await ctx.send(f":white_check_mark: Added **{q}** to queue")
        else:
            # Nothing was playing, so this will start playing now
            await ctx.send(f":white_check_mark: Now playing: **{q}**")

    @discord.app_commands.command(
        name="recommend",
        description="Suggest related tracks based on what you're playing.",
    )
    @discord.app_commands.describe(
        count="Number of recommendations (1-25)", mode="Display as list or add to queue"
    )
    @discord.app_commands.choices(
        mode=[
            discord.app_commands.Choice(name="List", value="list"),
            discord.app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def recommend(
        self,
        interaction: discord.Interaction,
        count: Optional[int] = None,
        mode: Optional[discord.app_commands.Choice[str]] = None,
    ):
        ctx = await commands.Context.from_interaction(interaction)
        player = self._get_player()
        if not player:
            await interaction.response.send_message(
                "Player backend not available.", ephemeral=True
            )
            return

        await interaction.response.defer()

        config = {}
        try:
            config = json_get(ctx.guild.id).get("Music", {})
        except Exception:
            config = {}

        default_mode = str(config.get("RecommendationMode", "list")).lower()
        if default_mode not in {"list", "queue"}:
            default_mode = "list"

        if mode is None:
            resolved_mode = default_mode
        else:
            resolved_mode = mode.value
            if resolved_mode not in {"list", "queue"}:
                resolved_mode = default_mode

        limit = player._get_recommendation_limit(ctx.guild.id)

        if count is None:
            default_count = config.get("RecommendationMaxResults", 5)
            try:
                count_val = int(default_count)
            except (TypeError, ValueError):
                count_val = 5
        else:
            try:
                count_val = int(count)
            except (TypeError, ValueError):
                count_val = 5

        count_val = max(1, min(limit, count_val))

        suggestions = await player.recommend(ctx.guild, max_rec=count_val)
        if not suggestions:
            await interaction.followup.send("No recommendations available right now.")
            return

        if resolved_mode == "queue":
            successes = 0
            for rec in suggestions:
                item = {
                    "title": rec.get("title"),
                    "requester": ctx.author.id,
                    "source": rec.get("url"),
                }
                ok = await player.enqueue(ctx.guild, item)
                if ok:
                    successes += 1

            if successes == 0:
                reason = player.get_last_enqueue_error(ctx.guild.id)
                msg = (
                    f":x: {reason}"
                    if reason
                    else ":x: Could not queue any recommended tracks."
                )
                await interaction.followup.send(msg)
                return

            failures = len(suggestions) - successes
            summary = f"Queued {successes} recommendation(s)."
            if failures:
                reason = player.get_last_enqueue_error(ctx.guild.id)
                if reason:
                    summary += f" {failures} failed ({reason})."
                else:
                    summary += f" {failures} failed to queue."
            await interaction.followup.send(summary)
            return

        lines = [
            f"{idx + 1}. [{rec['title']}]({rec['url']})"
            for idx, rec in enumerate(suggestions)
        ]
        embed = discord.Embed(
            title="Recommended Tracks",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=f"Use /p <url> to queue a suggestion or set mode to queue. Max results: {limit}."
        )
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(PlayCommands(bot))
