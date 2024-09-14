import discord
from discord.ext import commands
from modules.apicall import fetch_api_data
from modules.enviromentfilegenerator import check_and_load_env_file

class Status(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def apistatus(self, ctx):
        message = await ctx.send("Checking API status...")
        
        # Check API status
        api_data = await fetch_api_data()
        if api_data:
            embed = discord.Embed(
                title="API Status",
                description=":white_check_mark: API is working",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="API Status",
                description=":x: API is not working",
                color=discord.Color.red()
            )
        
        await message.edit(content=None, embed=embed)
