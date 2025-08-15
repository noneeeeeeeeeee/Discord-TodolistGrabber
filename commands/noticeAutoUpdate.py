from discord.ext import commands, tasks
import discord
import json
import os
from datetime import datetime, timedelta
import asyncio
from modules.setconfig import json_get, check_guild_config_available, edit_json_file
from modules.cache import cache_data, cache_read_latest
from modules.readversion import read_current_version
from modules.enviromentfilegenerator import check_and_load_env_file

# Make readings optional; if module/env not ready, disable silently
try:
    from modules.summarize_readings import (
        fetch_usccb_daily_readings,
        summarize_usccb_readings,
    )

    _HAVE_DAILY_READINGS = True
except Exception:
    _HAVE_DAILY_READINGS = False
    fetch_usccb_daily_readings = None
    summarize_usccb_readings = None

# Configure env early
check_and_load_env_file()
MAIN_GUILD = os.getenv("MAIN_GUILD")


class NoticeAutoUpdate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sent_message_ids = {}
        self.first_start = True
        self.guild_update_info = {}  # store last_update timestamps per guild
        self.startup_ping_sent = {}
        self.ping_message_being_updated = {}
        self.daily_readings = None
        self.ping_message_lock = asyncio.Lock()
        # Track current heartbeat and configure loops
        self.heartbeat_seconds = self.get_global_heartbeat()
        # run loops; initial interval will be adjusted to heartbeat
        self.update_noticeboard.change_interval(seconds=self.heartbeat_seconds)
        self.send_ping_message_loop.change_interval(seconds=self.heartbeat_seconds)
        self.update_noticeboard.start()
        self.send_ping_message_loop.start()
        self.fetch_daily_readings_task.start()

    def get_global_heartbeat(self) -> int:
        """Returns the current global heartbeat (seconds), defaulting to 1800."""
        try:
            if MAIN_GUILD:
                cfg = json_get(int(MAIN_GUILD))
                hb = cfg.get("General", {}).get("GlobalHeartbeat", 1800)
            else:
                hb = 1800
                if getattr(self.bot, "guilds", None):
                    any_guild = next(iter(self.bot.guilds), None)
                    if any_guild:
                        cfg = json_get(any_guild.id)
                        hb = cfg.get("General", {}).get("GlobalHeartbeat", 1800)
            hb = int(hb)
            if hb < 1800:
                hb = 1800
            return hb
        except Exception:
            return 1800

    def ensure_heartbeat_interval(self):
        """Adjust loop intervals if GlobalHeartbeat changed."""
        hb = self.get_global_heartbeat()
        if getattr(self, "heartbeat_seconds", None) != hb:
            self.heartbeat_seconds = hb
            self.update_noticeboard.change_interval(seconds=hb)
            self.send_ping_message_loop.change_interval(seconds=hb)

    def has_assignments_tomorrow(self, task_data: dict) -> bool:
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        target = tomorrow.strftime("%A, %d-%m-%Y")
        return bool(task_data.get("data", {}).get(target, []))

    def has_assignments_this_week(self, task_data: dict) -> bool:
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        for date_str, tasks in task_data.get("data", {}).items():
            if date_str == "unknown-due":
                continue
            try:
                task_date = datetime.strptime(date_str, "%A, %d-%m-%Y").date()
            except ValueError:
                continue
            if week_start <= task_date <= week_end and tasks:
                return True
        return False

    async def _update_noticeboard_for_guild(self, guild: discord.Guild):
        """Run a single noticeboard update for the provided guild."""
        guild_id = guild.id
        try:
            config = json_get(guild_id)
        except Exception as e:
            print(f"Error getting config for guild {guild_id}: {e}")
            return

        nb_cfg = config.get("Noticeboard", {})
        noticeboard_channel_id = nb_cfg.get("ChannelId", "Default")
        noticeboard_edit_ids = nb_cfg.get("NoticeboardEditIDs", [])
        interval_cfg = nb_cfg.get("UpdateInterval", None)

        if noticeboard_channel_id in ("Default", None, "null"):
            print(f"Noticeboard channel ID not set for guild {guild_id}. Skipping.")
            return

        channel = guild.get_channel(int(noticeboard_channel_id))
        if channel is None:
            print(f"Channel not found for guild {guild_id}.")
            return

        version = read_current_version()
        new_message_ids = []

        # Refresh and read tasks
        try:
            cache_data("all")
            task_data_str = cache_read_latest("all")
        except Exception as e:
            print(f"Error refreshing/reading cache for guild {guild_id}: {e}")
            return

        if not task_data_str:
            print("Error: No task data found in the cache.")
            return

        try:
            task_data = json.loads(task_data_str)
            api_call_time = task_data.get("api-call-time", "Unknown")
            # Store last api call time
            self.guild_update_info.setdefault(guild_id, {})[
                "api_call_time"
            ] = api_call_time
        except json.JSONDecodeError:
            print(
                f"Error: Unable to decode cached data. Raw data: {task_data_str[:200]}..."
            )
            return

        if not isinstance(task_data, dict):
            print(
                f"Error: Expected task data to be a dictionary but got {type(task_data)}."
            )
            return

        embeds = [
            self.create_notice_embed(task_data, version),
            self.create_weekly_embed(task_data, version, api_call_time),
            self.create_due_tomorrow_embed(task_data, version),
        ]

        # Edit existing (limit to embeds len)
        for i, message_id in enumerate(noticeboard_edit_ids[: len(embeds)]):
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embeds[i])
                await asyncio.sleep(1)
                new_message_ids.append(message_id)
            except discord.NotFound:
                try:
                    new_message = await channel.send(embed=embeds[i])
                    new_message_ids.append(new_message.id)
                except discord.HTTPException as e:
                    print(f"Failed to send new message for guild {guild_id}: {e}")
            except discord.HTTPException as e:
                print(
                    f"Failed to edit message ID {message_id} for guild {guild_id}: {e}"
                )

        # Send any missing
        for i in range(len(new_message_ids), len(embeds)):
            try:
                new_message = await channel.send(embed=embeds[i])
                new_message_ids.append(new_message.id)
            except discord.HTTPException as e:
                print(f"Failed to send new message for guild {guild_id}: {e}")

        if new_message_ids:
            edit_json_file(guild_id, "Noticeboard.NoticeboardEditIDs", new_message_ids)
        # mark last update time
        self.guild_update_info.setdefault(guild_id, {})[
            "last_update"
        ] = datetime.utcnow()

    async def run_update_noticeboard_once(self, guild_id: int):
        """Public: trigger an immediate update for a single guild."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            print(f"Guild {guild_id} not found for manual update.")
            return
        await self._update_noticeboard_for_guild(guild)

    @tasks.loop(minutes=30)
    async def update_noticeboard(self):
        """
        Runs on GlobalHeartbeat. For each guild, update only if its effective interval has elapsed.
        """
        # Ensure loop interval tracks GlobalHeartbeat dynamically
        self.ensure_heartbeat_interval()

        now = datetime.utcnow()
        for guild in self.bot.guilds:
            try:
                config = json_get(guild.id)
                original_nb_cfg = config.get("Noticeboard", {})
                nb_cfg = original_nb_cfg
                # Support FollowMain
                if original_nb_cfg.get("FollowMain") and MAIN_GUILD:
                    try:
                        main_cfg = json_get(int(MAIN_GUILD))
                        nb_cfg = main_cfg.get("Noticeboard", nb_cfg)
                    except Exception:
                        pass

                # Compute effective interval: floor by GlobalHeartbeat
                heartbeat = self.heartbeat_seconds or self.get_global_heartbeat()
                raw_interval = nb_cfg.get("UpdateInterval", None)
                if isinstance(raw_interval, int):
                    interval = max(raw_interval, heartbeat)
                else:
                    interval = heartbeat

                # Persist bump if this guild's own setting is below the heartbeat (and not following main)
                if not original_nb_cfg.get("FollowMain"):
                    own_raw = original_nb_cfg.get("UpdateInterval", None)
                    if isinstance(own_raw, int) and own_raw < heartbeat:
                        try:
                            edit_json_file(
                                guild.id, "Noticeboard.UpdateInterval", heartbeat
                            )
                        except Exception:
                            pass

                last_update = self.guild_update_info.get(guild.id, {}).get(
                    "last_update"
                )
                if (
                    last_update is None
                    or (now - last_update).total_seconds() >= interval
                ):
                    await self._update_noticeboard_for_guild(guild)
                    await asyncio.sleep(1)
            except Exception:
                continue

    @tasks.loop(minutes=10)
    async def send_ping_message_loop(self):
        """
        Runs on GlobalHeartbeat. Checks each guild whether ping should be sent.
        """
        # Ensure loop interval tracks GlobalHeartbeat dynamically
        self.ensure_heartbeat_interval()

        now = datetime.now()
        today = now.date()
        for guild in self.bot.guilds:
            guild_id = guild.id
            try:
                config = json_get(guild_id)
                nb_cfg = config.get("Noticeboard", {})
                # FollowMain handling
                if nb_cfg.get("FollowMain") and MAIN_GUILD:
                    try:
                        main_cfg = json_get(int(MAIN_GUILD))
                        nb_cfg = main_cfg.get("Noticeboard", nb_cfg)
                    except Exception:
                        pass
            except Exception:
                continue

            ping_daily_time = nb_cfg.get("PingDailyTime", "15:00")
            smart_ping = nb_cfg.get("SmartPingMode", True)
            noticeboard_channel_id = nb_cfg.get("ChannelId", "Default")
            if noticeboard_channel_id in ("Default", None, "null"):
                continue

            # Daily blacklist
            blacklist = set(nb_cfg.get("PingDayBlacklist", []))
            if today.strftime("%A") in blacklist:
                continue

            # Check if ping already sent today
            ping_date_str = nb_cfg.get("PingDate", None)
            if ping_date_str:
                try:
                    if datetime.strptime(ping_date_str, "%Y-%m-%d").date() == today:
                        continue
                except Exception:
                    pass

            # Prepare channel
            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                continue

            # Smart gating: only when enabled
            if smart_ping:
                try:
                    task_data_str = cache_read_latest("all")
                    if not task_data_str:
                        continue
                    task_data = json.loads(task_data_str)
                except Exception:
                    continue

                if not (
                    self.has_assignments_tomorrow(task_data)
                    or self.has_assignments_this_week(task_data)
                ):
                    continue

            # Parse scheduled ping time for today
            try:
                ping_time = datetime.strptime(ping_daily_time, "%H:%M").time()
            except ValueError:
                # invalid time; skip
                continue
            scheduled_dt = datetime.combine(today, ping_time)
            # If scheduled time is in the past (i.e., missed) or now >= scheduled_dt, and not yet pinged => send
            if now >= scheduled_dt:
                await self.handle_ping_message(
                    channel, guild_id, today, ping_daily_time, now
                )

    async def handle_ping_message(self, channel, guild_id, today, ping_daily_time, now):
        """Handles sending or editing the ping message reliably."""
        async with self.ping_message_lock:
            self.ping_message_being_updated[guild_id] = True
            try:
                config = json_get(guild_id)
                nb_cfg = config.get("Noticeboard", {})
                pingmessage_edit_id = nb_cfg.get("PingMessageEditID", None)

                # Use effective interval (floor by GlobalHeartbeat)
                heartbeat = self.heartbeat_seconds or self.get_global_heartbeat()
                raw_interval = nb_cfg.get("UpdateInterval", None)
                interval = (
                    heartbeat
                    if not isinstance(raw_interval, int)
                    else max(raw_interval, heartbeat)
                )
                next_update_time = now + timedelta(seconds=interval)

                api_call_time = self.guild_update_info.get(guild_id, {}).get(
                    "api_call_time", "Unknown"
                )

                new_content = await self.send_ping_message(
                    channel,
                    nb_cfg.get("PingRoleId", "NotSet"),
                    today,
                    self.get_next_ping_time(ping_daily_time),
                    next_update_time,
                    api_call_time,
                )

                if pingmessage_edit_id:
                    try:
                        msg = await channel.fetch_message(pingmessage_edit_id)
                        await self.edit_with_retries(
                            msg, content=new_content, embed=None
                        )
                    except discord.NotFound:
                        msg = await channel.send(new_content)
                        edit_json_file(
                            guild_id, "Noticeboard.PingMessageEditID", msg.id
                        )
                        self.sent_message_ids.setdefault(guild_id, {})["ping"] = msg.id
                else:
                    msg = await channel.send(new_content)
                    edit_json_file(guild_id, "Noticeboard.PingMessageEditID", msg.id)
                    self.sent_message_ids.setdefault(guild_id, {})["ping"] = msg.id
            finally:
                edit_json_file(
                    guild_id, "Noticeboard.PingDate", today.strftime("%Y-%m-%d")
                )
                self.ping_message_being_updated[guild_id] = False

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

        daily_readings_info = ""
        if isinstance(self.daily_readings, dict):
            try:
                title = self.daily_readings.get("title")
                link = self.daily_readings.get("link")
                quote = self.daily_readings.get("motivational_quote")
                if quote:
                    daily_readings_info += f"- Daily Motivational Quote: {quote}\n"
                if title and link:
                    daily_readings_info += (
                        f"      - Read the Full Text here: [{title}](<{link}>)\n"
                    )
            except Exception:
                pass

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

        for date, tasks in task_data.get("data", {}).items():
            if date == "unknown-due" or not self.is_valid_date(date):
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
                    f"{idx}. [{task['task']}] {task['subject']} - {task['description']}"
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
        unknown_due_tasks = task_data.get("data", {}).get("unknown-due", [])

        today = datetime.now().date()  # Get today's date

        for date, tasks in task_data.get("data", {}).items():
            if date == "unknown-due" or not self.is_valid_date(date):
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
                f"{idx}. [{task['task']}] {task['subject']}  - {task['description']}"
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
                f"{idx}. [{task['task']}] {task['subject']} - {task['description']}"
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

        for date, tasks in task_data.get("data", {}).items():
            if date == "unknown-due":
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

            # Store the message IDs in an array (correct path)
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

            # FIX: persist to Noticeboard.NoticeboardEditIDs (not legacy root key)
            edit_json_file(
                guild_id, "Noticeboard.NoticeboardEditIDs", noticeboard_edit_ids
            )

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
