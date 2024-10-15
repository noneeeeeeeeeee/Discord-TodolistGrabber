from discord.ext import commands, tasks
import discord
import json
from datetime import datetime, timedelta
import asyncio
from modules.setconfig import json_get, check_guild_config_available, edit_json_file
from modules.cache import cache_data, cache_read
from modules.readversion import read_current_version

class NoticeAutoUpdate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sent_message_ids = {}
        self.first_start = True
        self.ping_sent_today = {}
        self.guild_update_info = {}
        self.update_noticeboard.start()
        self.send_ping_message_loop.start()

    @tasks.loop(hours=1)
    async def update_noticeboard(self):
        today = datetime.now()
        for guild in self.bot.guilds:
            guild_id = guild.id
            if not check_guild_config_available(guild_id):
                continue
            
            config = json_get(guild_id)
            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")
            noticeboard_update_interval = config.get("NoticeBoardUpdateInterval", 3600)
            noticeboard_edit_id = config.get("noticeboardEditID", None)
            pingmessage_edit_id = config.get("pingmessageEditID", None)
    
            if noticeboard_channel_id == "Default":
                continue  # Skip if necessary configurations are missing
            
            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                continue
            
            # Adjust update interval
            self.update_noticeboard.change_interval(seconds=noticeboard_update_interval)
    
            next_update_time = self.get_next_update_time(noticeboard_update_interval)
            self.guild_update_info[guild_id] = {'next_update_time': next_update_time}
    
            cache_data("all")
            task_data_str = cache_read("all")
    
            if not task_data_str:
                await channel.send("Error: No task data found in the cache.")
                continue
            
            try:
                task_data = json.loads(task_data_str)
            except json.JSONDecodeError:
                await channel.send(f"Error: Unable to decode cached data. Raw data: {task_data_str}")
                continue
            
            if not isinstance(task_data, dict):
                await channel.send(f"Error: Expected task data to be a dictionary but got {type(task_data)}. Raw data: {task_data_str}")
                continue
            
            api_call_time = task_data.get("Status", [{}])[0].get("apicalltime", "Unknown")
            self.guild_update_info[guild_id]['api_call_time'] = api_call_time
    
            # Create embeds
            version = read_current_version()
            notice_embed = self.create_notice_embed(task_data, version)
            this_week_embed = self.create_weekly_embed(task_data, version, api_call_time)
            due_tomorrow_embed = self.create_due_tomorrow_embed(task_data, version)
    
            # Update noticeboard message
            if noticeboard_edit_id:
                try:
                    notice_message = await channel.fetch_message(noticeboard_edit_id)
                    await self.edit_with_retries(notice_message, embed=notice_embed)
                    self.sent_message_ids[guild_id] = {'notice': notice_message.id}
                except discord.NotFound:
                    await self.send_initial_messages(channel, notice_embed, this_week_embed, due_tomorrow_embed, guild_id)
            else:
                await self.send_initial_messages(channel, notice_embed, this_week_embed, due_tomorrow_embed, guild_id)
    
            # Attempt to edit the existing ping message with rate limit handling
            if pingmessage_edit_id:
                try:
                    ping_message = await channel.fetch_message(pingmessage_edit_id)
                    # Update the content of the ping message
                    next_update_timestamp = int(next_update_time.timestamp())
                    next_ping_time = self.get_next_ping_time(config.get("PingDailyTime", "15:00"))
                    next_ping_timestamp = int(next_ping_time.timestamp())
    
                    await self.edit_with_retries(ping_message, content=f"# Daily Ping <@&{config.get('PingRoleId', 'NotSet')}>\n"
                                                                       f"- Today's date: {today.strftime('%a, %d %b %Y')}\n"
                                                                       f"- Next Refresh in: <t:{next_update_timestamp}:R>\n"
                                                                       f"- Next Ping in: <t:{next_ping_timestamp}:R>\n"
                                                                       f"- Last API call: {api_call_time}")
                except discord.NotFound:
                    # If the ping message is not found, send a new one
                    print(f"Ping message not found for guild {guild_id}, sending a new one.")
                    ping_role = config.get("PingRoleId", "NotSet")
                    new_ping_message = await self.send_ping_message(channel, ping_role, today, next_ping_time, next_update_time)
                    self.sent_message_ids[guild_id]['ping'] = new_ping_message.id
                    # Update the pingmessageEditID in the config
                    edit_json_file(guild_id, "pingmessageEditID", new_ping_message.id)
                except Exception as e:
                    print(f"An error occurred while updating the ping message: {e}")
    
            # Save message IDs to the config using `edit_json_file`
            edit_json_file(guild_id, "noticeboardEditID", self.sent_message_ids[guild_id]['notice'])
    
    async def edit_with_retries(self, message, **kwargs):
        """Attempts to edit a message with retries for handling rate limits."""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                await message.edit(**kwargs)
                break  # Break if successful
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limit error
                    retry_after = e.retry_after or 60  # Wait time from error response
                    print(f"Rate limit hit. Retrying after {retry_after} seconds...")
                    await asyncio.sleep(retry_after)  # Wait before retrying
                else:
                    print(f"Failed to edit message due to an error: {e}")
                    break  # Break on any other error
                




    @tasks.loop(minutes=10)
    async def send_ping_message_loop(self):
        today = datetime.now().date()
        for guild in self.bot.guilds:
            guild_id = guild.id
            config = json_get(guild_id)
            ping_daily_time = config.get("PingDailyTime", "15:00")
            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")
            pingmessage_edit_id = config.get("pingmessageEditID", None)

            if noticeboard_channel_id == "Default":
                continue
            
            channel = guild.get_channel(int(noticeboard_channel_id))
            next_ping_time = self.get_next_ping_time(ping_daily_time)

            api_call_time = self.guild_update_info.get(guild_id, {}).get('api_call_time', "Unknown")
            next_update_time = self.guild_update_info.get(guild_id, {}).get('next_update_time', datetime.now() + timedelta(hours=1))

            current_time = datetime.now()
            ping_window_start = next_ping_time - timedelta(minutes=5)
            ping_window_end = next_ping_time + timedelta(minutes=5)

            # Check if a ping message was sent today
            if guild_id in self.ping_sent_today and self.ping_sent_today[guild_id] == today:
                print(f"Already sent ping message today for guild {guild_id}.")
                continue
            
            # Check the ping window
            if ping_window_start <= current_time <= ping_window_end:
                ping_role = config.get("PingRoleId", "NotSet")
                print(f"Sending ping message for guild {guild_id}.")

                # Delete old ping message if exists
                if pingmessage_edit_id:
                    try:
                        ping_message = await channel.fetch_message(pingmessage_edit_id)
                        await ping_message.delete()
                    except discord.NotFound:
                        print(f"Old ping message not found for guild {guild_id}, likely already deleted.")
                    except discord.Forbidden:
                        print(f"Bot does not have permission to delete the ping message in guild {guild_id}.")
                    except Exception as e:
                        print(f"An unexpected error occurred while deleting the ping message: {e}")

                # Send new ping message and update pingmessageEditID
                max_retries = 5
                for attempt in range(1, max_retries + 1):
                    try:
                        ping_message = await self.send_ping_message(channel, ping_role, today, next_ping_time, next_update_time, api_call_time)
                        self.sent_message_ids[guild_id]['ping'] = ping_message.id
                        self.ping_sent_today[guild_id] = today

                        # Update the pingmessageEditID in the config
                        edit_json_file(guild_id, "pingmessageEditID", ping_message.id)
                        break

                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = e.retry_after or 60
                            await asyncio.sleep(retry_after)
                        else:
                            break
                    except discord.Forbidden:
                        break
                    except discord.NotFound:
                        break
                    except Exception as e:
                        break
            else:
                print(f"Not within the ping window for guild {guild_id}.")

    async def refresh_daily_ping_message_on_startup(self):
        today = datetime.now().date()
        for guild in self.bot.guilds:
            guild_id = guild.id
            config = json_get(guild_id)
            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")
            pingmessage_edit_id = config.get("pingmessageEditID", None)

            if noticeboard_channel_id == "Default":
                continue

            channel = guild.get_channel(int(noticeboard_channel_id))

            # If the ping message exists, delete it
            if pingmessage_edit_id:
                try:
                    old_ping_message = await channel.fetch_message(pingmessage_edit_id)
                    await old_ping_message.delete()
                    print(f"Deleted old ping message for guild {guild_id}.")
                except discord.NotFound:
                    print(f"Old ping message not found for guild {guild_id}.")
                except discord.Forbidden:
                    print(f"Bot does not have permission to delete old ping message in guild {guild_id}.")
                except Exception as e:
                    print(f"An error occurred while deleting the old ping message: {e}")

            # Send a new ping message on startup
            try:
                ping_role = config.get("PingRoleId", "NotSet")
                next_ping_time = self.get_next_ping_time(config.get("PingDailyTime", "15:00"))
                next_update_time = self.get_next_update_time(3600)  # Example interval for updates

                new_ping_message = await self.send_ping_message(channel, ping_role, today, next_ping_time, next_update_time)
                self.sent_message_ids[guild_id]['ping'] = new_ping_message.id
                self.ping_sent_today[guild_id] = today

                # Update the pingmessageEditID in the config
                edit_json_file(guild_id, "pingmessageEditID", new_ping_message.id)
                print(f"Updated ping message for guild {guild_id}.")
            except discord.Forbidden:
                print(f"Bot does not have permission to send messages in channel {channel.id}.")
            except Exception as e:
                print(f"An unexpected error occurred while sending the ping message: {e}")



    # This will trigger the initial ping message when the bot starts.
    @send_ping_message_loop.before_loop
    async def before_send_ping_message_loop(self):
        await self.bot.wait_until_ready()
        await self.send_initial_ping_message_on_startup()

    # Method to handle the immediate ping on startup
    async def send_initial_ping_message_on_startup(self):
        today = datetime.now().date()
        for guild in self.bot.guilds:
            guild_id = guild.id
            config = json_get(guild_id)
            ping_daily_time = config.get("PingDailyTime", "15:00")
            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")

            if noticeboard_channel_id == "Default":
                continue

            channel = guild.get_channel(int(noticeboard_channel_id))
            next_ping_time = self.get_next_ping_time(ping_daily_time)
            api_call_time = self.guild_update_info.get(guild_id, {}).get('api_call_time', "Unknown")
            next_update_time = self.guild_update_info.get(guild_id, {}).get('next_update_time', datetime.now() + timedelta(hours=1))

            if guild_id not in self.ping_sent_today or self.ping_sent_today[guild_id] != today:
                ping_role = config.get("PingRoleId", "NotSet")
                print(f"Sending initial startup ping message for guild {guild_id}.")

                try:
                    ping_message = await self.send_ping_message(channel, ping_role, today, next_ping_time, next_update_time, api_call_time)
                    self.sent_message_ids[guild_id] = {'ping': ping_message.id}
                    self.ping_sent_today[guild_id] = today

                    # Update the pingmessageEditID in the config
                    edit_json_file(guild_id, "pingmessageEditID", ping_message.id)

                except discord.Forbidden:
                    print(f"Bot does not have permission to send messages in channel {channel.id}.")
                except Exception as e:
                    print(f"An unexpected error occurred while sending the initial ping message: {e}")



    async def send_ping_message(self, channel, ping_role, today, next_ping_time, next_update_time, api_call_time):
        next_update_timestamp = int(next_update_time.timestamp()) if isinstance(next_update_time, datetime) else "N/A"
        ping_message = await channel.send(
            f"# Daily Ping <@&{ping_role}>\n- Today's date: {today.strftime('%a, %d %b %Y')}\n- Next Refresh in: <t:{next_update_timestamp}:R>\n- Next Ping in: <t:{int(next_ping_time.timestamp())}:R> \n- Last API call: {api_call_time}"
        )
        return ping_message

    @update_noticeboard.before_loop
    async def before_update_noticeboard(self):
        await self.bot.wait_until_ready()
        print("Bot is ready and before_loop is complete.")

    def get_next_update_time(self, interval_seconds):
        current_time = datetime.now()
        next_update = current_time + timedelta(seconds=interval_seconds)
        return next_update

    def get_next_ping_time(self, ping_daily_time):
        today = datetime.now()
        target_time_str = f"{today.strftime('%Y-%m-%d')} {ping_daily_time}"
        next_ping_time = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M")
        if next_ping_time < today:
            next_ping_time += timedelta(days=1)
        return next_ping_time

    def add_task_fields(self, embed, tasks):
        for idx, task in enumerate(tasks, start=1):
            embed.add_field(
                name=f"{task['subject']} [{task['task']}] ",
                value=f"{task['description']}",
                inline=False
            )

    def is_valid_date(self, date_str):
        try:
            datetime.strptime(date_str, "%A, %d-%m-%Y")
            return True
        except ValueError:
            return False

    def create_weekly_embed(self, task_data, version, api_call_time):
        today = datetime.now().date()  # Get today's date
        week_start = today - timedelta(days=today.weekday())  # Start of the week
        week_end = week_start + timedelta(days=6)  # End of the week
        embed = discord.Embed(title="This Week's Assignments", color=discord.Color.green())
        embed.set_author(name=f"{week_start.strftime('%d %b %Y')} to {week_end.strftime('%d %b %Y')}")
        tasks_found = False

        for date, tasks in task_data.items():
            if date == "Status" or not self.is_valid_date(date):
                continue

            # Parse the task date and calculate the difference in days
            task_date = datetime.strptime(date, "%A, %d-%m-%Y").date()
            days_until_due = (task_date - today).days

            # Convert date to datetime for timestamp calculation
            task_datetime = datetime.combine(task_date, datetime.min.time())
            discord_timestamp = f"<t:{int(task_datetime.timestamp())}:R>"  # Use the combined datetime to get the timestamp

            # Determine the due in format
            due_in = f"Due in {days_until_due} day" if days_until_due == 1 else f"Due in {days_until_due} days"

            formatted_date = task_date.strftime('%d %b %Y')
            task_list = [f"{idx}. {task['subject']} [{task['task']}] - {task['description']}" for idx, task in enumerate(tasks, start=1)]

            # Display the date along with due information
            if days_until_due >= 0:
                embed.add_field(name=f"{formatted_date} ({due_in})", value="\n".join(task_list), inline=False)
            else:
                embed.add_field(name=formatted_date, value="\n".join(task_list), inline=False)

            tasks_found = True

        if not tasks_found:
            embed.description = "No Assignments this week! 🎉"
        embed.set_footer(text=f"Bot Version: {version}")
        return embed

    def create_notice_embed(self, task_data, version):
        embed = discord.Embed(title="Notice Board", description="Tasks you have to do", color=discord.Color.blue())
        unknown_due_tasks = []

        today = datetime.now().date()  # Get today's date

        for date, tasks in task_data.items():
            if date == "Status" or not self.is_valid_date(date):
                if date == "unknown-due":
                    unknown_due_tasks = tasks
                continue

            # Parse task date and calculate "Due in X day(s)"
            task_date = datetime.strptime(date, "%A, %d-%m-%Y").date()
            days_until_due = (task_date - today).days

            # Convert date to datetime for timestamp calculation
            task_datetime = datetime.combine(task_date, datetime.min.time())
            discord_timestamp = f"<t:{int(task_datetime.timestamp())}:R>"  # Use the combined datetime to get the timestamp

            # Determine the due in format
            due_in = f"Due in {days_until_due} day" if days_until_due == 1 else f"Due in {days_until_due} days"

            formatted_date = task_date.strftime('%d %b %Y')
            task_list = [f"{idx}. {task['subject']} [{task['task']}] - {task['description']}" for idx, task in enumerate(tasks, start=1)]

            # Display the date along with due information
            if days_until_due >= 0:
                embed.add_field(name=f"{formatted_date} ({due_in})", value="\n".join(task_list), inline=False)
            else:
                embed.add_field(name=formatted_date, value="\n".join(task_list), inline=False)

        if unknown_due_tasks:
            unknown_task_list = [f"{idx}. {task['subject']} [{task['task']}] - {task['description']}" for idx, task in enumerate(unknown_due_tasks, start=1)]
            embed.add_field(name="Due Date Unknown", value="\n".join(unknown_task_list), inline=False)

        embed.set_footer(text=f"Bot Version: {version}")
        return embed





    

    def create_due_tomorrow_embed(self, task_data, version):
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        next_due = None
        embed = discord.Embed(title="Assignments Due Tomorrow", color=discord.Color.orange())
    
        for date, tasks in task_data.items():
            if date == "Status" or date == "api-version":
                continue
            try:
                task_date = datetime.strptime(date, "%A, %d-%m-%Y")
            except ValueError:
                print(f"Skipping invalid date format: {date}")
                continue
            
            if task_date.date() == tomorrow.date():
                embed.set_author(name=f"Due on {tomorrow.strftime('%a, %d %b %Y')}")
                self.add_task_fields(embed, tasks)
                break
            elif task_date > tomorrow and next_due is None:
                next_due = task_date, tasks
    
        if not embed.fields:
            if next_due:
                next_due_date, next_due_tasks = next_due
                embed.title = f"Assignments Due <t:{int(next_due_date.timestamp())}:R>"
                embed.description = "**No assignments due tomorrow.** Here's what's coming up next:"
                self.add_task_fields(embed, next_due_tasks)
            else:
                embed.description = "Nice! There are no assignments due tomorrow!"
    
        embed.set_footer(text=f"Bot Version: {version}")
        return embed
    
    def format_discord_time(self, date_str):
        date_obj = datetime.strptime(date_str, "%A, %d-%m-%Y")
        return date_obj.strftime("%d %b %Y")

    async def send_initial_messages(self, channel, notice_embed, this_week_embed, due_tomorrow_embed, guild_id):
        try:
            notice_message = await channel.send(embed=notice_embed)
            self.sent_message_ids[guild_id] = {'notice': notice_message.id}

            this_week_message = await channel.send(embed=this_week_embed)
            self.sent_message_ids[guild_id]['this_week'] = this_week_message.id

            due_tomorrow_message = await channel.send(embed=due_tomorrow_embed)
            self.sent_message_ids[guild_id]['due_tomorrow'] = due_tomorrow_message.id

            print(f"Sent initial messages in guild {guild_id}.")
        except discord.Forbidden:
            print(f"Bot does not have permission to send messages in channel {channel.id}.")
        except discord.HTTPException as e:
            print(f"HTTP Exception occurred while sending initial messages: {e}")
        except Exception as e:
            print(f"An unexpected error occurred while sending initial messages: {e}")

async def setup(bot):
    await bot.add_cog(NoticeAutoUpdate(bot))
