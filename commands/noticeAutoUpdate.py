from discord.ext import commands, tasks
import discord
import json
import os
from datetime import datetime, timedelta
import asyncio
import google.generativeai as genai
from modules.setconfig import json_get, check_guild_config_available, edit_json_file
from modules.cache import cache_data, cache_read_latest
from catholic_mass_readings import USCCB, models
from modules.readversion import read_current_version
from modules.enviromentfilegenerator import check_and_load_env_file

# Configure the Gemini API
gemini_api_key = os.getenv("GeminiApiKey")
if gemini_api_key:
    try:
        genai.configure(api_key=gemini_api_key)
    except Exception as e:
        print(
            f"An error occurred while configuring the Gemini API: {str(e)} \n The daily readings module will now be disabled."
        )
        gemini_api_key = None


async def fetch_daily_readings():
    async with USCCB() as usccb:
        mass = await usccb.get_mass(datetime.today().date(), models.MassType.DEFAULT)
        return mass


async def summarize_readings(readings):
    prompt = f"Summarize the following daily readings and provide a motivational quote with a link embed, put the readings in a json format following: link, title, date, motivational_quote, summary_paragraph. \n The motivational quote should be plaintext and should be gotten from the daily readings. The JSON should instantly start with a curly bracket, and not formatted as ```json. The following below is the reading: \n\n{readings}"
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        summary = (
            response.text if response.text else "No valid response from Gemini API."
        )
    except Exception as e:
        summary = f"An error occurred while contacting the Gemini API: {str(e)}"
    return summary


