import discord
from discord.ext import commands
from discord.ui import Button, View
from modules.setconfig import create_default_config
import os

class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def setup(self, ctx):
        guild_id = ctx.guild.id
        config_path = f"./config/{guild_id}.json"
        
        if os.path.exists(config_path):
            embed = discord.Embed(
            title="Setup Wizard",
            description="A configuration for this server already exists. Do you want to resetup?",
            color=discord.Color.red()
            )
            view = View(timeout=30)
            yes_button = Button(label="Yes", style=discord.ButtonStyle.green)
            no_button = Button(label="No", style=discord.ButtonStyle.red)

            async def yes_callback(interaction):
                await interaction.response.defer()
                await self.start_setup(ctx, interaction.message)
                yes_button.disabled = True
                no_button.disabled = True
                await message.edit(view=view)

            async def no_callback(interaction):
                await interaction.response.send_message("Setup aborted.", ephemeral=True)
                yes_button.disabled = True
                no_button.disabled = True
                await message.edit(view=view)

            async def on_timeout():
                yes_button.disabled = True
                no_button.disabled = True
                await message.edit(view=view)

            view.on_timeout = on_timeout
            yes_button.callback = yes_callback
            no_button.callback = no_callback
            view.add_item(yes_button)
            view.add_item(no_button)

            message = await ctx.send(embed=embed, view=view)
        else:
            await self.start_setup(ctx)

    async def start_setup(self, ctx, message=None):
        if message is None:
            message = await ctx.send("Starting Setup...")

        embed = discord.Embed(
            title="Setup Wizard",
            description="This will guide you through the basic setup for the discord bot to work properly.",
            color=discord.Color.blue()
        )
        embed.add_field(name=":arrow_right: Step 1: Admin User", value="Please mention the User that you would like to be the admin or provide their UserID.")
        embed.add_field(name=":hourglass: Step 2: Default Role", value="Please mention the role that you would like to be the default role or provide the RoleID.")
        
        def check_admin_user(m):
            return m.author == ctx.author and (m.mentions or m.content.isdigit())

        await message.edit(content=None, embed=embed)
        prompt_msg = await ctx.send("Please mention the User that you would like to be the admin or provide their UserID.")
        admin_msg = await self.bot.wait_for('message', check=check_admin_user)
        await prompt_msg.delete()
        await admin_msg.delete()

        if admin_msg.mentions:
            admin_user = admin_msg.mentions[0]
        else:
            admin_user = await self.bot.fetch_user(int(admin_msg.content))

        embed.set_field_at(0, name=":white_check_mark: Step 1: Admin User", value=f"Admin User set to {admin_user.mention}")
        embed.set_field_at(1, name=":arrow_right: Step 2: Default Role", value="Please mention the role that you would like to be the default role or provide the RoleID.")
        await message.edit(content=None, embed=embed)

        def check_default_role(m):
            return m.author == ctx.author and (m.role_mentions or m.content.isdigit())

        prompt_msg = await ctx.send("Please mention the role that you would like to be the default role or provide the RoleID.")
        role_msg = await self.bot.wait_for('message', check=check_default_role)
        await prompt_msg.delete()
        await role_msg.delete()

        if role_msg.role_mentions:
            default_role = role_msg.role_mentions[0]
        else:
            default_role = ctx.guild.get_role(int(role_msg.content))

        embed.set_field_at(1, name=":white_check_mark: Step 2: Default Role", value=f"Default Role set to {default_role.mention}")
        await message.edit(content=None, embed=embed)

        create_default_config(ctx.guild.id, admin_user.id, default_role.id)
        await ctx.send("Setup complete! The bot is now ready to use.")

async def setup(bot):
    await bot.add_cog(Setup(bot))