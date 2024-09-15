import discord
from discord.ext import commands
from modules.setconfig import edit_noticeboard_config, check_guild_config_available, check_admin_role, json_get

class NoticeBoard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def noticeboard(self, ctx, action: str = None, field: str = None):
        """Command to configure the noticeboard settings."""
    
        guild_id = ctx.guild.id
        user_id = ctx.author.id

        # Get the user's role IDs
        user_roles = [role.id for role in ctx.author.roles]

        # Check if the guild config is available
        if not check_guild_config_available(guild_id):
            await ctx.send("The noticeboard hasn't been set up yet. Please run the `!setup` command first.")
            return

        # Check if the user has admin role
        if not check_admin_role(guild_id, user_roles):
            await ctx.send("You do not have permission to modify the noticeboard settings.")
            return

        # Normalize action and field (case and space insensitive)
        if action:
            action = action.strip().lower()
        if field:
            field = field.strip().lower()

        # Display current configuration if no action or field is specified
        if not action or not field:
            json_file = json_get(guild_id)
            noticeboard_channel_id = json_file.get('NoticeBoardChannelId', 'Default')
            noticeboard_update_interval = json_file.get('NoticeBoardUpdateInterval', None)
            if noticeboard_update_interval is None:
                noticeboard_update_interval = "Not set"

            if noticeboard_channel_id == "Default":
                noticeboard_channel_id = "Not Set"
            
            config_data = {
                "NoticeBoardChannelId": noticeboard_channel_id,
                "NoticeBoardUpdateInterval": noticeboard_update_interval
            }
            embed = discord.Embed(
                title="Noticeboard Configuration",
                description="Below are the current configurations for the noticeboard.",
                color=discord.Color.blue()
            )
            embed.add_field(name="Channel ID", value=config_data["NoticeBoardChannelId"], inline=False)
            embed.add_field(name="Update Interval (seconds)", value=config_data["NoticeBoardUpdateInterval"], inline=False)
            await ctx.send(embed=embed)
            return

        # Handle the "edit" action
        if action == "set":
            if field == "channelid":
                await ctx.send("Please send the channel id you would like to set as the NoticeBoard channel (e.g., `1234567890123456789`).")

                def check_channel(m):
                    return m.author == ctx.author and (m.channel_mentions or m.content.isdigit())

                try:
                    channel_msg = await self.bot.wait_for('message', check=check_channel, timeout=60.0)
                    if channel_msg.channel_mentions:
                        channel_id = channel_msg.channel_mentions[0].id
                    else:
                        channel_id = int(channel_msg.content)
                    
                    # Edit the noticeboard config for the Channel ID
                    edit_noticeboard_config(guild_id, channel_id=channel_id)
                    await ctx.send(f"NoticeBoard Channel has been set to <#{channel_id}>.")
                except (ValueError, discord.NotFound):
                    await ctx.send("Invalid channel mentioned or ID provided.")
                except discord.HTTPException:
                    await ctx.send("Something went wrong while setting the channel. Please try again.")

            elif field == "interval":
                await ctx.send("Please provide the update interval in seconds for the NoticeBoard.")

                def check_interval(m):
                    return m.author == ctx.author and m.content.isdigit()

                try:
                    interval_msg = await self.bot.wait_for('message', check=check_interval, timeout=60.0)
                    interval = int(interval_msg.content)
                    
                    # Edit the noticeboard config for the Update Interval
                    edit_noticeboard_config(guild_id, update_interval=interval)
                    await ctx.send(f"NoticeBoard update interval has been set to {interval} seconds.")
                except ValueError:
                    await ctx.send("Invalid interval provided. Please enter a valid number of seconds.")
                except discord.HTTPException:
                    await ctx.send("Something went wrong while setting the interval. Please try again.")
            else:
                await ctx.send("Unknown field. You can edit `ChannelId` or `Interval`.")
        else:
            await ctx.send("Unknown action. You can use the `edit` action to modify settings.")

async def setup(bot):
    await bot.add_cog(NoticeBoard(bot))
