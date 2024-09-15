import discord
from discord.ext import commands

class NoticeBoard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def setNoticeBoard(self, ctx):
        embed = discord.Embed(
            title="Noticeboard Configuration",
            description="Below are the current configurations for the noticeboard.",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="Command 1", value="Description for command 1", inline=False)
        embed.add_field(name="Command 2", value="Description for command 2", inline=False)
        embed.add_field(name="Command 3", value="Description for command 3", inline=False)
        
        message = await ctx.send(embed=embed)
        
        await message.edit(content=None, embed=embed)

async def setup(bot):
    await bot.add_cog(NoticeBoard(bot))