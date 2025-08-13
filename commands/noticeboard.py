import discord
from discord.ext import commands
import os
from modules.enviromentfilegenerator import check_and_load_env_file
from discord.ui import Button, View
from modules.setconfig import (
    edit_noticeboard_config,
    check_guild_config_available,
    check_admin_role,
    json_get,
)
from modules.cache import truncate_cache  # Make sure you have this function implemented


class NoticeBoard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        check_and_load_env_file()

    @commands.command(name="noticeboard")
    async def noticeboard_name(self, ctx, action: str = None, field: str = None):
        guild_id = ctx.guild.id
        # user_id = ctx.author.id

        user_roles = [role.id for role in ctx.author.roles]

        if not check_guild_config_available(guild_id):
            await ctx.send(
                "The default admin hasn't been set up yet. Please run the `!setup` command first."
            )
            return

        if not check_admin_role(guild_id, user_roles):
            await ctx.send(
                "You do not have permission to modify the noticeboard settings."
            )
            return

        if action:
            action = action.strip().lower()
        if field:
            field = field.strip().lower()

        if not action or not field:
            json_file = json_get(guild_id)
            nb = json_file.get("Noticeboard", {})
            noticeboard_channel_id = nb.get("ChannelId", "Default")
            noticeboard_update_interval = nb.get("UpdateInterval", None)
            ping_daily_time = nb.get("PingDailyTime", "Not Set")

            if noticeboard_update_interval is None:
                noticeboard_update_interval = "Not set"

            if noticeboard_channel_id == "Default":
                display_channel = "Not Set"
            else:
                display_channel = f"<#{noticeboard_channel_id}>"

            embed = discord.Embed(
                title="Noticeboard Configuration",
                description="Below are the current configurations for the noticeboard. \n Use `!noticeboard set <field>` to modify the settings.",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Channel", value=display_channel, inline=False)
            embed.add_field(
                name="Update Interval (seconds)",
                value=noticeboard_update_interval,
                inline=False,
            )
            embed.add_field(name="Daily Ping Time", value=ping_daily_time, inline=False)

            btnview = View(timeout=60)
            update_now = Button(label="🔄 Update Now", style=discord.ButtonStyle.green)
            delete_cache = Button(label="🗑️ Delete Cache", style=discord.ButtonStyle.red)

            async def update_callback(interaction):
                if interaction.user != ctx.author:
                    await interaction.response.send_message(
                        "You are not authorized to use this button.", ephemeral=True
                    )
                    return
                await interaction.response.send_message(
                    "Updating noticeboard now...", ephemeral=True
                )
                cog = self.bot.get_cog("NoticeAutoUpdate")
                if cog:
                    await cog.run_update_noticeboard_once(guild_id)

            async def delete_cache_callback(interaction):
                if interaction.user != ctx.author:
                    await interaction.response.send_message(
                        "You are not authorized to use this button.", ephemeral=True
                    )
                    return
                await interaction.response.send_message(
                    "Deleting cache...", ephemeral=True
                )
                truncate_cache()  # Ensure this function deletes the cache files

            update_now.callback = update_callback
            delete_cache.callback = delete_cache_callback
            btnview.add_item(update_now)
            btnview.add_item(delete_cache)

            # Send and retain the bot's panel message for safe edits later
            panel_msg = await ctx.send(embed=embed, view=btnview)

            async def on_timeout():
                for item in btnview.children:
                    item.disabled = True
                try:
                    await panel_msg.edit(view=btnview)
                except discord.Forbidden as e:
                    print(f"Failed to edit message: {e}")
                except Exception as e:
                    print(f"An unexpected error occurred: {e}")

            btnview.on_timeout = on_timeout
            return

        if action == "set":
            await ctx.send(
                "Unknown field. Use /settings to update the settings, This command has been deprecated."
            )
        else:
            await ctx.send(
                "Unknown action. Use /settings for editing noticeboard settings."
            )


async def setup(bot):
    await bot.add_cog(NoticeBoard(bot))