class NoticeAutoUpdate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sent_message_ids = {}
        self.first_start = True
        self.guild_update_info = {}
        self.startup_ping_sent = {}
        self.ping_message_being_updated = {}
        self.daily_readings = None
        self.ping_message_lock = asyncio.Lock()
        self.update_noticeboard.start()
        self.send_ping_message_loop.start()
        self.fetch_daily_readings_task.start()

        check_and_load_env_file()

    @tasks.loop(seconds=3600)
    async def update_noticeboard(self):
        today = datetime.now()
        for guild in self.bot.guilds:
            guild_id = guild.id
            try:
                config = json_get(guild_id)
                if int(os.getenv("DEV_GUILD", 0)) == guild_id:
                    interval = config.get("NoticeBoardUpdateInterval", 3600)
                    self.update_noticeboard.change_interval(seconds=interval)
                pingmessage_edit_id = config.get("pingmessageEditID", None)
                ping_role = config.get("PingRoleId", None)
            except Exception as e:
                print(f"Error getting config for guild {guild_id}: {e}")
                return

            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")
            noticeboard_edit_ids = config.get("noticeboardEditID", [])

            if noticeboard_channel_id == "Default" or noticeboard_channel_id is None:
                return

            if interval == None and os.getenv("DEV_GUILD", 0) == guild_id:
                return

            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                print(f"Channel not found for guild {guild_id}.")
                return

            version = read_current_version()
            new_message_ids = []

            # Fetch task data from the cache or another source
            cache_data("all")
            task_data_str = cache_read_latest("all")

            if not task_data_str:
                print("Error: No task data found in the cache.")
                return

            try:
                task_data = json.loads(task_data_str)
                api_call_time = task_data.get("api-call-time", "Unknown")
            except json.JSONDecodeError:
                print(f"Error: Unable to decode cached data. Raw data: {task_data_str}")
                return

            # Ensure task_data is in the expected format (dict)
            if not isinstance(task_data, dict):
                print(
                    f"Error: Expected task data to be a dictionary but got {type(task_data)}."
                )
                continue

            embeds = [
                self.create_notice_embed(task_data, version),
                self.create_weekly_embed(task_data, version, api_call_time),
                self.create_due_tomorrow_embed(task_data, version),
            ]

            next_update_time = datetime.now() + timedelta(seconds=interval)

            if ping_role is not None and pingmessage_edit_id is not None:
                print(
                    f"Attempting to edit ping message for guild {guild_id} with ID {pingmessage_edit_id}..."
                )

                try:
                    ping_message = await channel.fetch_message(pingmessage_edit_id)

                    # Calculate timestamps
                    ping_daily_time = config.get("PingDailyTime", "15:00")

                    ping_message = await channel.fetch_message(pingmessage_edit_id)
                    new_ping_message_content = await self.send_ping_message(
                        channel,
                        config.get("PingRoleId", "NotSet"),
                        today,
                        self.get_next_ping_time(ping_daily_time),
                        next_update_time,
                        api_call_time,
                    )
                    await ping_message.edit(content=new_ping_message_content)
                    print(f"Ping message edited for guild {guild_id} successfully.")

                except discord.NotFound:
                    print(
                        f"Ping message with ID {pingmessage_edit_id} not found for guild {guild_id}. Skipping edit."
                    )
                except discord.HTTPException as e:
                    print(f"Failed to edit ping message for guild {guild_id}: {e}")
                except Exception as e:
                    print(
                        f"An unexpected error occurred while editing the ping message for guild {guild_id}: {e}"
                    )

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
                        print(
                            f"Noticeboard message with ID {message_id} not found for guild {guild_id}. Sending new message."
                        )
                        try:
                            new_message = await channel.send(embed=embeds[i])
                            new_message_ids.append(new_message.id)
                            print(
                                f"New noticeboard message sent with ID {new_message.id} for guild {guild_id}."
                            )
                        except discord.HTTPException as e:
                            print(
                                f"Failed to send new message for guild {guild_id}: {e}"
                            )
                        break

                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = e.retry_after or 5
                            retries += 1
                            print(
                                f"Rate limited. Retrying after {retry_after} seconds. Attempt {retries}/3."
                            )
                            await asyncio.sleep(retry_after)
                        else:
                            print(
                                f"Failed to edit message ID {message_id} for guild {guild_id}: {e}"
                            )
                            break

            # If there are any message IDs that were not valid, send new messages for those embeds
            for i in range(len(noticeboard_edit_ids), len(embeds)):
                try:
                    new_message = await channel.send(embed=embeds[i])
                    new_message_ids.append(new_message.id)
                    print(
                        f"New noticeboard message sent with ID {new_message.id} for guild {guild_id}."
                    )
                except discord.HTTPException as e:
                    print(f"Failed to send new message for guild {guild_id}: {e}")

            # Update the noticeboardEditID with all valid message IDs
            edit_json_file(guild_id, "noticeboardEditID", new_message_ids)
            print(
                f"Updated noticeboardEditID for guild {guild_id} with new valid message IDs: {new_message_ids}"
            )

    @tasks.loop(hours=24)
    async def fetch_daily_readings_task(self):
        if gemini_api_key:
            try:
                readings = await fetch_daily_readings()
                summary = await summarize_readings(readings)
                summary_json = json.loads(summary)
                self.daily_readings = {
                    "date": datetime.now().strftime("%B %d, %Y"),
                    "motivational_quote": summary_json["motivational_quote"],
                    "summary_paragraph": summary_json["summary_paragraph"],
                    "title": summary_json["title"],
                    "link": summary_json["link"],
                }
            except Exception as e:
                print(f"Error fetching daily readings: {e}")
                self.daily_readings = None
        else:
            self.daily_readings = None

    @fetch_daily_readings_task.before_loop
    async def before_fetch_daily_readings_task(self):
        await self.bot.wait_until_ready()

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
                    break

    @tasks.loop(minutes=10)
    async def send_ping_message_loop(self):
        now = datetime.now()
        today = now.date()
        for guild in self.bot.guilds:
            guild_id = guild.id
            config = json_get(guild_id)
            ping_daily_time = config.get("PingDailyTime", "15:00")
            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")
            interval = config.get("NoticeBoardUpdateInterval", "null")
            if (
                noticeboard_channel_id == "Default"
                or noticeboard_channel_id is None
                or noticeboard_channel_id == "null"
            ):
                print(
                    f"Noticeboard channel ID not set for guild {guild_id}. Skipping update."
                )
                return

            if (
                interval is None
                or interval == "null"
                and os.getenv("DEV_GUILD") == guild_id
            ):
                print(
                    f"Noticeboard update interval not set for guild {guild_id}. Skipping update."
                )
                return

            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                print(f"Channel not found for guild {guild_id}.")
                return

            ping_date_str = config.get("pingDateTime", None)
            if ping_date_str is not None:
                ping_date = datetime.strptime(ping_date_str, "%Y-%m-%d").date()
                if ping_date == today:
                    continue
            else:
                print(
                    "It seems like the pingDateTime is not set. It will now send the ping message and set it up as the date today."
                )

            ping_time = datetime.strptime(ping_daily_time, "%H:%M").time()
            ping_datetime = datetime.combine(today, ping_time)

            if self.first_start:

                await asyncio.sleep(5)
                await self.handle_ping_message(
                    channel, guild_id, today, ping_daily_time, now
                )
                self.first_start = False
                continue
            time_diff = abs((now - ping_datetime).total_seconds() / 60)
            if time_diff > 10:
                continue

            await self.handle_ping_message(
                channel, guild_id, today, ping_daily_time, now
            )

    async def handle_ping_message(self, channel, guild_id, today, ping_daily_time, now):
        print("Handling ping message...")
        """Handles sending the ping message."""
        async with self.ping_message_lock:
            self.ping_message_being_updated[guild_id] = True
            try:
                config = json_get(guild_id)
                if guild_id not in self.sent_message_ids:
                    self.sent_message_ids[guild_id] = {}
                pingmessage_edit_id = config.get("pingmessageEditID", None)
                interval = config.get("NoticeBoardUpdateInterval", 3600)
                next_update_time = now + timedelta(seconds=interval)
                api_call_time = self.guild_update_info.get(guild_id, {}).get(
                    "api_call_time", "Unknown"
                )

                new_ping_message = await self.send_ping_message(
                    channel,
                    config.get("PingRoleId", "NotSet"),
                    today,
                    self.get_next_ping_time(ping_daily_time),
                    next_update_time,
                    api_call_time,
                )
                await asyncio.sleep(1)
                if pingmessage_edit_id is None:
                    print(
                        f"No ping message ID for guild {guild_id}. Sending a new ping message."
                    )
                    new_ping_message = await channel.send(new_ping_message)
                    self.sent_message_ids[guild_id]["ping"] = new_ping_message.id
                    edit_json_file(guild_id, "pingmessageEditID", new_ping_message.id)
                else:
                    await self.delete_message_with_retries(channel, pingmessage_edit_id)
                    new_ping_message = await channel.send(new_ping_message)
                    self.sent_message_ids[guild_id]["ping"] = new_ping_message.id
                    edit_json_file(guild_id, "pingmessageEditID", new_ping_message.id)
                    print(
                        f"New ping message sent for guild {guild_id}, ID: {new_ping_message.id}"
                    )
            finally:
                edit_json_file(guild_id, "pingDateTime", today.strftime("%Y-%m-%d"))
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
                return
            except discord.NotFound:
                print(
                    f"Message ID {message_id} not found for deletion. It may have already been deleted."
                )
                return
            except discord.Forbidden:
                print(
                    f"Bot does not have permission to delete message ID {message_id}."
                )
                return
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = e.retry_after or 60
                    print(
                        f"Rate limit hit while trying to delete message ID {message_id}. Retrying after {retry_after} seconds..."
                    )
                    await asyncio.sleep(retry_after)
                else:
                    print(
                        f"Failed to delete message ID {message_id} due to an error: {e}"
                    )
                    return

    @send_ping_message_loop.before_loop
    async def before_send_ping_message_loop(self):
        await self.bot.wait_until_ready()

    async def send_ping_message(
        self,
        channel,
        ping_role,
        today,
        next_ping_time,
        next_update_time,
        api_call_time,
    ):
        next_update_timestamp = (
            int(next_update_time.timestamp())
            if isinstance(next_update_time, datetime)
            else "N/A"
        )

        # Use the stored daily readings
        daily_readings_info = ""
        if self.daily_readings:
            daily_readings_info = (
                f"- Daily Motivational Quote: {self.daily_readings['motivational_quote']}\n"
                f"      - Read the Full Text here: [{self.daily_readings['title']}](<{self.daily_readings['link']}>)\n"
            )

        ping_message_content = (
            f"# Daily Ping <@&{ping_role}>\n"
            f"- Today's date: {today.strftime('%a, %d %b %Y')}\n"
            f"- Next Refresh in: <t:{next_update_timestamp}:R>\n"
            f"- Next Ping in: <t:{int(next_ping_time.timestamp())}:R>\n"
        )

        if daily_readings_info:
            ping_message_content += daily_readings_info

        return ping_message_content

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
                inline=False,
            )

    def is_valid_date(self, date_str):
        try:
            datetime.strptime(date_str, "%A, %d-%m-%Y")
            return True
        except ValueError:
            return False

    def create_weekly_embed(self, task_data, version, api_call_time):
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        embed = discord.Embed(
            title="This Week's Assignments", color=discord.Color.green()
        )
        embed.set_author(
            name=f"{week_start.strftime('%d %b %Y')} to {week_end.strftime('%d %b %Y')}"
        )
        tasks_found = False

        for date, tasks in task_data.items():
            if date == "Status" or not self.is_valid_date(date):
                continue

            task_date = datetime.strptime(date, "%A, %d-%m-%Y").date()

            # Only include tasks within the current week
            if week_start <= task_date <= week_end:
                days_until_due = (task_date - today).days

                task_datetime = datetime.combine(task_date, datetime.min.time())
                discord_timestamp = f"<t:{int(task_datetime.timestamp())}:R>"

                # Determine the due in format
                due_in = (
                    f"Due in {days_until_due} day"
                    if days_until_due == 1
                    else f"Due in {days_until_due} days"
                )

                formatted_date = task_date.strftime("%d %b %Y")
                task_list = [
                    f"{idx}. {task['subject']} [{task['task']}] - {task['description']}"
                    for idx, task in enumerate(tasks, start=1)
                ]

                if days_until_due >= 0:
                    embed.add_field(
                        name=f"{formatted_date} ({due_in})",
                        value="\n".join(task_list),
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name=formatted_date, value="\n".join(task_list), inline=False
                    )

                tasks_found = True

        if not tasks_found:
            embed.description = "No Assignments this week! 🎉"
        embed.set_footer(text=f"Bot Version: {version}")
        return embed

    def create_notice_embed(self, task_data, version):
        embed = discord.Embed(
            title="Notice Board",
            description="Tasks you have to do",
            color=discord.Color.blue(),
        )
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
            due_in = (
                f"Due in {days_until_due} day"
                if days_until_due == 1
                else f"Due in {days_until_due} days"
            )

            formatted_date = task_date.strftime("%d %b %Y")
            task_list = [
                f"{idx}. {task['subject']} [{task['task']}] - {task['description']}"
                for idx, task in enumerate(tasks, start=1)
            ]

            # Display the date along with due information
            if days_until_due >= 0:
                embed.add_field(
                    name=f"{formatted_date} ({due_in})",
                    value="\n".join(task_list),
                    inline=False,
                )
            else:
                embed.add_field(
                    name=formatted_date, value="\n".join(task_list), inline=False
                )

        if unknown_due_tasks:
            unknown_task_list = [
                f"{idx}. {task['subject']} [{task['task']}] - {task['description']}"
                for idx, task in enumerate(unknown_due_tasks, start=1)
            ]
            embed.add_field(
                name="Due Date Unknown",
                value="\n".join(unknown_task_list),
                inline=False,
            )

        embed.set_footer(text=f"Bot Version: {version}")
        return embed

    def create_due_tomorrow_embed(self, task_data, version):
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        next_due = None
        embed = discord.Embed(
            title="Assignments Due Tomorrow", color=discord.Color.orange()
        )

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
                embed.description = (
                    "**No assignments due tomorrow.** Here's what's coming up next:"
                )
                self.add_task_fields(embed, next_due_tasks)
            else:
                embed.description = "Nice! There are no assignments due tomorrow!"

        embed.set_footer(text=f"Bot Version: {version}")
        return embed

    async def send_initial_messages(self, channel, guild_id):
        try:
            # Create the embeds
            version = read_current_version()
            task_data_str = cache_read_latest("all")
            task_data = json.loads(task_data_str)
            notice_embed = self.create_notice_embed(task_data, version)
            this_week_embed = self.create_weekly_embed(
                task_data, version, datetime.now().strftime("%Y-%m-%d")
            )
            due_tomorrow_embed = self.create_due_tomorrow_embed(task_data, version)

            # Send the noticeboard messages
            notice_message = await channel.send(embed=notice_embed)
            this_week_message = await channel.send(embed=this_week_embed)
            due_tomorrow_message = await channel.send(embed=due_tomorrow_embed)

            # Store the message IDs in an array
            noticeboard_edit_ids = [
                notice_message.id,
                this_week_message.id,
                due_tomorrow_message.id,
            ]
            self.sent_message_ids[guild_id] = {
                "notice": notice_message.id,
                "this_week": this_week_message.id,
                "due_tomorrow": due_tomorrow_message.id,
            }

            # Update the noticeboardEditID in the config
            edit_json_file(guild_id, "noticeboardEditID", noticeboard_edit_ids)

            print(f"Sent new noticeboard messages in guild {guild_id}.")

        except discord.Forbidden:
            print(
                f"Bot does not have permission to send messages in channel {channel.id}."
            )
        except discord.HTTPException as e:
            print(f"HTTP Exception occurred while sending initial messages: {e}")
        except Exception as e:
            print(f"An unexpected error occurred while sending initial messages: {e}")


async def setup(bot):
    await bot.add_cog(NoticeAutoUpdate(bot))
