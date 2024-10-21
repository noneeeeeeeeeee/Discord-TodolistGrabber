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
        self.startup_ping_sent = {}
        self.ping_message_being_updated = {} 
        self.ping_message_lock = asyncio.Lock() 
        self.update_noticeboard.start()
        self.send_ping_message_loop.start()



    @tasks.loop(seconds=3600)  # default to 1 hour, or use the NoticeBoardUpdateInterval
    async def update_noticeboard(self):
        today = datetime.now()
        for guild in self.bot.guilds:
            guild_id = guild.id
            try:
                config = json_get(guild_id)
                interval = config.get("NoticeBoardUpdateInterval", 3600)  # Get the update interval
                self.update_noticeboard.change_interval(seconds=interval)  # Change the loop interval
                pingmessage_edit_id = config.get("pingmessageEditID", None)
                ping_role = config.get("PingRoleId", None)
            except Exception as e:
                print(f"Error getting config for guild {guild_id}: {e}")
                continue

            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")
            noticeboard_edit_ids = config.get("noticeboardEditID", [])

            if noticeboard_channel_id == "Default":
                continue

            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                print(f"Channel not found for guild {guild_id}.")
                continue

            version = read_current_version()
            new_message_ids = []

            # Fetch task data from the cache or another source
            cache_data("all")
            task_data_str = cache_read("all")

            if not task_data_str:
                print("Error: No task data found in the cache.")
                continue

            try:
                task_data = json.loads(task_data_str)
                api_call_time = task_data.get("api-call-time", "Unknown")
            except json.JSONDecodeError:
                print(f"Error: Unable to decode cached data. Raw data: {task_data_str}")
                continue

            # Ensure task_data is in the expected format (dict)
            if not isinstance(task_data, dict):
                print(f"Error: Expected task data to be a dictionary but got {type(task_data)}.")
                continue

            embeds = [
                self.create_notice_embed(task_data, version),
                self.create_weekly_embed(task_data, version, api_call_time),
                self.create_due_tomorrow_embed(task_data, version)
            ]
        
            next_update_time = datetime.now() + timedelta(seconds=interval)

            if ping_role is not None and pingmessage_edit_id is not None:
                print(f"Attempting to edit ping message for guild {guild_id} with ID {pingmessage_edit_id}...")

                try:
                    ping_message = await channel.fetch_message(pingmessage_edit_id)

                    # Calculate timestamps
                    next_update_timestamp = int(next_update_time.timestamp())
                    ping_daily_time = config.get("PingDailyTime", "15:00")

                    await ping_message.edit(content=f"# Daily Ping <@&{ping_role}>\n- Today's date: {today.strftime('%a, %d %b %Y')}\n- Next Refresh in: <t:{next_update_timestamp}:R>\n- Next Ping in: <t:{int(self.get_next_ping_time(ping_daily_time).timestamp())}:R> \n- Last API call: {api_call_time}")
                    print(f"Ping message edited for guild {guild_id} successfully.")

                except discord.NotFound:
                    print(f"Ping message with ID {pingmessage_edit_id} not found for guild {guild_id}. Skipping edit.")
                except discord.HTTPException as e:
                    print(f"Failed to edit ping message for guild {guild_id}: {e}")
                except Exception as e:
                    print(f"An unexpected error occurred while editing the ping message for guild {guild_id}: {e}")


            # Process existing noticeboard messages

            for i, message_id in enumerate(noticeboard_edit_ids):
                retries = 0
                while retries < 3:
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.edit(embed=embeds[i])
                        await asyncio.sleep(2) 
                        new_message_ids.append(message_id)
                        break

                    except discord.NotFound:
                        print(f"Noticeboard message with ID {message_id} not found for guild {guild_id}. Sending new message.")
                        try:
                            new_message = await channel.send(embed=embeds[i])  # Send the new message
                            new_message_ids.append(new_message.id)
                            print(f"New noticeboard message sent with ID {new_message.id} for guild {guild_id}.")
                        except discord.HTTPException as e:
                            print(f"Failed to send new message for guild {guild_id}: {e}")
                        break

                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = e.retry_after or 5
                            retries += 1
                            print(f"Rate limited. Retrying after {retry_after} seconds. Attempt {retries}/3.")
                            await asyncio.sleep(retry_after)
                        else:
                            print(f"Failed to edit message ID {message_id} for guild {guild_id}: {e}")
                            break

            # If there are any message IDs that were not valid, send new messages for those embeds
            for i in range(len(noticeboard_edit_ids), len(embeds)):
                try:
                    new_message = await channel.send(embed=embeds[i])
                    new_message_ids.append(new_message.id)
                    print(f"New noticeboard message sent with ID {new_message.id} for guild {guild_id}.")
                except discord.HTTPException as e:
                    print(f"Failed to send new message for guild {guild_id}: {e}")

            # Update the noticeboardEditID with all valid message IDs
            edit_json_file(guild_id, "noticeboardEditID", new_message_ids)
            print(f"Updated noticeboardEditID for guild {guild_id} with new valid message IDs: {new_message_ids}")










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
        now = datetime.now()
        today = now.date()

        for guild in self.bot.guilds:
            guild_id = guild.id
            config = json_get(guild_id)
            ping_daily_time = config.get("PingDailyTime", "15:00")
            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")

            if noticeboard_channel_id == "Default":
                continue

            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                print(f"Channel not found for guild {guild_id}.")
                continue

            # Skip if already sent today
            if self.ping_sent_today.get(guild_id) == today:
                continue

            # Convert PingDailyTime to a datetime object
            ping_time = datetime.strptime(ping_daily_time, "%H:%M").time()
            ping_datetime = datetime.combine(today, ping_time)

            # Logic for sending the first ping when the bot starts
            if self.first_start:
                await asyncio.sleep(5)
                await self.handle_ping_message(channel, guild_id, today, ping_daily_time, now)
                self.first_start = False  # Set to False after first start to prevent duplicate on next checks
                continue  # Skip the time window logic for the first run

            # Check if the current time is within the -10 to +10 minute window around the PingDailyTime
            time_diff = abs((now - ping_datetime).total_seconds() / 60)
            if time_diff > 10:  # Skip if we're outside the 10-minute window
                continue

            # Proceed to send the ping if within the time window
            await self.handle_ping_message(channel, guild_id, today, ping_daily_time, now)

    async def handle_ping_message(self, channel, guild_id, today, ping_daily_time, now):
        """Handles sending or updating the ping message."""
        async with self.ping_message_lock:
            self.ping_message_being_updated[guild_id] = True

            try:
                config = json_get(guild_id)
                if guild_id not in self.sent_message_ids:
                    self.sent_message_ids[guild_id] = {}

                pingmessage_edit_id = config.get("pingmessageEditID", None)
                next_update_time = now + timedelta(seconds=config.get("NoticeBoardUpdateInterval", 3600))
                api_call_time = self.guild_update_info.get(guild_id, {}).get('api_call_time', "Unknown")

                # Send new ping message
                new_ping_message = await self.send_ping_message(
                    channel,
                    config.get("PingRoleId", "NotSet"),
                    today,
                    self.get_next_ping_time(ping_daily_time),
                    next_update_time,
                    api_call_time
                )

                await asyncio.sleep(1)

                if pingmessage_edit_id is None:
                    print(f"No ping message ID for guild {guild_id}. Sending a new ping message.")
                    self.sent_message_ids[guild_id]['ping'] = new_ping_message.id
                    edit_json_file(guild_id, "pingmessageEditID", new_ping_message.id)
                    self.ping_sent_today[guild_id] = today
                    print(f"New ping message sent for guild {guild_id}, ID: {new_ping_message.id}")
                else:
                    # Delete the old ping message and resend the new one
                    await self.delete_message_with_retries(channel, pingmessage_edit_id)
                    self.sent_message_ids[guild_id]['ping'] = new_ping_message.id
                    edit_json_file(guild_id, "pingmessageEditID", new_ping_message.id)
                    self.ping_sent_today[guild_id] = today
                    print(f"New ping message sent for guild {guild_id}, ID: {new_ping_message.id}")
            finally:
                self.ping_message_being_updated[guild_id] = False


    


    




    async def delete_message_with_retries(self, channel, message_id):
        """Attempts to delete a message with retries for handling rate limits."""
        max_retries = 5
        print("Deleting Message With Retries Called!")
        for attempt in range(max_retries):
            print("Deleting message with retries...")
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
                print(f"Successfully deleted message ID {message_id}.")
                return  # Exit if successful
            except discord.NotFound:
                print(f"Message ID {message_id} not found for deletion. It may have already been deleted.")
                return  # Exit if the message was not found
            except discord.Forbidden:
                print(f"Bot does not have permission to delete message ID {message_id}.")
                return  # Exit if permissions are insufficient
            except discord.HTTPException as e:
                if e.status == 429: 
                    retry_after = e.retry_after or 60
                    print(f"Rate limit hit while trying to delete message ID {message_id}. Retrying after {retry_after} seconds...")
                    await asyncio.sleep(retry_after)
                else:
                    print(f"Failed to delete message ID {message_id} due to an error: {e}")
                    return  


    @send_ping_message_loop.before_loop
    async def before_send_ping_message_loop(self):
        await self.bot.wait_until_ready()


    async def send_ping_message(self, channel, ping_role, today, next_ping_time, next_update_time, api_call_time):
        next_update_timestamp = int(next_update_time.timestamp()) if isinstance(next_update_time, datetime) else "N/A"
        ping_message = await channel.send(
            f"# Daily Ping <@&{ping_role}>\n- Today's date: {today.strftime('%a, %d %b %Y')}\n- Next Refresh in: <t:{next_update_timestamp}:R>\n- Next Ping in: <t:{int(next_ping_time.timestamp())}:R> \n- Last API call: {api_call_time}"
        )
        return ping_message


    async def send_or_update_ping_message(self, channel, guild_id):
        config = json_get(guild_id)
        pingmessage_edit_id = config.get("pingmessageEditID", None)

        # Delete the existing ping message if necessary
        if pingmessage_edit_id:
            print(f"Attempting to delete old ping message ID: {pingmessage_edit_id} for guild {guild_id}")
            await self.delete_message_with_retries(channel, pingmessage_edit_id)
        else:
            print(f"No previous ping message to delete for guild {guild_id}.")

        # Send the new ping message
        ping_role = config.get("PingRoleId", "NotSet")
        today = datetime.now().date()
        next_ping_time = self.get_next_ping_time(config.get("PingDailyTime", "15:00"))
        next_update_time = datetime.now() + timedelta(hours=1)
        api_call_time = self.guild_update_info.get(guild_id, {}).get('api_call_time', "Unknown")

        try:
            ping_message = await self.send_ping_message(channel, ping_role, today, next_ping_time, next_update_time, api_call_time)
            self.sent_message_ids[guild_id]['ping'] = ping_message.id
            self.ping_sent_today[guild_id] = today

            # Update the pingmessageEditID in the config
            edit_json_file(guild_id, "pingmessageEditID", ping_message.id)
            print(f"New ping message sent and updated for guild {guild_id}, ID: {ping_message.id}.")
        except discord.Forbidden:
            print(f"Bot does not have permission to send messages in channel {channel.id}.")
        except Exception as e:
            print(f"An unexpected error occurred while sending the ping message: {e}")





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
            if date in ["Status", "api-version", "unknown-due"]: 
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


    async def send_initial_messages(self, channel, guild_id):
        try:
            # Create the embeds
            version = read_current_version()
            task_data_str = cache_read("all")
            task_data = json.loads(task_data_str)
            notice_embed = self.create_notice_embed(task_data, version)
            this_week_embed = self.create_weekly_embed(task_data, version, datetime.now().strftime("%Y-%m-%d"))
            due_tomorrow_embed = self.create_due_tomorrow_embed(task_data, version)

            # Send the noticeboard messages
            notice_message = await channel.send(embed=notice_embed)
            this_week_message = await channel.send(embed=this_week_embed)
            due_tomorrow_message = await channel.send(embed=due_tomorrow_embed)

            # Store the message IDs in an array
            noticeboard_edit_ids = [notice_message.id, this_week_message.id, due_tomorrow_message.id]
            self.sent_message_ids[guild_id] = {
                'notice': notice_message.id,
                'this_week': this_week_message.id,
                'due_tomorrow': due_tomorrow_message.id
            }

            # Update the noticeboardEditID in the config
            edit_json_file(guild_id, "noticeboardEditID", noticeboard_edit_ids)

            print(f"Sent new noticeboard messages in guild {guild_id}.")

        except discord.Forbidden:
            print(f"Bot does not have permission to send messages in channel {channel.id}.")
        except discord.HTTPException as e:
            print(f"HTTP Exception occurred while sending initial messages: {e}")
        except Exception as e:
            print(f"An unexpected error occurred while sending initial messages: {e}")





async def setup(bot):
    await bot.add_cog(NoticeAutoUpdate(bot))
