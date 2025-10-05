import subprocess
import discord
from discord.ext import commands
from modules.otaUpdate import check
import os
import re


class Update(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- version helpers ---
    @staticmethod
    def _parse_version(ver: str):
        """
        Parse versions like:
          - 2.4.2        -> (2,4,2, 0, 0)        stable
          - 3.0          -> (3,0,0, 0, 0)        stable
          - 3.0-Pre2     -> (3,0,0, 1, 2)        prerelease
          - 3.1-pre1     -> (3,1,0, 1, 1)
        Higher tuple wins; stable (flag=0) > prerelease (flag=1) for same base.
        """
        if not ver:
            return (0, 0, 0, 1, 0)
        ver = ver.strip()
        # Split prerelease suffix if present
        m = re.match(
            r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([A-Za-z]+)?(\d+)?)?\s*$", ver
        )
        if not m:
            # Fallback: try digits only
            nums = [int(x) for x in re.findall(r"\d+", ver)]
            major = nums[0] if len(nums) > 0 else 0
            minor = nums[1] if len(nums) > 1 else 0
            patch = nums[2] if len(nums) > 2 else 0
            return (major, minor, patch, 1, 0)
        major = int(m.group(1) or 0)
        minor = int(m.group(2) or 0)
        patch = int(m.group(3) or 0)
        pre_label = (m.group(4) or "").lower()
        pre_num = int(m.group(5) or 0)
        # treat any suffix as prerelease (pre, beta, alpha, rc, etc.)
        is_prerelease = 1 if pre_label else 0
        return (major, minor, patch, is_prerelease, pre_num)

    @staticmethod
    def _cmp_versions(a: str, b: str) -> int:
        ta = Update._parse_version(a)
        tb = Update._parse_version(b)
        return (ta > tb) - (ta < tb)

    class ConfirmUpdateView(discord.ui.View):
        def __init__(self, author_id):
            super().__init__(timeout=30.0)
            self.author_id = author_id
            self.value = None

        @discord.ui.button(label="Confirm Update", style=discord.ButtonStyle.green)
        async def confirm(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            # Ensure only the original user can confirm
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    ":x: You are not authorized to confirm this update.", ephemeral=True
                )
                return

            self.value = True
            await interaction.response.send_message(
                ":white_check_mark: Update confirmed! Starting OTA process."
            )
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            # Ensure only the original user can cancel
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    ":x: You are not authorized to cancel this update.", ephemeral=True
                )
                return

            self.value = False
            self.stop()

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            if hasattr(self, "message") and self.message:
                await self.message.edit(view=self)

    # New: multi-option view with flexible options
    class ChooseActionView(discord.ui.View):
        def __init__(
            self,
            author_id: int,
            enable_stable: bool = True,
            enable_prerelease: bool = True,
            enable_reinstall: bool = False,
            enable_switch: bool = False,
            switch_label: str = "Switch Channel",
        ):
            super().__init__(timeout=60.0)
            self.author_id = author_id
            self.choice = None
            self.enable_stable = enable_stable
            self.enable_prerelease = enable_prerelease
            self.enable_reinstall = enable_reinstall
            self.enable_switch = enable_switch
            self.switch_label = switch_label

            # Dynamically update button states
            self._update_buttons()

        def _update_buttons(self):
            """Update button visibility and enabled state."""
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    if child.custom_id == "choose_stable":
                        child.disabled = not self.enable_stable
                    elif child.custom_id == "choose_prerelease":
                        child.disabled = not self.enable_prerelease
                    elif child.custom_id == "choose_reinstall":
                        child.disabled = not self.enable_reinstall
                    elif child.custom_id == "choose_switch":
                        child.disabled = not self.enable_switch
                        child.label = self.switch_label

        @discord.ui.button(
            label="Update to Stable",
            style=discord.ButtonStyle.green,
            custom_id="choose_stable",
            row=0,
        )
        async def choose_stable(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    ":x: You are not authorized to choose this option.", ephemeral=True
                )
                return
            self.choice = "stable"
            await interaction.response.send_message(
                ":white_check_mark: Proceeding with stable update."
            )
            self.stop()

        @discord.ui.button(
            label="Update to Prerelease",
            style=discord.ButtonStyle.blurple,
            custom_id="choose_prerelease",
            row=0,
        )
        async def choose_prerelease(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    ":x: You are not authorized to choose this option.", ephemeral=True
                )
                return
            self.choice = "prerelease"
            await interaction.response.send_message(
                ":white_check_mark: Proceeding with prerelease update."
            )
            self.stop()

        @discord.ui.button(
            label="Reinstall Current",
            style=discord.ButtonStyle.grey,
            custom_id="choose_reinstall",
            row=1,
        )
        async def choose_reinstall(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    ":x: You are not authorized to choose this option.", ephemeral=True
                )
                return
            self.choice = "reinstall"
            await interaction.response.send_message(
                ":white_check_mark: Reinstalling current version."
            )
            self.stop()

        @discord.ui.button(
            label="Switch Channel",
            style=discord.ButtonStyle.primary,
            custom_id="choose_switch",
            row=1,
        )
        async def choose_switch(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    ":x: You are not authorized to choose this option.", ephemeral=True
                )
                return
            self.choice = "switch"
            await interaction.response.send_message(
                ":white_check_mark: Switching channel."
            )
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=2)
        async def cancel(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    ":x: You are not authorized to cancel.", ephemeral=True
                )
                return
            self.choice = None
            self.stop()

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            if hasattr(self, "message") and self.message:
                await self.message.edit(view=self)

    def _truncate_changelog(self, changelog: str, max_length: int = 3500) -> str:
        """Truncate changelog to fit within Discord embed limits."""
        if not changelog or len(changelog) <= max_length:
            return changelog
        return (
            changelog[:max_length]
            + "\n\n... (changelog truncated, see full release notes on GitHub)"
        )

    @commands.hybrid_command(name="checkupdates", description="Check for updates.")
    @discord.app_commands.describe(
        channel="Update channel to check: 'stable' (default) or 'prerelease'"
    )
    async def check_updates(self, ctx: commands.Context, channel: str = "stable"):
        # OWNER gate
        try:
            owner_env = os.getenv("OWNER_ID")
            owner_id = int(owner_env) if owner_env and owner_env.isdigit() else None
        except Exception:
            owner_id = None

        if owner_id is None or ctx.author.id != owner_id:
            # Disable for non-owners
            try:
                if (
                    getattr(ctx, "interaction", None)
                    and not ctx.interaction.response.is_done()
                ):
                    await ctx.interaction.response.send_message(
                        ":x: This command is restricted to the bot owner.",
                        ephemeral=True,
                    )
                else:
                    await ctx.send(
                        ":x: This command is restricted to the bot owner.",
                        delete_after=10,
                    )
            except Exception:
                pass
            return

        # Normalize channel parameter
        channel = channel.lower().strip()
        if channel not in ["stable", "prerelease", "pre"]:
            await ctx.send(
                ":x: Invalid channel. Use 'stable' or 'prerelease'.", delete_after=10
            )
            return

        # Normalize 'pre' to 'prerelease'
        if channel == "pre":
            channel = "prerelease"

        check_prerelease = channel == "prerelease"

        try:
            # Check for updates
            result = check.check_update()

            current_version = result.get("current_version", "0")
            stable_version = result.get("stable_version")
            prerelease_version = result.get("prerelease_version")
            stable_changelog = result.get("stable_changelog", "Unknown")
            prerelease_changelog = result.get("prerelease_changelog", "Unknown")

            def cmp(a, b):
                return Update._cmp_versions(a or "0", b or "0")

            # Determine what type of version we're currently on
            current_parsed = Update._parse_version(current_version)
            is_on_prerelease = current_parsed[3] == 1  # prerelease flag
            is_on_stable = not is_on_prerelease

            # Check if current matches stable or prerelease
            on_latest_stable = (
                stable_version and cmp(current_version, stable_version) == 0
            )
            on_latest_prerelease = (
                prerelease_version and cmp(current_version, prerelease_version) == 0
            )

            # Determine valid upgrade candidates
            stable_candidate = bool(
                stable_version and cmp(current_version, stable_version) < 0
            )
            prerelease_candidate = bool(
                prerelease_version and cmp(current_version, prerelease_version) < 0
            )

            # Logic based on channel parameter
            if check_prerelease:
                # User wants to check prerelease channel
                if on_latest_prerelease:
                    # Already on latest prerelease
                    embed = discord.Embed(
                        title="✅ Up to Date (Prerelease)",
                        description=f"You are on the latest prerelease version.\n\n**Current:** {current_version}",
                        color=discord.Color.blue(),
                    )
                    if stable_version:
                        embed.add_field(
                            name="Latest Stable", value=stable_version, inline=True
                        )
                    if prerelease_version:
                        embed.add_field(
                            name="Latest Prerelease",
                            value=prerelease_version,
                            inline=True,
                        )

                    # Offer options to reinstall or switch to stable
                    view = self.ChooseActionView(
                        ctx.author.id,
                        enable_stable=False,
                        enable_prerelease=False,
                        enable_reinstall=True,
                        enable_switch=stable_version is not None,
                        switch_label="Switch to Stable",
                    )
                    view.message = await ctx.send(embed=embed, view=view)
                    await view.wait()

                    if view.choice == "reinstall":
                        self._start_ota("--prefer-prerelease")
                        await ctx.send(
                            ":warning: Reinstalling current prerelease version..."
                        )
                    elif view.choice == "switch":
                        self._start_ota("--prefer-stable")
                        await ctx.send(":warning: Switching to stable channel...")
                    return

                elif prerelease_candidate:
                    # New prerelease available
                    prerelease_changelog_truncated = self._truncate_changelog(
                        prerelease_changelog or "No changelog provided.", 3800
                    )
                    desc = f"# Changelog v{prerelease_version}\n{prerelease_changelog_truncated}\n\n:warning: This is a prerelease and may be unstable."
                    embed = discord.Embed(
                        title="🆕 Prerelease Update Available",
                        description=desc,
                        color=discord.Color.orange(),
                    )
                    embed.add_field(name="Current", value=current_version, inline=True)
                    embed.add_field(
                        name="Latest Prerelease", value=prerelease_version, inline=True
                    )
                    if stable_version:
                        embed.add_field(
                            name="Latest Stable", value=stable_version, inline=True
                        )

                    view = self.ChooseActionView(
                        ctx.author.id,
                        enable_stable=False,
                        enable_prerelease=True,
                        enable_reinstall=True,
                        enable_switch=stable_version and not on_latest_stable,
                        switch_label="Switch to Stable" if stable_version else "Switch",
                    )
                    view.message = await ctx.send(embed=embed, view=view)
                    await view.wait()

                    if view.choice == "prerelease":
                        self._start_ota("--prefer-prerelease")
                        await ctx.send(":warning: Updating to latest prerelease...")
                    elif view.choice == "reinstall":
                        self._start_ota("--prefer-prerelease")
                        await ctx.send(":warning: Reinstalling current version...")
                    elif view.choice == "switch":
                        self._start_ota("--prefer-stable")
                        await ctx.send(":warning: Switching to stable channel...")
                    elif view.choice is None:
                        await ctx.send("Update cancelled.")
                    return
                else:
                    # No prerelease updates
                    embed = discord.Embed(
                        title="ℹ️ No Prerelease Updates",
                        description=f"No newer prerelease versions available.\n\n**Current:** {current_version}",
                        color=discord.Color.greyple(),
                    )
                    if stable_version:
                        embed.add_field(
                            name="Latest Stable", value=stable_version, inline=True
                        )
                    if prerelease_version:
                        embed.add_field(
                            name="Latest Prerelease",
                            value=prerelease_version,
                            inline=True,
                        )

                    view = self.ChooseActionView(
                        ctx.author.id,
                        enable_stable=False,
                        enable_prerelease=False,
                        enable_reinstall=True,
                        enable_switch=stable_version is not None,
                        switch_label="Switch to Stable",
                    )
                    view.message = await ctx.send(embed=embed, view=view)
                    await view.wait()

                    if view.choice == "reinstall":
                        self._start_ota(
                            "--prefer-prerelease"
                            if is_on_prerelease
                            else "--prefer-stable"
                        )
                        await ctx.send(":warning: Reinstalling current version...")
                    elif view.choice == "switch":
                        self._start_ota("--prefer-stable")
                        await ctx.send(":warning: Switching to stable channel...")
                    return

            else:
                # User wants to check stable channel (default)
                if on_latest_stable:
                    # Already on latest stable
                    embed = discord.Embed(
                        title="✅ Up to Date (Stable)",
                        description=f"You are on the latest stable version.\n\n**Current:** {current_version}",
                        color=discord.Color.green(),
                    )
                    if stable_version:
                        embed.add_field(
                            name="Latest Stable", value=stable_version, inline=True
                        )
                    if prerelease_version:
                        embed.add_field(
                            name="Latest Prerelease",
                            value=prerelease_version,
                            inline=True,
                        )

                    # Offer options to reinstall or switch to prerelease
                    view = self.ChooseActionView(
                        ctx.author.id,
                        enable_stable=False,
                        enable_prerelease=False,
                        enable_reinstall=True,
                        enable_switch=prerelease_version is not None,
                        switch_label="Switch to Prerelease",
                    )
                    view.message = await ctx.send(embed=embed, view=view)
                    await view.wait()

                    if view.choice == "reinstall":
                        self._start_ota("--prefer-stable")
                        await ctx.send(
                            ":warning: Reinstalling current stable version..."
                        )
                    elif view.choice == "switch":
                        self._start_ota("--prefer-prerelease")
                        await ctx.send(":warning: Switching to prerelease channel...")
                    return

                elif stable_candidate:
                    # New stable version available
                    stable_changelog_truncated = self._truncate_changelog(
                        stable_changelog or "No changelog provided.", 3800
                    )
                    desc = (
                        f"# Changelog v{stable_version}\n{stable_changelog_truncated}"
                    )
                    embed = discord.Embed(
                        title="🎉 Stable Update Available",
                        description=desc,
                        color=discord.Color.green(),
                    )
                    embed.add_field(name="Current", value=current_version, inline=True)
                    embed.add_field(
                        name="Latest Stable", value=stable_version, inline=True
                    )
                    if prerelease_version:
                        embed.add_field(
                            name="Latest Prerelease",
                            value=prerelease_version,
                            inline=True,
                        )

                    view = self.ChooseActionView(
                        ctx.author.id,
                        enable_stable=True,
                        enable_prerelease=False,
                        enable_reinstall=True,
                        enable_switch=prerelease_version and not on_latest_prerelease,
                        switch_label=(
                            "Switch to Prerelease" if prerelease_version else "Switch"
                        ),
                    )
                    view.message = await ctx.send(embed=embed, view=view)
                    await view.wait()

                    if view.choice == "stable":
                        self._start_ota("--prefer-stable")
                        await ctx.send(":warning: Updating to latest stable version...")
                    elif view.choice == "reinstall":
                        self._start_ota(
                            "--prefer-stable" if is_on_stable else "--prefer-prerelease"
                        )
                        await ctx.send(":warning: Reinstalling current version...")
                    elif view.choice == "switch":
                        self._start_ota("--prefer-prerelease")
                        await ctx.send(":warning: Switching to prerelease channel...")
                    elif view.choice is None:
                        await ctx.send("Update cancelled.")
                    return
                else:
                    # No stable updates
                    embed = discord.Embed(
                        title="ℹ️ No Stable Updates",
                        description=f"No newer stable versions available.\n\n**Current:** {current_version}",
                        color=discord.Color.greyple(),
                    )
                    if stable_version:
                        embed.add_field(
                            name="Latest Stable", value=stable_version, inline=True
                        )
                    if prerelease_version:
                        embed.add_field(
                            name="Latest Prerelease",
                            value=prerelease_version,
                            inline=True,
                        )

                    view = self.ChooseActionView(
                        ctx.author.id,
                        enable_stable=False,
                        enable_prerelease=False,
                        enable_reinstall=True,
                        enable_switch=prerelease_version is not None,
                        switch_label="Switch to Prerelease",
                    )
                    view.message = await ctx.send(embed=embed, view=view)
                    await view.wait()

                    if view.choice == "reinstall":
                        self._start_ota(
                            "--prefer-stable" if is_on_stable else "--prefer-prerelease"
                        )
                        await ctx.send(":warning: Reinstalling current version...")
                    elif view.choice == "switch":
                        self._start_ota("--prefer-prerelease")
                        await ctx.send(":warning: Switching to prerelease channel...")
                    return

        except Exception as e:
            embed = discord.Embed(
                title="❌ Error Checking Updates",
                description=f"Error: {e}",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            print(f"Error in checkupdates: {e}")

    def _start_ota(self, flag: str):
        """Helper to start OTA update process."""
        subprocess.Popen(
            [
                "python",
                "modules/otaUpdate/startOTA.py",
                "worker",
                flag,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


async def setup(bot):
    await bot.add_cog(Update(bot))
