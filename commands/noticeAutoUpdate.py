from discord.ext import commands, tasks
import discord
import json
import os
from datetime import datetime, timedelta
import asyncio
from zoneinfo import ZoneInfo
from modules.setconfig import json_get, edit_json_file
from modules.cache import cache_data, cache_read_latest
from modules.readversion import read_current_version
from modules.enviromentfilegenerator import check_and_load_env_file

# Daily readings disabled

# Configure env early
check_and_load_env_file()
MAIN_GUILD = os.getenv("MAIN_GUILD")
LOCAL_TZ_NAME = (
    os.getenv("LOCAL_TZ") or os.getenv("LOCAL_REGION") or os.getenv("TIMEZONE") or "UTC"
)


class NoticeAutoUpdate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sent_message_ids = {}
        self.guild_update_info = {}
        self.ping_message_being_updated = {}
        self.ping_message_lock = asyncio.Lock()
        self.ping_last_refreshed_ts: dict[int, str] = {}
        self.heartbeat_seconds = self.get_global_heartbeat()
        # Debug: startup info
        self._dbg(
            f"Initialized. Heartbeat={self.heartbeat_seconds}s, TZ={LOCAL_TZ_NAME}"
        )

    def _dbg(self, msg: str):
        """Lightweight debug print with local timestamp."""
        try:
            ts = self.local_now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[NoticeAutoUpdate {ts}] {msg}")

    def _local_tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(LOCAL_TZ_NAME or "UTC")
        except Exception:
            return ZoneInfo("UTC")

    def local_now(self) -> datetime:
        return datetime.now(self._local_tz())

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
        """Adjust internal state to current GlobalHeartbeat."""
        hb = self.get_global_heartbeat()
        if getattr(self, "heartbeat_seconds", None) != hb:
            self._dbg(
                f"Heartbeat interval change detected: {getattr(self, 'heartbeat_seconds', None)} -> {hb}"
            )
            self.heartbeat_seconds = hb

    def _effective_interval(self, nb_cfg: dict) -> int:
        """Return effective update interval (max of per-guild UpdateInterval and global heartbeat)."""
        raw = nb_cfg.get("UpdateInterval", None)
        hb = self.heartbeat_seconds or self.get_global_heartbeat()
        try:
            return max(int(raw), hb) if isinstance(raw, int) else hb
        except Exception:
            return hb

    def has_assignments_tomorrow(self, task_data: dict) -> bool:
        today = self.local_now().date()
        tomorrow = today + timedelta(days=1)
        target = tomorrow.strftime("%A, %d-%m-%Y")
        return bool(task_data.get("data", {}).get(target, []))

    def has_assignments_this_week(self, task_data: dict) -> bool:
        today = self.local_now().date()
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
        self._dbg(f"[NB] Begin update for guild={guild_id}")
        try:
            config = json_get(guild_id)
        except Exception as e:
            self._dbg(f"[NB] Config load failed for guild={guild_id}: {e}")
            print(f"Error getting config for guild {guild_id}: {e}")
            return

        nb_cfg = config.get("Noticeboard", {})
        noticeboard_channel_id = nb_cfg.get("ChannelId", "Default")
        noticeboard_edit_ids = nb_cfg.get("NoticeboardEditIDs", [])

        if noticeboard_channel_id in ("Default", None, "null"):
            self._dbg(
                f"[NB] Skipping: Noticeboard.ChannelId not set for guild={guild_id}"
            )
            print(f"Noticeboard channel ID not set for guild {guild_id}. Skipping.")
            return

        channel = guild.get_channel(int(noticeboard_channel_id))
        if channel is None:
            print(f"Channel not found for guild {guild_id}.")
            return

        version = read_current_version()
        last_update_local = self.local_now()
        new_message_ids = []
        success_count = 0

        # Refresh and read tasks robustly
        task_data_str = None
        try:
            # Try to refresh cache; if it fails, continue with last known cache
            cache_data("all")
        except Exception as e:
            self._dbg(
                f"[NB] Cache refresh failed for guild={guild_id}: {e}. Will attempt last cache."
            )
            print(
                f"[NoticeUpdate] Refresh failed for guild {guild_id}: {e}. Using last cache."
            )
        try:
            task_data_str = cache_read_latest("all")
        except Exception as e:
            self._dbg(f"[NB] Cache read failed for guild={guild_id}: {e}")
            print(f"[NoticeUpdate] Failed reading cache for guild {guild_id}: {e}")
            return
        if not task_data_str:
            self._dbg(f"[NB] No cache available for guild={guild_id}")
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
            self._dbg(
                f"[NB] Invalid cached JSON for guild={guild_id}, skipping this tick."
            )
            print(
                "[NoticeUpdate] Invalid cached JSON; skipping this heartbeat for noticeboard."
            )
            return

        if not isinstance(task_data, dict):
            print(
                f"Error: Expected task data to be a dictionary but got {type(task_data)}."
            )
            return

        embeds = [
            self.create_notice_embed(task_data, version, last_update_local),
            self.create_weekly_embed(
                task_data, version, api_call_time, last_update_local
            ),
            self.create_due_tomorrow_embed(task_data, version, last_update_local),
        ]

        # Edit existing (limit to embeds len)
        for i, message_id in enumerate(noticeboard_edit_ids[: len(embeds)]):
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embeds[i])
                self._dbg(
                    f"[NB] Edited noticeboard message idx={i} id={message_id} guild={guild_id}"
                )
                await asyncio.sleep(1)
                new_message_ids.append(message_id)
                success_count += 1
            except discord.NotFound:
                try:
                    new_message = await channel.send(embed=embeds[i])
                    self._dbg(
                        f"[NB] Re-sent missing noticeboard message idx={i} new_id={new_message.id} guild={guild_id}"
                    )
                    new_message_ids.append(new_message.id)
                    success_count += 1
                except discord.HTTPException as e:
                    self._dbg(
                        f"[NB] Send failed for noticeboard idx={i} guild={guild_id}: {e}"
                    )
                    print(f"Failed to send new message for guild {guild_id}: {e}")
            except discord.HTTPException as e:
                # Fallback: try sending a new message if edit failed
                try:
                    new_message = await channel.send(embed=embeds[i])
                    self._dbg(
                        f"[NB] Edit failed; sent new message idx={i} new_id={new_message.id} guild={guild_id}"
                    )
                    new_message_ids.append(new_message.id)
                    success_count += 1
                except discord.HTTPException as ee:
                    self._dbg(f"[NB] Edit+send failed idx={i} guild={guild_id}: {ee}")
                    print(f"Failed to edit/send message for guild {guild_id}: {ee}")

        # Send any missing (based on collected successes)
        for i in range(len(new_message_ids), len(embeds)):
            try:
                new_message = await channel.send(embed=embeds[i])
                new_message_ids.append(new_message.id)
                success_count += 1
            except discord.HTTPException as e:
                print(f"Failed to send new message for guild {guild_id}: {e}")

        # If none succeeded, attempt a full re-post of all 3 embeds
        if success_count == 0:
            try:
                forced_ids = []
                for i, emb in enumerate(embeds):
                    msg = await channel.send(embed=emb)
                    forced_ids.append(msg.id)
                new_message_ids = forced_ids
                success_count = len(forced_ids)
                self._dbg(f"[NB] Forced re-post of all embeds guild={guild_id}")
            except Exception as e:
                self._dbg(f"[NB] Forced re-post failed guild={guild_id}: {e}")

        # Persist IDs only if we have all embeds, otherwise keep current IDs
        if len(new_message_ids) == len(embeds):
            try:
                edit_json_file(
                    guild_id, "Noticeboard.NoticeboardEditIDs", new_message_ids
                )
                self._dbg(
                    f"[NB] Persisted NoticeboardEditIDs count={len(new_message_ids)} guild={guild_id}"
                )
            except Exception:
                pass
        # Update "last_update" and LastUpdateTs only on success
        if success_count > 0:
            self.guild_update_info.setdefault(guild_id, {})[
                "last_update"
            ] = last_update_local
            try:
                edit_json_file(
                    guild_id,
                    "Noticeboard.LastUpdateTs",
                    last_update_local.isoformat(),
                )
            except Exception:
                pass
        self._dbg(f"[NB] Completed update for guild={guild_id}")

    async def run_update_noticeboard_once(self, guild_id: int):
        """Public: trigger an immediate update for a single guild."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            print(f"Guild {guild_id} not found for manual update.")
            return
        await self._update_noticeboard_for_guild(guild)

    # Public: called by GlobalHeartbeat to process one heartbeat tick for updates
    async def process_noticeboard_tick(self):
        self.ensure_heartbeat_interval()
        # Use local-aware timestamps consistently
        now = self.local_now()
        for guild in self.bot.guilds:
            try:
                config = json_get(guild.id)
                original_nb_cfg = config.get("Noticeboard", {})
                nb_cfg = original_nb_cfg
                if original_nb_cfg.get("FollowMain") and MAIN_GUILD:
                    try:
                        main_cfg = json_get(int(MAIN_GUILD))
                        nb_cfg = main_cfg.get("Noticeboard", nb_cfg)
                    except Exception:
                        pass

                interval = self._effective_interval(nb_cfg)

                if not original_nb_cfg.get("FollowMain"):
                    own_raw = original_nb_cfg.get("UpdateInterval", None)
                    if isinstance(own_raw, int) and own_raw < (
                        self.heartbeat_seconds or self.get_global_heartbeat()
                    ):
                        try:
                            edit_json_file(
                                guild.id,
                                "Noticeboard.UpdateInterval",
                                self.heartbeat_seconds,
                            )
                        except Exception:
                            pass

                last_update = self.guild_update_info.get(guild.id, {}).get(
                    "last_update"
                )
                # Normalize any previously stored naive datetime to local-aware
                if last_update is not None and last_update.tzinfo is None:
                    last_update = last_update.replace(tzinfo=self._local_tz())
                if (
                    last_update is None
                    or (now - last_update).total_seconds() >= interval
                ):

                    await self._update_noticeboard_for_guild(guild)
                    await asyncio.sleep(1)
                else:
                    remaining = interval - (now - last_update).total_seconds()
                    self._dbg(
                        f"[HB] Skipping NB update guild={guild.id}; {int(remaining)}s remaining"
                    )
            except Exception as e:
                self._dbg(f"[HB] NB tick error guild={getattr(guild,'id','?')}: {e}")
                continue

    async def process_ping_tick(self):
        self.ensure_heartbeat_interval()
        now = self.local_now()
        today = now.date()
        for guild in self.bot.guilds:
            guild_id = guild.id
            try:
                config = json_get(guild_id)
                nb_cfg = config.get("Noticeboard", {})
                if nb_cfg.get("FollowMain") and MAIN_GUILD:
                    try:
                        main_cfg = json_get(int(MAIN_GUILD))
                        nb_cfg = main_cfg.get("Noticeboard", nb_cfg)
                    except Exception:
                        pass
            except Exception as e:
                self._dbg(f"[HB] Ping tick: config error guild={guild_id}: {e}")
                continue

            ping_daily_time = nb_cfg.get("PingDailyTime", "15:00")
            smart_ping = nb_cfg.get("SmartPingMode", True)
            noticeboard_channel_id = nb_cfg.get("ChannelId", "Default")
            if noticeboard_channel_id in ("Default", None, "null"):
                self._dbg(f"[Ping] Skip: no channel set guild={guild_id}")
                continue

            # Refresh/edit existing ping content on LastUpdateTs (no re-ping)
            try:
                last_update_iso = nb_cfg.get("LastUpdateTs")
                pingmessage_edit_id = nb_cfg.get("PingMessageEditID", None)
                if last_update_iso and pingmessage_edit_id:
                    if self.ping_last_refreshed_ts.get(guild_id) != last_update_iso:
                        self._dbg(
                            f"[Ping] Refreshing ping content due to LastUpdateTs change guild={guild_id}"
                        )
                        channel = guild.get_channel(int(noticeboard_channel_id))
                        if channel is not None:
                            try:
                                msg = await channel.fetch_message(pingmessage_edit_id)
                                interval = self._effective_interval(nb_cfg)
                                new_content = await self.send_ping_message(
                                    channel,
                                    nb_cfg.get("PingRoleId", "NotSet"),
                                    today,
                                    self.get_next_ping_time(ping_daily_time),
                                    now + timedelta(seconds=interval),
                                    self.guild_update_info.get(guild_id, {}).get(
                                        "api_call_time", "Unknown"
                                    ),
                                )
                                await self.edit_with_retries(
                                    msg, content=new_content, embed=None
                                )
                                self.ping_last_refreshed_ts[guild_id] = last_update_iso
                            except (discord.NotFound, discord.HTTPException):
                                pass
            except Exception:
                pass

            # Blacklist day check
            bl_raw = nb_cfg.get("PingDayBlacklist", None)
            blacklist = set(bl_raw or [])
            if today.strftime("%A") in blacklist:
                continue

            last_ping_ts = nb_cfg.get("LastPingTs", None)
            if last_ping_ts:
                try:
                    lp = datetime.fromisoformat(last_ping_ts)
                    if lp.tzinfo is None:
                        # Assume local tz for legacy naive timestamps
                        lp = lp.replace(tzinfo=self._local_tz())
                    if lp.astimezone(self._local_tz()).date() == today:
                        continue
                except Exception:
                    pass

            channel = guild.get_channel(int(noticeboard_channel_id))
            if channel is None:
                self._dbg(
                    f"[Ping] Skip: channel not found guild={guild_id} id={noticeboard_channel_id}"
                )
                continue

            # Smart gating (optional)
            if smart_ping:
                try:
                    task_data_str = cache_read_latest("all")
                    if not task_data_str:
                        continue
                    task_data = json.loads(task_data_str)
                except Exception as e:
                    self._dbg(
                        f"[Ping] Smart skip: cache read/parse failed guild={guild_id}: {e}"
                    )
                    continue
                if not (
                    self.has_assignments_tomorrow(task_data)
                    or self.has_assignments_this_week(task_data)
                ):
                    self._dbg(f"[Ping] Smart skip: no upcoming work guild={guild_id}")
                    continue

            # Build today's scheduled ping datetime in local tz
            try:
                ping_time = datetime.strptime(ping_daily_time, "%H:%M").time()
            except ValueError:
                self._dbg(
                    f"[Ping] Invalid PingDailyTime='{ping_daily_time}' guild={guild_id}"
                )
                continue
            scheduled_dt = datetime.combine(today, ping_time).replace(
                tzinfo=self._local_tz()
            )

            # If now >= scheduled and not yet pinged today: delete old, send new
            if now >= scheduled_dt:
                self._dbg(
                    f"[Ping] Due: sending ping guild={guild_id} now={now.time()} scheduled={scheduled_dt.time()}"
                )
                old_id = nb_cfg.get("PingMessageEditID", None)
                if old_id:
                    try:
                        old_msg = await channel.fetch_message(old_id)
                        await old_msg.delete()
                        self._dbg(
                            f"[Ping] Deleted old ping message id={old_id} guild={guild_id}"
                        )
                    except (discord.NotFound, discord.HTTPException):
                        self._dbg(
                            f"[Ping] Old ping message missing/unreachable id={old_id} guild={guild_id}"
                        )
                        pass
                await self.handle_ping_message(
                    channel, guild_id, today, ping_daily_time, now
                )
            else:
                pass  # no verbose [Ping] debug

    async def edit_with_retries(
        self, msg: discord.Message, content=None, embed=None, attempts: int = 3
    ) -> bool:
        """Edit a message with simple retry/backoff. Returns True on success."""
        delay = 1.5
        for i in range(attempts):
            try:
                await msg.edit(content=content, embed=embed)
                self._dbg(f"[MsgEdit] Success on attempt {i+1} for message id={msg.id}")
                return True
            except discord.NotFound:
                self._dbg(f"[MsgEdit] NotFound for message id={getattr(msg,'id','?')}")
                return False
            except discord.HTTPException as e:
                self._dbg(f"[MsgEdit] HTTPException on attempt {i+1}: {e}")
                await asyncio.sleep(delay)
                delay *= 2
            except Exception as e:
                self._dbg(f"[MsgEdit] Unexpected error: {e}")
                return False
        return False

    async def handle_ping_message(self, channel, guild_id, today, ping_daily_time, now):
        """Handles sending or editing the ping message reliably."""
        async with self.ping_message_lock:
            self.ping_message_being_updated[guild_id] = True
            try:
                # build content
                config = json_get(guild_id)
                nb_cfg = config.get("Noticeboard", {})
                pingmessage_edit_id = nb_cfg.get("PingMessageEditID", None)
                # Prepare last ping (previous) if available
                last_ping_iso = nb_cfg.get("LastPingTs")
                last_ping_dt = None
                try:
                    if last_ping_iso:
                        last_ping_dt = datetime.fromisoformat(last_ping_iso)
                        if last_ping_dt.tzinfo is None:
                            last_ping_dt = last_ping_dt.replace(tzinfo=self._local_tz())
                except Exception:
                    last_ping_dt = None

                # Use effective interval (floor by GlobalHeartbeat)
                interval = self._effective_interval(nb_cfg)
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
                    last_ping_dt=last_ping_dt,
                )

                if pingmessage_edit_id:
                    try:
                        msg = await channel.fetch_message(pingmessage_edit_id)
                        ok = await self.edit_with_retries(
                            msg, content=new_content, embed=None
                        )
                        if not ok:
                            new_msg = await channel.send(new_content)
                            edit_json_file(
                                guild_id, "Noticeboard.PingMessageEditID", new_msg.id
                            )
                            self.sent_message_ids.setdefault(guild_id, {})[
                                "ping"
                            ] = new_msg.id
                            self._dbg(
                                f"[Ping] Edit failed; sent new ping message id={new_msg.id} guild={guild_id}"
                            )
                    except discord.NotFound:
                        new_msg = await channel.send(new_content)
                        edit_json_file(
                            guild_id, "Noticeboard.PingMessageEditID", new_msg.id
                        )
                        self.sent_message_ids.setdefault(guild_id, {})[
                            "ping"
                        ] = new_msg.id
                        self._dbg(
                            f"[Ping] Previous ping not found; sent new id={new_msg.id} guild={guild_id}"
                        )
                    except discord.HTTPException as e:
                        self._dbg(
                            f"[Ping] HTTPException while editing/sending ping guild={guild_id}: {e}"
                        )
                        pass
                else:
                    try:
                        msg = await channel.send(new_content)
                        edit_json_file(
                            guild_id, "Noticeboard.PingMessageEditID", msg.id
                        )
                        self.sent_message_ids.setdefault(guild_id, {})["ping"] = msg.id
                        self._dbg(
                            f"[Ping] Sent fresh ping message id={msg.id} guild={guild_id}"
                        )
                    except discord.HTTPException as e:
                        self._dbg(f"[Ping] Send failed guild={guild_id}: {e}")
                        pass
            finally:
                # Mark last ping time using LastPingTs (local-aware)
                try:
                    edit_json_file(
                        guild_id,
                        "Noticeboard.LastPingTs",
                        self.local_now().isoformat(),
                    )
                except Exception:
                    pass
                self.ping_message_being_updated[guild_id] = False

    async def send_ping_message(
        self,
        channel,
        ping_role,
        today,
        next_ping_time,
        next_update_time,
        api_call_time,
        last_ping_dt: datetime | None = None,
    ):
        next_update_timestamp = (
            int(next_update_time.timestamp())
            if isinstance(next_update_time, datetime)
            else "N/A"
        )
        last_ping_line = ""
        try:
            if isinstance(last_ping_dt, datetime):
                last_ping_line = f"- Last Ping: <t:{int(last_ping_dt.timestamp())}:R>\n"
        except Exception:
            pass

        ping_message_content = (
            f"# Daily Ping <@&{ping_role}>\n"
            f"- Today's date: {today.strftime('%a, %d %b %Y')}\n"
            f"- Next Refresh in: <t:{next_update_timestamp}:R>\n"
            f"- Next Ping in: <t:{int(next_ping_time.timestamp())}:R>\n"
            f"{last_ping_line}"
        )
        return ping_message_content

    def get_next_update_time(self, interval_seconds):
        current_time = self.local_now()
        next_update = current_time + timedelta(seconds=interval_seconds)
        return next_update

    def get_next_ping_time(self, ping_daily_time):
        now_local = self.local_now()
        target_time_str = f"{now_local.strftime('%Y-%m-%d')} {ping_daily_time}"
        naive = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M")
        next_ping_time = naive.replace(tzinfo=self._local_tz())
        if next_ping_time < now_local:
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

    def create_weekly_embed(
        self, task_data, version, api_call_time, last_update_dt: datetime | None = None
    ):
        today = self.local_now().date()
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

    def create_notice_embed(
        self, task_data, version, last_update_dt: datetime | None = None
    ):
        embed = discord.Embed(
            title="Notice Board",
            description="Tasks you have to do",
            color=discord.Color.blue(),
        )
        unknown_due_tasks = task_data.get("data", {}).get("unknown-due", [])

        today = self.local_now().date()  # Get today's date

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

    def create_due_tomorrow_embed(
        self, task_data, version, last_update_dt: datetime | None = None
    ):
        today = self.local_now()
        tomorrow = today + timedelta(days=1)
        next_due = None
        embed = discord.Embed(
            title="Assignments Due Tomorrow", color=discord.Color.orange()
        )

        for date, tasks in task_data.get("data", {}).items():
            if date == "unknown-due":
                continue
            try:
                task_naive = datetime.strptime(date, "%A, %d-%m-%Y")
                task_date = task_naive.replace(tzinfo=self._local_tz())
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
            lu = self.local_now()
            notice_embed = self.create_notice_embed(task_data, version, lu)
            this_week_embed = self.create_weekly_embed(
                task_data, version, self.local_now().strftime("%Y-%m-%d"), lu
            )
            due_tomorrow_embed = self.create_due_tomorrow_embed(task_data, version, lu)

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
