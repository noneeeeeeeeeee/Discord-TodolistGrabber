import discord
from discord.ext import commands
import time

class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def help(self, ctx):
        embed = discord.Embed(
            title="Help",
            description="Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="Command 1", value="Description for command 1", inline=False)
        embed.add_field(name="Command 2", value="Description for command 2", inline=False)
        embed.add_field(name="Command 3", value="Description for command 3", inline=False)
        
        message = await ctx.send(embed=embed)
        
        await message.edit(content=None, embed=embed)

async def setup(bot):
    await bot.add_cog(Ping(bot))