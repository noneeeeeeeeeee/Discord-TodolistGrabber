import discord
from discord.ext import commands
from modules.readversion import read_current_version

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def help(self, ctx):
        embed = discord.Embed(
            title="Help Menu",
            description="Help menu.",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="Help", value="This Command", inline=False)
        embed.add_field(name="Ping", value="Response time from the bot to discord", inline=False)
        embed.add_field(name="Status", value="Check if the API is up", inline=False)
        embed.add_field(name="NoticeBoard", value="Setup the notice board [channel] [pingrole]", inline=False)
        embed.set_footer(text="Bot Version: " + read_current_version())
        message = await ctx.reply(embed=embed)
        
        await message.edit(content=None, embed=embed)

async def setup(bot):
    await bot.add_cog(Help(bot))