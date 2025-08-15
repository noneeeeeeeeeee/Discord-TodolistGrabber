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

    # New: multi-option view
    class ChooseUpdateView(discord.ui.View):
        def __init__(
            self, author_id: int, enable_stable: bool, enable_prerelease: bool
        ):
            super().__init__(timeout=45.0)
            self.author_id = author_id
            self.choice = None
            # Enable/disable after the auto-created buttons exist
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    if child.custom_id == "choose_stable":
                        child.disabled = not enable_stable
                    elif child.custom_id == "choose_prerelease":
                        child.disabled = not enable_prerelease

        @discord.ui.button(
            label="Update to Stable (recommended)",
            style=discord.ButtonStyle.green,
            custom_id="choose_stable",
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
            label="Update to Prerelease (may be unstable)",
            style=discord.ButtonStyle.blurple,
            custom_id="choose_prerelease",
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

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
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

    @commands.hybrid_command(name="checkupdates", description="Check for updates.")
    async def check_updates(self, ctx: commands.Context):
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

            # Determine valid upgrade candidates (strictly higher than current)
            stable_candidate = bool(
                stable_version and cmp(current_version, stable_version) < 0
            )
            prerelease_candidate = bool(
                prerelease_version and cmp(current_version, prerelease_version) < 0
            )

            # Up-to-date checks with prerelease-aware logic
            up_to_date = False
            reason = ""
            if prerelease_version and cmp(current_version, prerelease_version) == 0:
                up_to_date = True
                reason = "You are on the latest prerelease."
            elif stable_candidate or prerelease_candidate:
                up_to_date = False
            else:
                up_to_date = True
                reason = "You are on the latest stable."

            if up_to_date:
                embed = discord.Embed(
                    title="No Updates Available",
                    description=f"{reason}\nCurrent version: {current_version}",
                    color=discord.Color.dark_gray(),
                )
                if stable_version:
                    embed.add_field(
                        name="Latest Stable", value=stable_version, inline=True
                    )
                if prerelease_version:
                    embed.add_field(
                        name="Latest Prerelease", value=prerelease_version, inline=True
                    )
                await ctx.send(embed=embed)
                return

            # If both candidates exist, let user choose
            if stable_candidate and prerelease_candidate:
                desc_parts = []
                desc_parts.append(
                    f"Stable target: v{stable_version}\n{stable_changelog or 'No changelog provided.'}"
                )
                desc_parts.append(
                    f"\nPrerelease target: v{prerelease_version}\n{prerelease_changelog or 'No changelog provided.'}\n:warning: Prereleases may be unstable."
                )
                embed = discord.Embed(
                    title="Multiple updates available",
                    description="\n".join(desc_parts),
                    color=discord.Color.gold(),
                )
                embed.add_field(
                    name="Current Version", value=current_version, inline=False
                )
                view = self.ChooseUpdateView(
                    ctx.author.id, enable_stable=True, enable_prerelease=True
                )
                view.message = await ctx.send(embed=embed, view=view)
                await view.wait()

                if view.choice == "stable":
                    subprocess.Popen(
                        [
                            "python",
                            "modules/otaUpdate/startOTA.py",
                            "worker",
                            "--prefer-stable",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    await ctx.send(
                        ":warning: The bot will now shortly shutdown for OTA update to Stable. If it doesn't turn back on in a few minutes, please check the ota_logs."
                    )
                elif view.choice == "prerelease":
                    subprocess.Popen(
                        [
                            "python",
                            "modules/otaUpdate/startOTA.py",
                            "worker",
                            "--prefer-prerelease",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    await ctx.send(
                        ":warning: The bot will now shortly shutdown for OTA update to Prerelease. If it doesn't turn back on in a few minutes, please check the ota_logs."
                    )
                else:
                    await ctx.send("OTA update cancelled.")
                return

            # Otherwise, single target flow (confirm + pass appropriate flag)
            is_prerelease_target = prerelease_candidate and not stable_candidate
            target_version = (
                prerelease_version if is_prerelease_target else stable_version
            )
            target_changelog = (
                prerelease_changelog if is_prerelease_target else stable_changelog
            ) or "No changelog provided."
            title = (
                "Prerelease Available" if is_prerelease_target else "Update Available!"
            )
            warn = (
                "\n:warning: This is a prerelease and may be unstable."
                if is_prerelease_target
                else ""
            )
            desc = f"# Changelog v{target_version}\n{target_changelog}{warn}"
            embed = discord.Embed(
                title=title,
                description=desc,
                color=(
                    discord.Color.orange()
                    if is_prerelease_target
                    else discord.Color.green()
                ),
            )
            embed.add_field(name="Current Version", value=current_version, inline=True)
            if stable_version:
                embed.add_field(name="Latest Stable", value=stable_version, inline=True)
            if prerelease_version:
                embed.add_field(
                    name="Latest Prerelease", value=prerelease_version, inline=True
                )

            view = self.ConfirmUpdateView(ctx.author.id)
            view.message = await ctx.send(embed=embed, view=view)
            await view.wait()

            if view.value:
                args = [
                    "python",
                    "modules/otaUpdate/startOTA.py",
                    "worker",
                    (
                        "--prefer-prerelease"
                        if is_prerelease_target
                        else "--prefer-stable"
                    ),
                ]
                subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                await ctx.send(
                    ":warning: The bot will now shortly shutdown for OTA update. If it doesn't turn back on in a few minutes, please check the ota_logs."
                )
            else:
                await ctx.send("OTA update cancelled.")
        except Exception as e:
            embed = discord.Embed(
                title="Error Checking Updates",
                description=f"Error: {e}",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            print(f"Error: {e}")


async def setup(bot):
    await bot.add_cog(Update(bot))
