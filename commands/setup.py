import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands, tasks
from discord.ui import Button, View

from modules.setconfig import SETTINGS_SCHEMA, json_get


CONFIG_RETENTION_DAYS = int(os.getenv("CONFIG_RETENTION_DAYS", "7"))


def _build_default_config() -> Dict[str, Dict[str, Any]]:
    config: Dict[str, Dict[str, Any]] = {}
    for section, fields in SETTINGS_SCHEMA.items():
        section_payload: Dict[str, Any] = {}
        for key, meta in fields.items():
            section_payload[key] = meta.get("default")
        config[section] = section_payload
    return config


class ConfirmResetView(View):
    def __init__(self, author_id: int):
        super().__init__(timeout=45)
        self.author_id = author_id
        self.value: Optional[bool] = None

        yes_button = Button(label="Yes", style=discord.ButtonStyle.green)
        no_button = Button(label="No", style=discord.ButtonStyle.red)

        yes_button.callback = self._on_yes
        no_button.callback = self._on_no

        self.add_item(yes_button)
        self.add_item(no_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                ":x: Only the command invoker can respond to this prompt.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        self.value = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _on_no(self, interaction: discord.Interaction) -> None:
        self.value = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class Setup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
        os.makedirs(self.config_dir, exist_ok=True)
        self.orphaned_path = os.path.join(self.config_dir, ".orphaned.json")
        self.cleanup_configs.start()

    def cog_unload(self):
        if self.cleanup_configs.is_running():
            self.cleanup_configs.cancel()

    def _config_path(self, guild_id: int) -> str:
        return os.path.join(self.config_dir, f"{guild_id}.json")

    def _config_exists(self, guild_id: int) -> bool:
        return os.path.exists(self._config_path(guild_id))

    def _load_orphaned(self) -> Dict[str, str]:
        if not os.path.exists(self.orphaned_path):
            return {}
        try:
            with open(self.orphaned_path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return {}

    def _save_orphaned(self, payload: Dict[str, str]) -> None:
        with open(self.orphaned_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2)

    def _mark_orphaned(self, guild_id: int) -> None:
        data = self._load_orphaned()
        data[str(guild_id)] = datetime.now(timezone.utc).isoformat()
        self._save_orphaned(data)

    def _clear_orphaned(self, guild_id: int) -> None:
        data = self._load_orphaned()
        if data.pop(str(guild_id), None) is not None:
            self._save_orphaned(data)

    async def _notify_admins(self, guild: discord.Guild) -> None:
        embed = discord.Embed(
            title="Action Required: Configure the Bot",
            description=(
                "Thanks for adding the bot! An administrator needs to run `!setup` "
                "or `/setup` to initialize configuration. Configuration is retained "
                "while the bot remains in your server. If the bot is removed, its "
                f"saved settings are purged after {CONFIG_RETENTION_DAYS} day(s)."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="Next Steps",
            value=(
                "Run `!setup` or `/setup` in a channel where the bot can reply. "
                "Only members with the Administrator permission can complete the wizard."
            ),
            inline=False,
        )

        notified = False
        admins: List[discord.Member] = [
            member for member in guild.members if member.guild_permissions.administrator
        ]

        if guild.owner and guild.owner not in admins:
            admins.insert(0, guild.owner)

        for member in admins[:10]:
            try:
                await member.send(embed=embed)
                notified = True
            except Exception:
                continue

        if notified:
            return

        channel: Optional[discord.TextChannel] = guild.system_channel
        if channel is None:
            for candidate in guild.text_channels:
                if candidate.permissions_for(guild.me).send_messages:
                    channel = candidate
                    break
        if channel:
            await channel.send(embed=embed)

    @tasks.loop(hours=12)
    async def cleanup_configs(self) -> None:
        data = self._load_orphaned()
        now = datetime.now(timezone.utc)
        retention = timedelta(days=CONFIG_RETENTION_DAYS)
        updated = False

        active_guild_ids = {g.id for g in self.bot.guilds}

        # Ensure all config files on disk are tracked for retention
        try:
            config_filenames = [
                fname
                for fname in os.listdir(self.config_dir)
                if fname.endswith(".json") and fname != ".orphaned.json"
            ]
        except FileNotFoundError:
            config_filenames = []

        for fname in config_filenames:
            try:
                gid = int(fname[:-5])
            except ValueError:
                continue

            if gid in active_guild_ids:
                if data.pop(str(gid), None) is not None:
                    updated = True
                continue

            if str(gid) not in data:
                data[str(gid)] = now.isoformat()
                updated = True

        for guild_id_str, timestamp in list(data.items()):
            gid = int(guild_id_str)
            if gid in active_guild_ids:
                data.pop(guild_id_str, None)
                updated = True
                continue

            config_path = self._config_path(gid)
            if not os.path.exists(config_path):
                data.pop(guild_id_str, None)
                updated = True
                continue

            try:
                orphaned_time = datetime.fromisoformat(timestamp)
            except Exception:
                orphaned_time = None

            if orphaned_time and now - orphaned_time < retention:
                continue

            try:
                os.remove(config_path)
            except Exception:
                continue
            else:
                data.pop(guild_id_str, None)
                updated = True

        if updated:
            self._save_orphaned(data)

    @cleanup_configs.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        self._clear_orphaned(guild.id)
        if not self._config_exists(guild.id):
            await self._notify_admins(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if self._config_exists(guild.id):
            self._mark_orphaned(guild.id)

    @commands.hybrid_command(name="setup", description="Run the configuration wizard.")
    async def setup(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            if ctx.interaction and not ctx.interaction.response.is_done():
                await ctx.defer(ephemeral=True)
            await ctx.send("This command can only be used inside a server.")
            return

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

        if not ctx.author.guild_permissions.administrator:
            embed = discord.Embed(
                title=":x: Insufficient Permissions",
                description="Only administrators can initialize the bot.",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            return

        guild_id = ctx.guild.id
        config_exists = self._config_exists(guild_id)

        if config_exists:
            embed = discord.Embed(
                title="Setup Wizard",
                description=(
                    "A configuration already exists for this guild. Do you want to "
                    "run the setup again? This will overwrite saved role settings."
                ),
                color=discord.Color.orange(),
            )
            view = ConfirmResetView(ctx.author.id)
            message = await ctx.send(embed=embed, view=view)
            await view.wait()
            for child in view.children:
                child.disabled = True
            await message.edit(view=view)

            if view.value is not True:
                if view.value is False:
                    await ctx.send("Setup aborted.")
                else:
                    await ctx.send("Setup timed out. Please run the command again.")
                return
            await self.start_setup(ctx, placeholder_message=message)
        else:
            await self.start_setup(ctx)

    async def _prompt_for_role(
        self, ctx: commands.Context, prompt: str
    ) -> Optional[discord.Role]:
        instruction = f"{prompt}\n*(Type `cancel` to abort setup.)*"
        prompt_msg = await ctx.send(instruction)

        def check(message: discord.Message) -> bool:
            return message.author == ctx.author and message.channel == ctx.channel

        try:
            while True:
                message = await self.bot.wait_for("message", check=check, timeout=300)
                content = message.content.strip()

                if content.lower() == "cancel":
                    await message.delete()
                    await prompt_msg.delete()
                    return None

                role: Optional[discord.Role] = None
                if message.role_mentions:
                    role = message.role_mentions[0]
                elif content.isdigit():
                    role = ctx.guild.get_role(int(content))

                if role is None:
                    await ctx.send(
                        "Invalid role. Mention a role or provide its numeric ID.",
                        delete_after=10,
                    )
                    await message.delete()
                    continue

                await message.delete()
                await prompt_msg.delete()
                return role
        except asyncio.TimeoutError:
            await prompt_msg.delete()
            await ctx.send(
                "Setup timed out waiting for a response. Please run `!setup` or `/setup` again."
            )
            return None

    def _progress_embed(
        self,
        steps: List[Dict[str, str]],
        results: Dict[str, Optional[discord.Role]],
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Setup Wizard",
            description="Provide the requested roles to finish configuring the bot.",
            color=discord.Color.blurple(),
        )
        for index, step in enumerate(steps, start=1):
            role = results.get(step["key"])
            if role:
                value = f":white_check_mark: {role.mention}"
            else:
                value = f":hourglass_flowing_sand: {step['prompt']}"
            embed.add_field(
                name=f"Step {index}: {step['name']}",
                value=value,
                inline=False,
            )
        return embed

    async def start_setup(
        self,
        ctx: commands.Context,
        *,
        placeholder_message: Optional[discord.Message] = None,
    ) -> None:
        steps = [
            {
                "name": "Admin Role",
                "prompt": "Mention or provide the ID of the role that should manage the bot.",
                "key": "admin",
            },
            {
                "name": "Default Role",
                "prompt": "Mention or provide the ID of your default member role.",
                "key": "default",
            },
            {
                "name": "Ping Role",
                "prompt": "Mention or provide the ID of the role to ping for updates.",
                "key": "ping",
            },
            {
                "name": "DJ Role",
                "prompt": "Mention or provide the ID of the DJ role for music commands.",
                "key": "dj",
            },
        ]

        progress: Dict[str, Optional[discord.Role]] = {
            step["key"]: None for step in steps
        }

        if placeholder_message is None:
            placeholder_message = await ctx.send("Preparing setup wizard…")

        await placeholder_message.edit(
            content=None,
            embed=self._progress_embed(steps, progress),
            view=None,
        )

        for step in steps:
            role = await self._prompt_for_role(ctx, step["prompt"])
            if role is None:
                await placeholder_message.edit(view=None)
                return
            progress[step["key"]] = role
            await placeholder_message.edit(embed=self._progress_embed(steps, progress))

        admin_role = progress["admin"]
        default_role = progress["default"]
        ping_role = progress["ping"]
        dj_role = progress["dj"]

        if not all([admin_role, default_role, ping_role, dj_role]):
            await ctx.send(":x: Setup failed because one or more roles were missing.")
            return

        config = _build_default_config()
        config.setdefault("General", {})
        config.setdefault("Noticeboard", {})
        config.setdefault("Music", {})

        config["General"]["DefaultAdmin"] = admin_role.id
        config["General"]["DefaultRoleId"] = default_role.id
        config["Noticeboard"]["PingRoleId"] = ping_role.id
        config["Music"]["DJRole"] = dj_role.id
        config["Music"]["Enabled"] = True
        config["General"]["LastSetupTs"] = datetime.now(timezone.utc).isoformat()

        config_path = self._config_path(ctx.guild.id)
        try:
            with open(config_path, "w", encoding="utf-8") as fp:
                json.dump(config, fp, indent=4)
            # Normalize via setconfig to ensure schema compatibility
            json_get(ctx.guild.id)
        except Exception as exc:
            await ctx.send(f":x: Failed to write configuration: {exc}")
            return

        self._clear_orphaned(ctx.guild.id)

        summary = discord.Embed(
            title="Setup Complete",
            description=(
                "Configuration saved successfully. Use `/settings` to make further "
                "changes or review module options."
            ),
            color=discord.Color.green(),
        )
        summary.add_field(name="Admin Role", value=admin_role.mention, inline=True)
        summary.add_field(name="Default Role", value=default_role.mention, inline=True)
        summary.add_field(name="Ping Role", value=ping_role.mention, inline=True)
        summary.add_field(name="DJ Role", value=dj_role.mention, inline=True)
        summary.set_footer(
            text=(
                "Configuration files are automatically purged if the bot is absent "
                f"from a server for {CONFIG_RETENTION_DAYS} day(s)."
            )
        )

        await placeholder_message.edit(embed=summary, view=None)
        await ctx.send(
            "Setup finished! Administrators can now run `/settings` or use module commands."
        )


async def setup(bot):
    await bot.add_cog(Setup(bot))
