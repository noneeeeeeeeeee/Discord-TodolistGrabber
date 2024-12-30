import discord
from discord.ext import commands
from modules.otaUpdate import check
# import modules.otaUpdate.startOTA as start_ota

# class Update(commands.Cog):
#     def __init__(self, bot):
#         self.bot = bot

#     class UpdateView(discord.ui.View):
#         def __init__(self):
#             super().__init__(timeout=15.0)
#             self.value = None

#         @discord.ui.button(
#             label="Start OTA Update", style=discord.ButtonStyle.green, emoji="✅"
#         )
#         async def confirm(
#             self, button: discord.ui.Button, interaction: discord.Interaction
#         ):
#             self.stop()

#         async def on_timeout(self):
#             for child in self.children:
#                 child.disabled = True
#             await self.message.edit(view=self)

#     @commands.hybrid_command(name="checkupdates", description="Check for updates.")
#     async def check_updates(self, ctx: commands.Context):
#         # Check for updates using the imported function
#         result = check.check_update()
#         print(result)
#         update_available = False
#         version = "Unknown"
#         if update_available:
#             view = self.UpdateView()
#             view.message = await ctx.send(
#                 "Update available! Click the button to start the OTA update.", view=view
#             )
#             await view.wait()

#             if view.value:
#                 await view.message.edit(content="Starting OTA update... The bot will be stopped soon, if something goes wrong within 10 minutes please check the ota_logs in the bot folder", view=None)
#                 start_ota.main()
#             else:
#                 await view.message.edit(
#                     content="Update confirmation timed out.", view=None
#                 )
#         else:
#             await ctx.send(f"No updates available. Latest version {version} is already installed.")

async def setup(bot):
   await bot.add_cog(Update(bot))