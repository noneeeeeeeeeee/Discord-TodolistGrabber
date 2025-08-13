import discord
from discord.ext import commands
from discord import app_commands
from modules.readversion import read_current_version
from modules.setconfig import json_get, check_guild_config_available


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.cooldown(1, 10, commands.BucketType.user)  # Add 10-second cooldown
    async def help(self, ctx):
        guild_id = getattr(ctx.guild, "id", None)
        cfg = (
            json_get(guild_id)
            if guild_id and check_guild_config_available(guild_id)
            else {}
        )
        nb_enabled = cfg.get("Noticeboard", {}).get("Enabled", True)
        music_enabled = cfg.get("Music", {}).get("Enabled", False)

        embed = discord.Embed(
            title="Help Menu",
            color=discord.Color.blue(),
        )
        # General
        embed.add_field(
            name="General",
            value="/help\n/ping\n/settings",
            inline=False,
        )
        # Noticeboard
        embed.add_field(
            name=f"{'✅' if nb_enabled else '❌'} Noticeboard",
            value="!noticeboard",
            inline=False,
        )
        # Music
        embed.add_field(
            name=f"{'✅' if music_enabled else '❌'} Music",
            value="/play, /queue, /pauseplay, /skip, /dc, /volume",
            inline=False,
        )
        embed.set_footer(text="Bot Version: " + read_current_version())
        await ctx.send(embed=embed)

    @app_commands.command(name="help", description="Displays the help menu.")
    @commands.cooldown(
        1, 10, commands.BucketType.user
    )  # Add 10-second cooldown for slash command as well
    async def help_slash(self, interaction: discord.Interaction):
        guild_id = getattr(interaction.guild, "id", None)
        cfg = (
            json_get(guild_id)
            if guild_id and check_guild_config_available(guild_id)
            else {}
        )
        nb_enabled = cfg.get("Noticeboard", {}).get("Enabled", True)
        music_enabled = cfg.get("Music", {}).get("Enabled", False)

        embed = discord.Embed(
            title="Help Menu",
            color=discord.Color.blue(),
        )
        embed.add_field(name="General", value="/help\n/ping\n/settings", inline=False)
        embed.add_field(
            name=f"{'✅' if nb_enabled else '❌'} Noticeboard",
            value="!noticeboard",
            inline=False,
        )
        embed.add_field(
            name=f"{'✅' if music_enabled else '❌'} Music",
            value="/play, /queue, /pauseplay, /skip, /dc, /volume",
            inline=False,
        )
        embed.set_footer(text="Bot Version: " + read_current_version())
        await interaction.response.send_message(embed=embed)

    # Handle the cooldown error for both text and slash commands
    @help.error
    async def help_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = round(error.retry_after, 1)
            await ctx.send(
                f":hourglass: Please wait {retry_after} seconds before using this command again."
            )
        else:
            await ctx.send(":x: An unexpected error occurred.")

    @help_slash.error
    async def help_slash_error(self, interaction: discord.Interaction, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = round(error.retry_after, 1)
            await interaction.response.send_message(
                f":hourglass: Please wait {retry_after} seconds before using this command again.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                ":x: An unexpected error occurred.", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(Help(bot))
