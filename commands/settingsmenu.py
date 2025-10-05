import discord
from discord.ext import commands
from discord.ui import View, Select, Button, Modal, TextInput
from modules.setconfig import (
    json_get,
    check_guild_config_available,
    check_admin_role,
    get_settings_schema,
    get_setting_meta,
    edit_json_file,
    coerce_value_for_path,
)
import re
import os


def _flatten_schema(schema):
    flat = {}
    for section, fields in schema.items():
        for key, meta in fields.items():
            flat.setdefault(section, {})[f"{section}.{key}"] = {"key": key, **meta}
    return flat


def _safe_get(cfg: dict, path: str):
    cur = cfg
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _format_value(val):
    if isinstance(val, list):
        return ", ".join(map(str, val)) if val else "[]"
    if val is None:
        return "null"
    return str(val)


class SetValueModal(Modal, title="Set Setting Value"):
    def __init__(self, path: str, placeholder: str = "", default: str = ""):
        super().__init__(timeout=120)
        self.path = path
        self.input = TextInput(
            label="Value", placeholder=placeholder, default=default, required=True
        )
        self.add_item(self.input)
        self.result = None

    async def on_submit(self, interaction: discord.Interaction):
        self.result = str(self.input.value).strip()
        await interaction.response.defer()


def _access_label(v: int) -> str:
    return {
        0: "Editable",
        1: "Not Editable",
        2: "Hidden",
        3: "Central-only Editable",
        4: "Central-only Editable (Hidden elsewhere)",
    }.get(int(v or 0), "Editable")


def _is_owner(user_id: int) -> bool:
    try:
        return str(user_id) == str(os.getenv("OWNER_ID") or "")
    except Exception:
        return False


def _can_view(meta: dict, is_owner: bool) -> bool:
    access = int(meta.get("access", 0))
    if access == 2:
        return False
    if access == 4 and not is_owner:
        return False
    return True


def _can_edit(meta: dict, is_owner: bool) -> bool:
    access = int(meta.get("access", 0))
    if access == 0:
        return True
    if access in (3, 4) and is_owner:
        return True
    return False


class ChoiceSelectionView(View):
    """Dropdown selector for settings with fixed choices."""

    def __init__(self, parent_view, meta: dict):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.meta = meta
        self.selected_path = parent_view.selected_path

        # Build dropdown options from choices
        choices = meta.get("choices", [])
        setting_type = meta.get("type", "str")
        is_list_type = "list[" in setting_type
        is_nullable = "|null" in setting_type or "|None" in setting_type

        if isinstance(choices, list) and len(choices) <= 25:
            max_values = 1
            if is_list_type:
                max_values = len(choices)

            select = Select(
                placeholder="Select value(s)" if is_list_type else "Select a value",
                min_values=0 if is_nullable else 1,
                max_values=max_values,
            )

            if is_nullable and not is_list_type:
                select.add_option(
                    label="None (Clear Value)", value="__NULL__", emoji="🚫"
                )

            for choice in choices:
                select.add_option(label=str(choice), value=str(choice))

            select.callback = self.on_choice_select
            self.add_item(select)

    async def on_choice_select(self, interaction: discord.Interaction):
        selected_values = self.children[0].values
        setting_type = self.meta.get("type", "str")
        is_list_type = "list[" in setting_type

        try:
            if not selected_values or (
                len(selected_values) == 1 and selected_values[0] == "__NULL__"
            ):
                coerced = None
                display = "None"
            elif is_list_type:
                raw_value = selected_values
                coerced = coerce_value_for_path(self.selected_path, raw_value)
                display = ", ".join(selected_values)
            else:
                raw_value = selected_values[0]
                coerced = coerce_value_for_path(self.selected_path, raw_value)
                display = raw_value

            edit_json_file(
                self.parent_view.ctx.guild.id,
                self.selected_path,
                coerced,
                actor_user_id=self.parent_view.ctx.author.id,
            )
            self.parent_view.cfg = json_get(self.parent_view.ctx.guild.id)
            await self.parent_view.message.edit(
                embed=self.parent_view._embed(message=f"Set to: {display}"),
                view=self.parent_view,
            )
            await interaction.response.send_message(
                f"✅ Value updated to: **{display}**", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Error: {str(e)}", ephemeral=True
            )


