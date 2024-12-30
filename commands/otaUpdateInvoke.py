import subprocess
import discord
from discord.ext import commands
from modules.otaUpdate import check

class Update(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    class ConfirmUpdateView(discord.ui.View):
        def __init__(self, author_id):
            super().__init__(timeout=30.0)  # Timeout after 30 seconds
            self.author_id = author_id
            self.value = None

        @discord.ui.button(label="Confirm Update", style=discord.ButtonStyle.green)
        async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
            # Ensure only the original user can confirm
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    "You are not authorized to confirm this update.", ephemeral=True
                )
                return

            self.value = True
            await interaction.response.send_message("Update confirmed! Starting OTA process.")
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
            # Ensure only the original user can cancel
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(
                    "You are not authorized to cancel this update.", ephemeral=True
                )
                return

            self.value = False
            await interaction.response.send_message("Update cancelled.")
            self.stop()

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            if self.message:
                await self.message.edit(view=self)

    @commands.hybrid_command(name="checkupdates", description="Check for updates.")
    async def check_updates(self, ctx: commands.Context):
        try:
            # Check for updates
            result = check.check_update()
            print(f"Update check result: {result}")

            update_available = result.get("status") == "update-available"
            current_version = result.get("current_version", "Unknown")
            new_version = result.get("new_version", "Unknown")
            changelog = result.get("changelog", "Unknown")

            if update_available:
                embed = discord.Embed(
                    title="Update Available!",
                    description=f"Current Version: {current_version}\nNew Version: {new_version}\nChangelog: {changelog}",
                    color=discord.Color.green()
                )
                view = self.ConfirmUpdateView(ctx.author.id)
                view.message = await ctx.send(embed=embed, view=view)

                await view.wait()  # Wait for user interaction

                if view.value:
                    # Run startOTA.py in a separate process
                    subprocess.Popen(
                        ["python", "modules/otaUpdate/startOTA.py"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    await ctx.send("OTA update process started successfully.")
                else:
                    await ctx.send("OTA update cancelled.")
            else:
                embed = discord.Embed(
                    title="No Updates Available",
                    description=f"Latest version {current_version} is already installed.",
                    color=discord.Color.dark_gray()
                )
                await ctx.send(embed=embed)
        except Exception as e:
            embed = discord.Embed(
                title="Error Checking Updates",
                description=f"Error: {e}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            print(f"Error: {e}")

async def setup(bot):
    await bot.add_cog(Update(bot))
