import subprocess
from discord.ext import commands
from modules.otaUpdate import check

class Update(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="checkupdates", description="Check for updates.")
    async def check_updates(self, ctx: commands.Context):
        try:
            # Check for updates
            result = check.check_update()
            print(f"Update check result: {result}")

            update_available = result.get("status") == "update-available"
            version = result.get("current_version", "Unknown")

            if update_available:
                await ctx.send(
                    "Update available! Starting the OTA update process. The bot will stop shortly."
                )

                # Run startOTA.py in a separate process
                subprocess.Popen(
                    ["python", "modules/otaUpdate/startOTA.py"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                await ctx.send("OTA update process started successfully.")
            else:
                await ctx.send(f"No updates available. Latest version {version} is already installed.")
        except Exception as e:
            await ctx.send(f"Error checking updates: {e}")
            print(f"Error: {e}")

async def setup(bot):
    await bot.add_cog(Update(bot))