class RoleSelectionView(View):
    """Dropdown selector for role selection."""

    def __init__(self, parent_view, meta: dict):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.meta = meta
        self.selected_path = parent_view.selected_path

        # Get guild roles
        guild = parent_view.ctx.guild
        roles = [r for r in guild.roles if not r.is_default() and not r.managed][:25]

        if roles:
            select = Select(placeholder="Select a role")
            # Add "None" option for nullable roles
            if "null" in meta.get("type", ""):
                select.add_option(label="None (Clear Role)", value="null", emoji="🚫")

            for role in roles:
                select.add_option(label=role.name, value=str(role.id), emoji="👤")
            select.callback = self.on_role_select
            self.add_item(select)

    async def on_role_select(self, interaction: discord.Interaction):
        selected = self.children[0].values[0]

        try:
            if selected == "null":
                coerced = None
            else:
                coerced = coerce_value_for_path(self.selected_path, selected)

            edit_json_file(
                self.parent_view.ctx.guild.id,
                self.selected_path,
                coerced,
                actor_user_id=self.parent_view.ctx.author.id,
            )
            self.parent_view.cfg = json_get(self.parent_view.ctx.guild.id)

            if selected == "null":
                display = "None"
            else:
                role = interaction.guild.get_role(int(selected))
                display = role.name if role else f"Role ID: {selected}"

            await self.parent_view.message.edit(
                embed=self.parent_view._embed(message=f"Set to: {display}"),
                view=self.parent_view,
            )
            await interaction.response.send_message(
                f"✅ Role updated to: **{display}**", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Error: {str(e)}", ephemeral=True
            )


