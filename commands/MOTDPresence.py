import asyncio
import discord
from discord.ext import commands, tasks
import random
from modules.enviromentfilegenerator import check_and_load_env_file
import os
from datetime import datetime, timedelta


class MOTDPresence(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.motd_list = self.load_motd_list()
        self.change_presence_task.start()
        self.next_refresh_time = datetime.utcnow() + timedelta(hours=6)

    check_and_load_env_file()
    owner_id = os.getenv("OWNER_ID")

    def load_motd_list(self):
        with open("modules/sentenceslist/MOTD_List.txt", "r", encoding="utf-8") as f:
            motd_list = [line.strip() for line in f if line.strip()]
        return motd_list

    @tasks.loop(hours=6)
    async def change_presence_task(self):
        motd = random.choice(self.motd_list)
        await self.bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=motd)
        )
        self.next_refresh_time = datetime.utcnow() + timedelta(hours=6)

    @change_presence_task.before_loop
    async def before_change_presence_task(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="motd")
    async def motd(self, ctx, action: str = None, numeral: int = None):
        if str(ctx.author.id) != self.owner_id:
            await ctx.send(
                "You do not have permission to use this command.", delete_after=5
            )
            return

        if action == "refresh_now":
            self.change_presence_task.restart()
            await ctx.send("MOTD presence refreshed.", delete_after=5)
        elif action == "set" and numeral is not None:
            if 1 <= numeral <= len(self.motd_list):
                motd = self.motd_list[numeral - 1]
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching, name=motd
                    )
                )
                await ctx.send(f"MOTD set to: {motd}", delete_after=5)
            else:
                await ctx.send("Invalid numeral provided.", delete_after=5)
        elif action == "toggle_auto_refresh":
            if self.change_presence_task.is_running():
                self.change_presence_task.stop()
                await ctx.send("MOTD updates stopped.", delete_after=5)
            else:
                self.change_presence_task.start()
                await ctx.send("MOTD updates started.", delete_after=5)
        elif action == "help":
            await ctx.send(
                "```Usage: !motd [action] [numeral]\n\nActions:\n- refresh_now: Refreshes the MOTD presence immediately\n- set [numeral]: Sets the MOTD presence to the specified numeral\n- toggle_auto_refresh: Toggles automatic MOTD presence updates\n- help: Displays this help message\n\nNumeral: The numeral of the MOTD to set the presence to```",
                delete_after=30,
            )
        else:
            await self.send_motd_list(ctx)

    async def send_motd_list(self, ctx):
        per_page = 10
        total_pages = (len(self.motd_list) - 1) // per_page + 1

        view = MOTDPaginator(ctx, self.motd_list, per_page, total_pages)
        await view.start()


class MOTDPaginator(discord.ui.View):
    def __init__(self, ctx, motd_list, per_page, total_pages):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.motd_list = motd_list
        self.per_page = per_page
        self.total_pages = total_pages
        self.current_page = 0

        self.update_buttons()

    async def start(self):
        self.message = await self.ctx.send(embed=self.create_embed(), view=self)

    def create_embed(self):
        start_index = self.current_page * self.per_page
        page_content = "\n".join(
            f"{start_index + i + 1}. {motd}"
            for i, motd in enumerate(
                self.motd_list[start_index : start_index + self.per_page]
            )
        )
        embed = discord.Embed(
            title=f"MOTD List - Page {self.current_page + 1}/{self.total_pages}",
            description=page_content,
        )
        embed.set_footer(
            text=f"Next MOTD refresh time: {self.ctx.cog.next_refresh_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\nLoop is {'running' if self.ctx.cog.change_presence_task.is_running() else 'stopped'}"
        )
        return embed

    async def interaction_check(self, interaction):
        return interaction.user == self.ctx.author

    async def disable_buttons(self):
        for button in self.children:
            button.disabled = True
        await self.message.edit(view=self)

    async def on_timeout(self):
        await self.disable_buttons()

    def update_buttons(self):
        self.first_page.disabled = self.current_page == 0
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page == self.total_pages - 1
        self.last_page.disabled = self.current_page == self.total_pages - 1

    @discord.ui.button(label="<<", style=discord.ButtonStyle.primary)
    async def first_page(self, interaction, button):
        self.current_page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="<", style=discord.ButtonStyle.primary)
    async def previous_page(self, interaction, button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(
                embed=self.create_embed(), view=self
            )

    @discord.ui.button(label=">", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction, button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(
                embed=self.create_embed(), view=self
            )

    @discord.ui.button(label=">>", style=discord.ButtonStyle.primary)
    async def last_page(self, interaction, button):
        self.current_page = self.total_pages - 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


async def setup(bot):
    await bot.add_cog(MOTDPresence(bot))
