from discord.ext import commands, tasks
import discord
import json
from datetime import datetime, timedelta
from modules.setconfig import json_get, check_guild_config_available
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

            self.update_noticeboard.change_interval(seconds=noticeboard_update_interval)

            if noticeboard_channel_id == "Default":
                continue

            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                print(f"Channel with ID {noticeboard_channel_id} not found in guild {guild_id}.")
                continue

            next_update_time = self.get_next_update_time(noticeboard_update_interval)
            self.guild_update_info[guild_id] = {'next_update_time': next_update_time}

            print("Fetching task data from cache...")
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

            version = read_current_version()
            notice_embed = self.create_notice_embed(task_data, version)
            this_week_embed = self.create_weekly_embed(task_data, version, api_call_time)
            due_tomorrow_embed = self.create_due_tomorrow_embed(task_data, version)

            if guild_id in self.sent_message_ids:
                try:
                    message_ids = self.sent_message_ids[guild_id]
                    messages = [await channel.fetch_message(msg_id) for msg_id in message_ids.values()]
                    if len(messages) >= 3:
                        await messages[0].edit(embed=notice_embed)
                        await messages[1].edit(embed=this_week_embed)
                        await messages[2].edit(embed=due_tomorrow_embed)
                    else:
                        self.sent_message_ids.pop(guild_id)
                        await self.send_initial_messages(channel, notice_embed, this_week_embed, due_tomorrow_embed, guild_id)
                except discord.NotFound:
                    self.sent_message_ids.pop(guild_id)
                    await self.send_initial_messages(channel, notice_embed, this_week_embed, due_tomorrow_embed, guild_id)
            else:
                await self.send_initial_messages(channel, notice_embed, this_week_embed, due_tomorrow_embed, guild_id)

            # Handle ping message
            ping_role = config.get("PingRoleId", "NotSet")
            next_ping_time = self.get_next_ping_time(config.get("PingDailyTime", "15:00"))

            if self.first_start:
                self.first_start = False
                # Initial ping message
                api_call_time = self.guild_update_info.get(guild_id, {}).get('api_call_time', 'Unknown')
                ping_message = await self.send_ping_message(channel, ping_role, today, next_ping_time, next_update_time, api_call_time)
                self.sent_message_ids[guild_id]['ping'] = ping_message.id
                self.ping_sent_today[guild_id] = today.date()
            else:
                # Update the existing ping message
                ping_message_id = self.sent_message_ids[guild_id].get('ping')
                if ping_message_id:
                    try:
                        ping_message = await channel.fetch_message(ping_message_id)
                        # Update the next update time in the ping message
                        await ping_message.edit(content=f"# Daily Ping <@&{ping_role}>\n"
                                                         f"- Today's date: {today.strftime('%a, %d %b %Y')}\n"
                                                         f"- Next Refresh in: <t:{int(next_update_time.timestamp())}:R>\n"
                                                         f"- Next Ping in: <t:{int(next_ping_time.timestamp())}:R>\n"
                                                         f"- Last API call: {api_call_time}")
                    except discord.NotFound:
                        print(f"Ping message with ID {ping_message_id} not found. It may have been deleted.")


    @tasks.loop(minutes=10)
    async def send_ping_message_loop(self):
        today = datetime.now().date()
        for guild in self.bot.guilds:
            guild_id = guild.id
            config = json_get(guild_id)
            ping_daily_time = config.get("PingDailyTime", "15:00")
            noticeboard_channel_id = config.get("NoticeBoardChannelId", "Default")

            if noticeboard_channel_id == "Default":
                print(f"Channel ID for guild {guild_id} is default; skipping...")
                continue

            channel = guild.get_channel(int(noticeboard_channel_id))
            next_ping_time = self.get_next_ping_time(ping_daily_time)
            next_update_time = self.guild_update_info.get(guild_id, {}).get('next_update_time', datetime.now() + timedelta(hours=1))

            # Create a 10-minute window around the next_ping_time (5 minutes before and after)
            current_time = datetime.now()
            ping_window_start = next_ping_time - timedelta(minutes=5)
            ping_window_end = next_ping_time + timedelta(minutes=5)


            # Check if the ping has already been sent today
            if guild_id in self.ping_sent_today and self.ping_sent_today[guild_id] == today:
                continue

            if ping_window_start <= current_time <= ping_window_end:
                print(f"Sending ping message in guild {guild_id}...")
                ping_role = config.get("PingRoleId", "NotSet")

                # Delete the old ping message if it exists
                if guild_id in self.sent_message_ids and 'ping' in self.sent_message_ids[guild_id]:
                    ping_message_id = self.sent_message_ids[guild_id]['ping']
                    try:
                        ping_message = await channel.fetch_message(ping_message_id)
                        await ping_message.delete()
                        print(f"Deleted old ping message with ID {ping_message_id}")
                    except discord.NotFound:
                        print("Ping message not found, likely already deleted.")
                    except discord.Forbidden:
                        print("Bot does not have permission to delete the message.")
                    except Exception as e:
                        print(f"An unexpected error occurred while deleting the message: {e}")

                # Send the new ping message
                ping_message = await self.send_ping_message(channel, ping_role, today, next_ping_time, next_update_time, api_call_time="Unknown")
                self.sent_message_ids[guild_id]['ping'] = ping_message.id
                self.ping_sent_today[guild_id] = today  # Mark ping as sent for today






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
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        embed = discord.Embed(title="This Week's Assignments", color=discord.Color.green())
        embed.set_author(name=f"{week_start.strftime('%a, %d %b %Y')} to {week_end.strftime('%a, %d %b %Y')}")
        tasks_found = False

        for date, tasks in task_data.items():
            if date == "Status" or not self.is_valid_date(date):
                continue
            task_date = datetime.strptime(date, "%A, %d-%m-%Y")
            if week_start <= task_date <= week_end:
                formatted_date = self.format_discord_time(date)
                task_list = []
                for idx, task in enumerate(tasks, start=1):
                    task_list.append(f"{idx}. {task['subject']} [{task['task']}] - {task['description']}")
                embed.add_field(name=formatted_date, value="\n".join(task_list), inline=False)
                tasks_found = True

        if not tasks_found:
            embed.description = "No Assignments this week! 🎉"
        embed.set_footer(text=f"Bot Version: {version}")
        return embed

    def create_notice_embed(self, task_data, version):
        embed = discord.Embed(title="Notice Board", description="Tasks you have to do", color=discord.Color.blue())
        for date, tasks in task_data.items():
            if date == "Status" or not self.is_valid_date(date):
                continue
            formatted_date = self.format_discord_time(date)
            task_list = []
            for idx, task in enumerate(tasks, start=1):
                task_list.append(f"{idx}. {task['subject']} [{task['task']}] - {task['description']}")
            embed.add_field(name=formatted_date, value="\n".join(task_list), inline=False)
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
