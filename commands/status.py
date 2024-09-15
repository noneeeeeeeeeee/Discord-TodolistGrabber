import discord
from discord.ext import commands
from modules.apicall import fetch_api_data
import json

class Status(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        print("Status cog initialized.")

    @commands.command()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def apistatus(self, ctx):
        print("apistatus command invoked.")

        message = await ctx.reply("Checking API status...")

        try:
            # Check API status
            api_data = fetch_api_data(None, True)
            if api_data:
                api_data_json = json.loads(api_data)
                embed = discord.Embed(
                    title=":white_check_mark: API is working",
                    description="Please do not spam this command",
                    color=discord.Color.green()
                )
                status_info = api_data_json.get("Status", [{}])[0]
                responsetime = status_info.get("responsetime", "N/A")
                apicalltime = status_info.get("apicalltime", "N/A")

                embed.add_field(name="Response Time", value=f"{responsetime} seconds", inline=False)
                embed.add_field(name="API Call Time", value=apicalltime, inline=False)
            else:
                embed = discord.Embed(
                    title=":x: API is not working",
                    description="Please do not spam this command",
                    color=discord.Color.red()
                )
            await message.edit(content=None, embed=embed)
        except Exception as e:
            await message.edit(content=":x: An error occurred while checking the API status.")
            return

    @apistatus.error
    async def apistatus_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = round(error.retry_after, 1)
            await ctx.send(f":hourglass: Please wait {retry_after} seconds before using this command again. This is to prevent spamming to the api")
        else:
            await ctx.send(":x: An unexpected error occurred.")

async def setup(bot):
    print("Setting up Status cog.")
    await bot.add_cog(Status(bot))
