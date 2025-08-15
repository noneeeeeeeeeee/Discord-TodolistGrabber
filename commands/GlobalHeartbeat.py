from discord.ext import commands, tasks
import discord
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from modules.setconfig import json_get, edit_json_file
from modules.enviromentfilegenerator import check_and_load_env_file

check_and_load_env_file()
OWNER_ID = os.getenv("OWNER_ID")
MAIN_GUILD = os.getenv("MAIN_GUILD")
LOCAL_TZ_NAME = os.getenv("LOCAL_REGION")


def _central_guild_id(bot: commands.Bot) -> int | None:
    try:
        if MAIN_GUILD:
            return int(MAIN_GUILD)
        # fallback: any guild
        return bot.guilds[0].id if bot.guilds else None
    except Exception:
        return None


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(LOCAL_TZ_NAME or "UTC")
    except Exception:
        return ZoneInfo("UTC")


class HeartbeatView(discord.ui.View):
    def __init__(self, cog: "GlobalHeartbeat"):
        super().__init__(timeout=60)
        self.cog = cog
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if OWNER_ID and str(interaction.user.id) == str(OWNER_ID):
            return True
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return False

    async def on_timeout(self):
        try:
            for item in self.children:
                if isinstance(item, discord.ui.Item):
                    item.disabled = True
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Invoke Now", style=discord.ButtonStyle.green)
    async def invoke_now(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.invoke_heartbeat(run_now=True)
        await interaction.followup.send("Heartbeat invoked.", ephemeral=True)

    @discord.ui.button(label="Enable/Disable", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button):
        gid = _central_guild_id(self.cog.bot)
        if gid is None:
            await interaction.response.send_message(
                "No guild available.", ephemeral=True
            )
            return
        cfg = json_get(gid)
        enabled = bool(cfg.get("General", {}).get("GlobalHeartbeatEnabled", True))
        try:
            edit_json_file(
                gid,
                "General.GlobalHeartbeatEnabled",
                not enabled,
                actor_user_id=int(OWNER_ID) if OWNER_ID else None,
            )
            await interaction.response.send_message(
                f"Heartbeat {'enabled' if not enabled else 'disabled'}.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to toggle: {e}", ephemeral=True
            )


class GlobalHeartbeat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._initial_invoked = False
        self.heartbeat_loop.start()

    def cog_unload(self):
        try:
            self.heartbeat_loop.cancel()
        except Exception:
            pass

    def _read_heartbeat_settings(self):
        gid = _central_guild_id(self.bot)
        if gid is None:
            return 1800, True, None
        cfg = json_get(gid)
        general = cfg.get("General", {})
        hb = int(general.get("GlobalHeartbeat", 1800))
        enabled = bool(general.get("GlobalHeartbeatEnabled", True))
        last_ts = general.get("LastHeartbeatTs", None)
        last_dt = None
        try:
            if last_ts:
                last_dt = datetime.fromisoformat(last_ts)
                if last_dt.tzinfo is None:
                    # Assume local timezone for legacy naive timestamps
                    last_dt = last_dt.replace(tzinfo=_local_tz())
        except Exception:
            last_dt = None
        return max(hb, 1800), enabled, (gid, last_dt)

    async def invoke_heartbeat(self, run_now: bool = False):
        hb, enabled, last_info = self._read_heartbeat_settings()
        gid, _ = last_info if last_info else (None, None)
        if not enabled and not run_now:
            return
        # Run ticks
        na = self.bot.get_cog("NoticeAutoUpdate")
        if na:
            try:
                await na.process_noticeboard_tick()
                await na.process_ping_tick()
            except Exception:
                pass
        # Persist last heartbeat timestamp (local timezone, aware)
        if gid is not None:
            try:
                edit_json_file(
                    gid,
                    "General.LastHeartbeatTs",
                    datetime.now(_local_tz()).isoformat(),
                )
            except Exception:
                pass

    @tasks.loop(seconds=30)
    async def heartbeat_loop(self):
        # Check if it's time to run; otherwise no-op
        hb, enabled, last_info = self._read_heartbeat_settings()
        gid, last_dt = last_info if last_info else (None, None)
        if not enabled:
            return
        now = datetime.now(_local_tz())
        due = last_dt is None or (now - last_dt) >= timedelta(seconds=hb)
        if due:
            await self.invoke_heartbeat(run_now=True)

    @heartbeat_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    def _embed_status(self) -> discord.Embed:
        hb, enabled, last_info = self._read_heartbeat_settings()
        _, last_dt = last_info if last_info else (None, None)
        now = datetime.now(_local_tz())
        # Convert last heartbeat to local timezone for display
        last_local_str = "Never"
        try:
            if last_dt:
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_local_tz())
                last_local = last_dt.astimezone(_local_tz())
                last_local_str = last_local.isoformat()
        except Exception:
            last_local_str = last_dt.isoformat() if last_dt else "Never"
        next_ts = (
            (last_dt + timedelta(seconds=hb))
            if (enabled and last_dt)
            else (now + timedelta(seconds=hb) if enabled else None)
        )
        desc = []
        desc.append(f"Status: {'Enabled' if enabled else 'Disabled'}")
        desc.append(f"Heartbeat interval: {hb} seconds")
        desc.append(f"Last heartbeat: {last_local_str}")
        if next_ts:
            desc.append(f"Time to next heartbeat: <t:{int(next_ts.timestamp())}:R>")
        embed = discord.Embed(
            title="Global Heartbeat",
            description="\n".join(desc),
            color=discord.Color.blurple(),
        )
        return embed

    @commands.hybrid_command(
        name="heartbeat",
        description="Show and control the global heartbeat (owner only).",
    )
    async def heartbeat(self, ctx: commands.Context):
        if not OWNER_ID or str(ctx.author.id) != str(OWNER_ID):
            await ctx.send("Not authorized.")
            return
        embed = self._embed_status()
        view = HeartbeatView(self)
        msg = await ctx.send(embed=embed, view=view)
        try:
            view.message = msg
        except Exception:
            pass
        return embed

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._initial_invoked:
            try:
                await self.invoke_heartbeat(run_now=True)
            except Exception:
                pass
            self._initial_invoked = True


async def setup(bot):
    await bot.add_cog(GlobalHeartbeat(bot))
