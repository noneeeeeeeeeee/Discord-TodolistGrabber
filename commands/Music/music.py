import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

import discord
from discord.ext import commands

from modules.setconfig import (
    SETTINGS_SCHEMA,
    check_guild_config_available,
    edit_json_file,
    json_get,
)
from modules.music.music_player import SPONSORBLOCK_ALLOWED

LOG = logging.getLogger(__name__)


DEFAULT_SPONSORBLOCK_CATEGORIES: List[str] = list(
    SETTINGS_SCHEMA["Music"]["SponsorBlockCategories"]["default"]
)


def _format_category_label(raw: str) -> str:
    return raw.replace("_", " ").title()


class MusicSettingsView(discord.ui.View):
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.bot = ctx.bot
        self.guild_id = ctx.guild.id
        self.message: Optional[discord.Message] = None
        self.state: Dict[str, Any] = self._load_state()

        # interactive controls
        self.auto_play_button = discord.ui.Button(custom_id="music:auto", row=0)
        self.auto_play_button.callback = self._toggle_autoplay  # type: ignore
        self.add_item(self.auto_play_button)

        self.sponsorblock_button = discord.ui.Button(
            custom_id="music:sponsorblock",
            row=0,
        )
        self.sponsorblock_button.callback = self._toggle_sponsorblock  # type: ignore
        self.add_item(self.sponsorblock_button)

        self.nonsong_button = discord.ui.Button(
            custom_id="music:nonsong",
            row=1,
        )
        self.nonsong_button.callback = self._toggle_nonsong_filter  # type: ignore
        self.add_item(self.nonsong_button)

        options = [
            discord.SelectOption(
                label=_format_category_label(cat),
                value=cat,
            )
            for cat in sorted(SPONSORBLOCK_ALLOWED)
        ]
        self.category_select = discord.ui.Select(
            custom_id="music:categories",
            placeholder="SponsorBlock categories",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.category_select.callback = self._update_categories  # type: ignore
        self.add_item(self.category_select)

        self._sync_components()

    def _load_state(self) -> Dict[str, Any]:
        try:
            cfg = json_get(self.guild_id)
        except Exception:
            return {}
        return dict(cfg.get("Music", {}))

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Music Settings",
            color=discord.Color.blurple(),
        )

        autoplay = bool(self.state.get("AutoPlay", False))
        embed.add_field(
            name="AutoPlay",
            value="Enabled ✅" if autoplay else "Disabled ❌",
            inline=True,
        )

        sponsor_enabled = bool(self.state.get("SponsorBlockEnabled", True))
        categories = (
            self.state.get("SponsorBlockCategories") or DEFAULT_SPONSORBLOCK_CATEGORIES
        )
        embed.add_field(
            name="SponsorBlock",
            value=("Enabled ✅" if sponsor_enabled else "Disabled ❌"),
            inline=True,
        )

        remove_nonsongs = bool(self.state.get("RemoveNonSongsUsingSponsorBlock", True))
        embed.add_field(
            name="Non-song Filter",
            value="Filtering intros/outros" if remove_nonsongs else "Keep all segments",
            inline=False,
        )

        embed.add_field(
            name="SponsorBlock Categories",
            value=", ".join(_format_category_label(c) for c in categories) or "None",
            inline=False,
        )

        vote_percent = self.state.get("VoteSkipPercent", 60)
        vote_floor = self.state.get("VoteSkipFloor", 2)
        embed.add_field(
            name="Vote Skip",
            value=f"{vote_percent}% • minimum {vote_floor} listeners",
            inline=True,
        )

        auto_disconnect = self.state.get("AutoDisconnectSeconds", 300)
        embed.add_field(
            name="Auto Disconnect",
            value=f"{auto_disconnect}s of inactivity",
            inline=True,
        )

        queue_limit = self.state.get("QueueLimit", 10)
        embed.add_field(
            name="Queue Limit",
            value=f"Max {queue_limit} items" if queue_limit else "Unlimited",
            inline=False,
        )

        embed.set_footer(text="Adjustments apply instantly across the guild")
        return embed

    def _sync_components(self) -> None:
        autoplay = bool(self.state.get("AutoPlay", False))
        sponsor_enabled = bool(self.state.get("SponsorBlockEnabled", True))
        nonsong = bool(self.state.get("RemoveNonSongsUsingSponsorBlock", True))
        categories = list(
            self.state.get("SponsorBlockCategories") or DEFAULT_SPONSORBLOCK_CATEGORIES
        )

        self._style_toggle(self.auto_play_button, "AutoPlay", autoplay)
        self._style_toggle(self.sponsorblock_button, "SponsorBlock", sponsor_enabled)
        self._style_toggle(
            self.nonsong_button,
            "Skip Non-song Segments",
            nonsong,
        )

        updated_options = []
        for option in sorted(SPONSORBLOCK_ALLOWED):
            updated_options.append(
                discord.SelectOption(
                    label=_format_category_label(option),
                    value=option,
                    default=option in categories,
                )
            )
        self.category_select.options = updated_options
        self.category_select.max_values = max(1, len(updated_options))

    @staticmethod
    def _style_toggle(button: discord.ui.Button, label: str, enabled: bool) -> None:
        button.label = f"{label}: {'On' if enabled else 'Off'}"
        button.style = (
            discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        )

    async def _toggle_autoplay(self, interaction: discord.Interaction) -> None:
        if not await self._interaction_guard(interaction):
            return
        new_value = not bool(self.state.get("AutoPlay", False))
        await self._commit_setting(
            interaction, "Music.AutoPlay", new_value, apply_callback=None
        )

    async def _toggle_sponsorblock(self, interaction: discord.Interaction) -> None:
        if not await self._interaction_guard(interaction):
            return
        new_value = not bool(self.state.get("SponsorBlockEnabled", True))
        await self._commit_setting(
            interaction,
            "Music.SponsorBlockEnabled",
            new_value,
            apply_callback=self._apply_sponsorblock_live,
        )

    async def _toggle_nonsong_filter(self, interaction: discord.Interaction) -> None:
        if not await self._interaction_guard(interaction):
            return
        new_value = not bool(self.state.get("RemoveNonSongsUsingSponsorBlock", True))
        await self._commit_setting(
            interaction,
            "Music.RemoveNonSongsUsingSponsorBlock",
            new_value,
            apply_callback=self._apply_sponsorblock_live,
        )

    async def _update_categories(self, interaction: discord.Interaction) -> None:
        if not await self._interaction_guard(interaction):
            return
        selected = list(self.category_select.values)
        await self._commit_setting(
            interaction,
            "Music.SponsorBlockCategories",
            selected,
            apply_callback=self._apply_sponsorblock_live,
        )

    async def _commit_setting(
        self,
        interaction: discord.Interaction,
        key: str,
        value: Any,
        *,
        apply_callback: Optional[Callable[[], Awaitable[None]]],
    ) -> None:
        actor_id = getattr(interaction.user, "id", None)
        try:
            edit_json_file(
                self.guild_id,
                key,
                value,
                actor_user_id=actor_id,
            )
        except Exception as exc:  # pragma: no cover - configuration errors
            LOG.warning("Failed to update %s: %s", key, exc)
            await interaction.response.send_message(
                f":x: Could not update setting: {exc}", ephemeral=True
            )
            return

        if apply_callback is not None:
            try:
                await apply_callback()
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("Live apply failed for %s: %s", key, exc)

        self.state = self._load_state()
        self._sync_components()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _apply_sponsorblock_live(self) -> None:
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            return
        try:
            await player._apply_sponsorblock_settings(self.guild_id)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOG.warning("Applying SponsorBlock settings failed: %s", exc)

    async def _interaction_guard(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if user is None or user.id != self.ctx.author.id:
            await interaction.response.send_message(
                ":x: Only the command invoker can use this panel.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class MusicSettings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="musicsettings", description="Interactive panel for music options."
    )
    async def musicsettings(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send(":x: Guild-only command.")
            return

        if not check_guild_config_available(ctx.guild.id):
            await ctx.send("Server not setup. Please run !setup.")
            return

        perms = ctx.author.guild_permissions
        if not (perms.manage_guild or perms.administrator):
            await ctx.send(":x: Manage Server permission required.")
            return

        view = MusicSettingsView(ctx)
        embed = view.build_embed()

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )
            try:
                view.message = await ctx.interaction.original_response()
            except Exception:
                view.message = None
        else:
            msg = await ctx.send(embed=embed, view=view)
            view.message = msg


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicSettings(bot))