class SettingsView(View):
    def __init__(self, bot: commands.Bot, ctx: commands.Context, schema, cfg):
        super().__init__(timeout=60)
        self.bot = bot
        self.ctx = ctx
        self.schema = schema
        self.flat = _flatten_schema(schema)
        self.cfg = cfg
        self.selected_section = None
        self.selected_path = None
        self.is_owner = _is_owner(ctx.author.id)
        self.visible_paths = []

        self.section_select = Select(placeholder="Choose module/section")
        for section in self.flat.keys():
            self.section_select.add_option(label=section, value=section)
        self.section_select.callback = self.on_section_select
        self.add_item(self.section_select)

        self.setting_select = Select(
            placeholder="Choose setting",
            options=[
                discord.SelectOption(label="Select a module first", value="__noop__")
            ],
            disabled=True,
        )
        self.setting_select.callback = self.on_setting_select
        self.add_item(self.setting_select)

        self.toggle_btn = Button(
            label="Toggle (bool)", style=discord.ButtonStyle.primary, disabled=True
        )
        self.toggle_btn.callback = self.on_toggle
        self.add_item(self.toggle_btn)

        self.set_value_btn = Button(
            label="Set Value", style=discord.ButtonStyle.success, disabled=True
        )
        self.set_value_btn.callback = self.on_set_value
        self.add_item(self.set_value_btn)

        self.reset_btn = Button(
            label="Reset to Default", style=discord.ButtonStyle.danger, disabled=True
        )
        self.reset_btn.callback = self.on_reset
        self.add_item(self.reset_btn)

        self.refresh_btn = Button(label="Refresh", style=discord.ButtonStyle.secondary)
        self.refresh_btn.callback = self.on_refresh
        self.add_item(self.refresh_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    def current_meta(self):
        if not self.selected_path:
            return None
        return get_setting_meta(self.selected_path) or self.flat.get(
            self.selected_section, {}
        ).get(self.selected_path)

    async def on_section_select(self, interaction: discord.Interaction):
        self.selected_section = self.section_select.values[0]
        self.selected_path = None
        self.setting_select.options.clear()

        visible_paths = []
        for path, meta in self.flat[self.selected_section].items():
            if _can_view(meta, self.is_owner):
                visible_paths.append((path, meta))
        self.visible_paths = visible_paths

        if not visible_paths:
            self.setting_select.add_option(
                label="No settings available",
                value="__noop__",
                description="None visible here",
            )
            self.setting_select.disabled = True
        else:
            for path, meta in visible_paths:
                # Show "{Setting Name}" and description (truncate to 100 chars)
                opt_label = meta["key"]
                desc_raw = meta.get("description") or opt_label
                opt_desc = desc_raw if len(desc_raw) <= 100 else (desc_raw[:97] + "...")
                self.setting_select.add_option(
                    label=opt_label,
                    value=path,
                    description=opt_desc,
                )
            self.setting_select.disabled = False

        self.toggle_btn.disabled = True
        self.set_value_btn.disabled = True
        self.reset_btn.disabled = True
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_setting_select(self, interaction: discord.Interaction):
        sel = self.setting_select.values[0]
        if sel == "__noop__":
            return
        self.selected_path = sel
        meta = self.current_meta() or {}
        t = meta.get("type", "str")
        is_bool = t == "bool"
        can_edit = _can_edit(meta, self.is_owner)
        self.toggle_btn.disabled = not (is_bool and can_edit)
        self.set_value_btn.disabled = not (can_edit and not is_bool)
        self.reset_btn.disabled = not can_edit
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_toggle(self, interaction: discord.Interaction):
        if not self.selected_path:
            return
        cur = _safe_get(self.cfg, self.selected_path)
        try:
            new_val = not bool(cur)
            edit_json_file(
                self.ctx.guild.id,
                self.selected_path,
                new_val,
                actor_user_id=self.ctx.author.id,
            )
            self.cfg = json_get(self.ctx.guild.id)
            await interaction.response.edit_message(
                embed=self._embed(message="Toggled successfully."), view=self
            )
        except Exception as e:
            await interaction.response.edit_message(
                embed=self._embed(error=str(e)), view=self
            )

    async def on_set_value(self, interaction: discord.Interaction):
        if not self.selected_path:
            return
        meta = self.current_meta() or {}
        t = meta.get("type", "str")

        # If setting has fixed choices, show dropdown
        if "choices" in meta and meta["choices"]:
            view = ChoiceSelectionView(self, meta)
            await interaction.response.send_message(
                "Select a value:", view=view, ephemeral=True
            )
            return

        # If setting is role type, show role selector
        if t in ("role", "role|null"):
            view = RoleSelectionView(self, meta)
            await interaction.response.send_message(
                "Select a role:", view=view, ephemeral=True
            )
            return

        # Otherwise show modal input
        placeholder = meta.get("type", "str")
        cur_val = _format_value(_safe_get(self.cfg, self.selected_path))
        modal = SetValueModal(
            self.selected_path,
            placeholder=placeholder,
            default=str(cur_val if cur_val is not None else ""),
        )
        await interaction.response.send_modal(modal)
        try:
            await modal.wait()
        except Exception:
            return
        if modal.result is None:
            return
        # Convert common mention/ID inputs for channel
        raw_input = modal.result
        try:
            if t in ("channel|Default",):
                m = re.search(r"(\d{15,25})", raw_input)
                raw_value = m.group(1) if m else raw_input
            elif t in ("int", "int|null"):
                raw_value = raw_input
            elif t == "float":
                raw_value = raw_input
            elif t == "list[str]":
                raw_value = raw_input  # comma-separated supported
            else:
                raw_value = raw_input
            # Let setconfig coerce and validate
            coerced = coerce_value_for_path(self.selected_path, raw_value)
            edit_json_file(
                self.ctx.guild.id,
                self.selected_path,
                coerced,
                actor_user_id=self.ctx.author.id,
            )
            self.cfg = json_get(self.ctx.guild.id)
            await self.message.edit(
                embed=self._embed(message="Value updated."), view=self
            )
        except Exception as e:
            await self.message.edit(embed=self._embed(error=str(e)), view=self)

    async def on_reset(self, interaction: discord.Interaction):
        meta = self.current_meta() or {}
        default = meta.get("default", None)
        try:
            edit_json_file(
                self.ctx.guild.id,
                self.selected_path,
                default,
                actor_user_id=self.ctx.author.id,
            )
            self.cfg = json_get(self.ctx.guild.id)
            await interaction.response.edit_message(
                embed=self._embed(message="Reset to default."), view=self
            )
        except Exception as e:
            await interaction.response.edit_message(
                embed=self._embed(error=str(e)), view=self
            )

    async def on_refresh(self, interaction: discord.Interaction):
        self.cfg = json_get(self.ctx.guild.id)
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

    def _embed(self, message: str = None, error: str = None):
        # Build breadcrumb navigation
        breadcrumb = "Settings"
        if self.selected_section:
            breadcrumb += f" > {self.selected_section}"
            if self.selected_path:
                meta = self.current_meta() or {}
                setting_name = meta.get("key", self.selected_path.split(".")[-1])
                breadcrumb += f" > {setting_name}"

        title = "Settings Menu"

        # Module level view
        if self.selected_section and not self.selected_path:
            module_desc = {
                "Music": "Configure DJ role, queue limits, playlist behavior, and player settings. Central-only settings (owner) propagate to all guilds.",
                "Noticeboard": "Configure noticeboard updates, ping schedules, and notifications.",
                "General": "General bot configuration and administrative settings.",
                "GoogleClassroom": "Google Classroom integration settings (Not yet implemented).",
            }.get(
                self.selected_section,
                f"Configure {self.selected_section} module settings.",
            )

            desc = f"**{self.selected_section} Module**\n{module_desc}"
        # Setting detail view
        elif self.selected_path:
            meta = self.current_meta() or {}
            setting_name = meta.get("key", self.selected_path.split(".")[-1])
            desc = f"**{setting_name}**\n"

            if "description" in meta:
                desc += f"{meta['description']}\n\n"

            cur_val = _safe_get(self.cfg, self.selected_path)
            desc += f"**Type:** {meta.get('type', 'str')}\t\t**Access:** {_access_label(meta.get('access', 0))}\n"
            desc += f"**Current Value:** `{_format_value(cur_val)}`\n"

            if "min" in meta or "max" in meta:
                desc += f"**Range:** {meta.get('min','-')} to {meta.get('max','-')}\n"

            if "choices" in meta:
                desc += f"**Choices:** {', '.join(map(str, meta['choices']))}\n"
        # Root level view
        else:
            desc = "Select a module to configure its settings."

        if message:
            desc += f"\n\n✅ {message}"
        if error:
            desc += f"\n\n❌ {error}"

        embed = discord.Embed(
            title=title,
            description=f"{breadcrumb}\n\n{desc}",
            color=discord.Color.blurple(),
        )

        return embed

    async def start(self):
        self.message = await self.ctx.send(embed=self._embed(), view=self)


class SettingsMenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="settings", description="Open the interactive settings menu."
    )
    async def settings(self, ctx: commands.Context):
        guild_id = getattr(ctx.guild, "id", None)
        if not guild_id or not check_guild_config_available(guild_id):
            await ctx.send("Config not found. Please run !setup first.")
            return

        user_roles = [r.id for r in ctx.author.roles]
        if not check_admin_role(guild_id, user_roles):
            await ctx.send("You do not have permission to edit settings.")
            return

        schema = get_settings_schema()
        cfg = json_get(guild_id)
        view = SettingsView(self.bot, ctx, schema, cfg)
        await view.start()


async def setup(bot):
    await bot.add_cog(SettingsMenu(bot))
