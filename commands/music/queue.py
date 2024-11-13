import discord
from discord.ext import commands
from modules.readversion import read_current_version

class MusicQueue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="queue", aliases=["q"], description="Shows the Song Queue")
    async def queue(self, ctx):
        music_player = self.bot.get_cog("MusicPlayer")
        guild_id = ctx.guild.id

        if guild_id not in music_player.music_queue or not music_player.music_queue[guild_id] or music_player.now_playing.get(guild_id) is None:
            await ctx.send(":x: The queue is currently empty.")
            return

        queue = music_player.music_queue[guild_id]
        per_page = 5 
        max_pages = (len(queue) - 1) // per_page + 1

        view = QueuePaginator(ctx, queue, per_page, max_pages)
        await view.start()

class QueuePaginator(discord.ui.View):
    def __init__(self, ctx, queue, per_page, max_pages):
        super().__init__(timeout=10)  
        self.ctx = ctx
        self.queue = queue
        self.per_page = per_page
        self.max_pages = max_pages
        self.current_page = 0

        self.update_buttons()

    async def start(self):
        """Starts the paginator by sending the initial embed and setting up buttons."""
        self.message = await self.ctx.send(embed=self.create_embed(), view=self)

    def create_embed(self):
        """Creates an embed for the current page."""
        start_idx = self.current_page * self.per_page
        end_idx = start_idx + self.per_page
        page_queue = self.queue[start_idx:end_idx]

        music_player = self.ctx.bot.get_cog("MusicPlayer")
        guild_id = self.ctx.guild.id
        now_playing = music_player.now_playing.get(guild_id)

        queue_list = ""
        if now_playing:
            title = now_playing['title']
            duration = now_playing['duration']
            ogurl = now_playing['ogurl']
            queue_list += f"### :arrow_forward: [{title}]({ogurl}) ({duration // 60}:{duration % 60:02})\n"

        queue_list += "\n".join(
            f"{idx + 1}. [{title}]({ogurl}) ({duration // 60}:{duration % 60:02})"
            for idx, (author, url, ogurl, title, duration) in enumerate(page_queue, start=start_idx)
        )

        embed = discord.Embed(
            title="Current Song Queue",
            description=queue_list,
            color=discord.Color.blue()
        )
        embed.set_author(name=f"Page {self.current_page + 1}/{self.max_pages}")
        embed.set_footer(text=f"Bot Version: {read_current_version()}")
        return embed

    async def interaction_check(self, interaction):
        """Ensures only the original user can interact with the buttons."""
        return interaction.user == self.ctx.author

    async def disable_buttons(self):
        """Disable all buttons in the view."""
        for button in self.children:
            button.disabled = True
        await self.message.edit(view=self)

    async def on_timeout(self):
        """Disables the buttons when the view times out."""
        await self.disable_buttons()

    def update_buttons(self):
        """Update the state of the buttons based on the current page."""
        self.first_page.disabled = self.current_page == 0
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page == self.max_pages - 1
        self.last_page.disabled = self.current_page == self.max_pages - 1

    # Button callbacks
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
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label=">", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction, button):
        if self.current_page < self.max_pages - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.primary)
    async def last_page(self, interaction, button):
        self.current_page = self.max_pages - 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

async def setup(bot):
    await bot.add_cog(MusicQueue(bot))
