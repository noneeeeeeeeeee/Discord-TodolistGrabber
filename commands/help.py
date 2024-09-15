import discord
from discord.ext import commands
from discord import app_commands
from modules.readversion import read_current_version

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def help(self, ctx):
        """Traditional text command help."""
        embed = discord.Embed(
            title="Help Menu",
            description="Commands with (hybrid) are available as slash commands. The rest are legacy text commands. It is recommended to use the slash commands.",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="!help (hybrid)", value="This Command", inline=False)
        embed.add_field(name="!ping (hybrid)", value="Response time from the bot to discord", inline=False)
        embed.add_field(name="!apistatus", value="Check if the API is up", inline=False)
        embed.add_field(name="!noticeboard", value="Open the NoticeBoard setup menu", inline=False)
        embed.add_field(name="!setup", value="Open the Setup Wizard", inline=False)
        embed.set_footer(text="Bot Version: " + read_current_version())
        
        await ctx.send(embed=embed)

    @app_commands.command(name="help", description="Displays the help menu.")
    async def help_slash(self, interaction: discord.Interaction):
        """Slash command help."""
        embed = discord.Embed(
            title="Help Menu",
            description="Commands with (hybrid) are available as slash commands. The rest are legacy text commands. It is recommended to use the slash commands.",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="!help (hybrid)", value="This Command", inline=False)
        embed.add_field(name="!ping (hybrid)", value="Response time from the bot to discord", inline=False)
        embed.add_field(name="!apistatus", value="Check if the API is up", inline=False)
        embed.add_field(name="!noticeboard", value="Open the NoticeBoard setup menu", inline=False)
        embed.add_field(name="!setup", value="Open the Setup Wizard", inline=False)
        embed.set_footer(text="Bot Version: " + read_current_version())
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Help(bot))
