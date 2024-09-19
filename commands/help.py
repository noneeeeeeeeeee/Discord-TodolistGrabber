import discord
from discord.ext import commands
from discord import app_commands
from modules.readversion import read_current_version

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.cooldown(1, 10, commands.BucketType.user)  # Add 10-second cooldown
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
    @commands.cooldown(1, 10, commands.BucketType.user)  # Add 10-second cooldown for slash command as well
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

    # Handle the cooldown error for both text and slash commands
    @help.error
    async def help_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = round(error.retry_after, 1)
            await ctx.send(f":hourglass: Please wait {retry_after} seconds before using this command again.")
        else:
            await ctx.send(":x: An unexpected error occurred.")

    @help_slash.error
    async def help_slash_error(self, interaction: discord.Interaction, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = round(error.retry_after, 1)
            await interaction.response.send_message(f":hourglass: Please wait {retry_after} seconds before using this command again.", ephemeral=True)
        else:
            await interaction.response.send_message(":x: An unexpected error occurred.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Help(bot))
