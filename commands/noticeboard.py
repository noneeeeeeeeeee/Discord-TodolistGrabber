import discord
from discord.ext import commands
import os
from modules.enviromentfilegenerator import check_and_load_env_file
from discord.ui import Button, View
from modules.setconfig import (
    check_guild_config_available,
    check_admin_role,
    json_get,
)
from modules.cache import (
    truncate_cache,
)  # Make sure you have this function implemented

check_and_load_env_file()
OWNER_ID = os.getenv("OWNER_ID")


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
                description="Below are the current configurations for the noticeboard. To edit them use /settings category:noticeboard to edit.",
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
            delete_cache = Button(label="🗑️ Delete Cache", style=discord.ButtonStyle.red)

            show_delete_cache = False
            try:
                if OWNER_ID and str(ctx.author.id) == str(OWNER_ID):
                    show_delete_cache = True
            except Exception:
                show_delete_cache = False

            async def delete_cache_callback(interaction):
                if interaction.user != ctx.author:
                    await interaction.response.send_message(
                        "You are not authorized to use this button.", ephemeral=True
                    )
                    return
                await interaction.response.send_message(
                    "Deleting cache...", ephemeral=True
                )
                truncate_cache()

            if show_delete_cache:
                delete_cache.callback = delete_cache_callback
                btnview.add_item(delete_cache)

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
                "Use /settings to update the settings, This command has been deprecated."
            )


async def setup(bot):
    await bot.add_cog(NoticeBoard(bot))
