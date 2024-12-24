import discord
from discord.ext import commands
import subprocess
import asyncio


class Update(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    class UpdateView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=15.0)
            self.value = None

        @discord.ui.button(
            label="Start OTA Update", style=discord.ButtonStyle.green, emoji="✅"
        )
        async def confirm(
            self, button: discord.ui.Button, interaction: discord.Interaction
        ):
            self.value = True
            self.stop()

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            await self.message.edit(view=self)

    @commands.hybrid_command(name="checkupdates", description="Check for updates.")
    async def check_updates(self, ctx: commands.Context):
        # Run the check.py script
        result = subprocess.run(["python", "check.py"], capture_output=True, text=True)

        if "update available" in result.stdout:
            view = self.UpdateView()
            view.message = await ctx.send(
                "Update available! Click the button to start the OTA update.", view=view
            )
            await view.wait()

            if view.value:
                await view.message.edit(content="Starting OTA update...", view=None)
                subprocess.run(["python", "startOTA.py"])
            else:
                await view.message.edit(
                    content="Update confirmation timed out.", view=None
                )
        else:
            await ctx.send("No updates available.")


async def setup(bot):
    await bot.add_cog(Update(bot))
