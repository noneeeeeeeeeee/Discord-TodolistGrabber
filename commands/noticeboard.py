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
            noticeboard_channel_id = json_file.get("NoticeBoardChannelId", "Default")
            noticeboard_update_interval = json_file.get(
                "NoticeBoardUpdateInterval", None
            )
            ping_daily_time = json_file.get("PingDailyTime", "Not Set")

            if noticeboard_update_interval is None:
                noticeboard_update_interval = "Not set"

            if noticeboard_channel_id == "Default":
                noticeboard_channel_id = "Not Set"

            config_data = {
                "NoticeBoardChannelId": noticeboard_channel_id,
                "NoticeBoardUpdateInterval": noticeboard_update_interval,
                "PingDailyTime": ping_daily_time,
            }
            embed = discord.Embed(
                title="Noticeboard Configuration",
                description="Below are the current configurations for the noticeboard. \n Use `!noticeboard set <field>` to modify the settings.",
                color=discord.Color.blue(),
            )

            noticeboard_CId = config_data["NoticeBoardChannelId"]
            embed.add_field(name="Channel", value=f"<#{noticeboard_CId}>", inline=False)
            embed.add_field(
                name="Update Interval (seconds)",
                value=config_data["NoticeBoardUpdateInterval"],
                inline=False,
            )
            embed.add_field(
                name="Daily Ping Time", value=config_data["PingDailyTime"], inline=False
            )

            async def update_callback(interaction):
                if interaction.user != ctx.author:
                    await interaction.response.send_message(
                        "You are not authorized to use this button.", ephemeral=True
                    )
                    return
                await interaction.response.send_message(
                    "Updating noticeboard now...", ephemeral=True, delete_after=10
                )
                # Trigger the update manually
                cog = self.bot.get_cog("NoticeAutoUpdate")
                if cog:
                    await cog.update_noticeboard()

            async def delete_cache_callback(interaction):
                if interaction.user != ctx.author:
                    await interaction.response.send_message(
                        "You are not authorized to use this button.", ephemeral=True
                    )
                    return
                await interaction.response.send_message(
                    "Deleting cache...", ephemeral=True, delete_after=10
                )
                truncate_cache()  # Ensure this function deletes the cache files

            btnview = View(timeout=30)
            update_now = Button(label="🔄 Update Now", style=discord.ButtonStyle.green)
            update_now.callback = update_callback
            delete_cache = Button(label="🗑️ Delete Cache", style=discord.ButtonStyle.red)
            delete_cache.callback = delete_cache_callback
            btnview.add_item(update_now)
            btnview.add_item(delete_cache)

            btnview = discord.ui.View(timeout=60)  # Set the timeout for the buttons

            async def on_timeout():
                for item in btnview.children:
                    item.disabled = True
                try:
                    if ctx.message.author == self.bot.user:
                        await ctx.message.edit(view=btnview)
                    else:
                        print("Cannot edit a message authored by another user.")
                except discord.Forbidden as e:
                    print(f"Failed to edit message: {e}")
                except Exception as e:
                    print(f"An unexpected error occurred: {e}")

            btnview.on_timeout = on_timeout

            await ctx.send("Noticeboard command executed.", view=btnview)

        # Handle the "edit" action
        if action == "set":
            if field == "channelid":
                await ctx.send(
                    "Please send the channel id you would like to set as the NoticeBoard channel (e.g., `1234567890123456789`)."
                )

                def check_channel(m):
                    return m.author == ctx.author and (
                        m.channel_mentions or m.content.isdigit()
                    )

                try:
                    channel_msg = await self.bot.wait_for(
                        "message", check=check_channel, timeout=60.0
                    )
                    if channel_msg.channel_mentions:
                        channel_id = channel_msg.channel_mentions[0].id
                    else:
                        channel_id = int(channel_msg.content)

                    edit_noticeboard_config(guild_id, channel_id)
                    await ctx.send(
                        f"NoticeBoard Channel has been set to <#{channel_id}>."
                    )
                except (ValueError, discord.NotFound):
                    await ctx.send("Invalid channel mentioned or ID provided.")
                except discord.HTTPException:
                    await ctx.send(
                        "Something went wrong while setting the channel. Please try again."
                    )

            elif field == "interval":
                await ctx.send(
                    "Please provide the update interval in seconds for the NoticeBoard."
                )

                def check_interval(m):
                    return m.author == ctx.author and m.content.isdigit()

                try:
                    interval_msg = await self.bot.wait_for(
                        "message", check=check_interval, timeout=60.0
                    )
                    interval = int(interval_msg.content)

                    edit_noticeboard_config(guild_id, None, interval)
                    if os.getenv("DEV_GUILD") == guild_id:
                        await ctx.send(
                            f"NoticeBoard update interval has been set to {interval} seconds."
                        )
                    else:
                        await ctx.send(
                            f"NoticeBoard update interval has been set to {interval} seconds, although it will not take effect, since it is not in the owners guild, if you have an issue with the check time, please contact the bot hoster."
                        )
                except ValueError:
                    await ctx.send(
                        "Invalid interval provided. Please enter a valid number of seconds."
                    )
                except discord.HTTPException:
                    await ctx.send(
                        "Something went wrong while setting the interval. Please try again."
                    )
            elif field == "pingtime":
                await ctx.send(
                    "Please provide the time of day to send the daily ping in 24-hour format (e.g., `18:00`)."
                )

                def check_ping_time(m):
                    return m.author == ctx.author and len(m.content.split(":")) == 2

                try:
                    ping_time_msg = await self.bot.wait_for(
                        "message", check=check_ping_time, timeout=60.0
                    )
                    ping_time = ping_time_msg.content

                    edit_noticeboard_config(guild_id, None, None, ping_time)
                    await ctx.send(f"Daily ping time has been set to {ping_time}.")
                except discord.HTTPException:
                    await ctx.send(
                        "Something went wrong while setting the ping time. Please try again."
                    )
            else:
                await ctx.send(
                    "Unknown field. You can use `set ChannelId` or `set Interval`."
                )
        else:
            await ctx.send(
                "Unknown action. You can use the `edit` action to modify settings."
            )


async def setup(bot):
    await bot.add_cog(NoticeBoard(bot))
