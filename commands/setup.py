import discord
from discord.ext import commands
from discord.ui import Button, View
from modules.setconfig import create_default_config
import os
import asyncio

class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def setup(self, ctx):
        if not ctx.author.guild_permissions.administrator:
            embed = discord.Embed(
                title=":x: Insufficient Permissions",
                description="This bot hasn't been setup by the administrator, please try again later.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        guild_id = ctx.guild.id
        config_path = f"./config/{guild_id}.json"
        
        if os.path.exists(config_path):
            if not ctx.author.guild_permissions.administrator:
                embed = discord.Embed(
                    title=":x: Insufficient Permissions",
                    description="You have insufficient permissions to run this command.",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            embed = discord.Embed(
                title="Setup Wizard",
                description="A configuration for this server already exists. Do you want to resetup?",
                color=discord.Color.red()
            )
            view = View(timeout=30)
            yes_button = Button(label="Yes", style=discord.ButtonStyle.green)
            no_button = Button(label="No", style=discord.ButtonStyle.red)

            async def yes_callback(interaction):
                yes_button.disabled = True
                no_button.disabled = True
                await message.edit(view=view)
                await interaction.response.defer()
                await self.start_setup(ctx)

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
        embed.add_field(name=":arrow_right: Step 1: Admin Role", value="Please mention the role that you would like to be the admin role or provide the RoleID.")
        embed.add_field(name=":hourglass: Step 2: Default Role", value="Please mention the role that you would like to be the default role or provide the RoleID.")
        embed.add_field(name=":hourglass: Step 3: Ping Role", value="Please mention the role that you would like to be the ping role or provide the RoleID.")
        
        view = View(timeout=300)
        cancel_button = Button(label="Cancel", style=discord.ButtonStyle.red)

        async def cancel_callback(interaction):
            await interaction.response.send_message("Setup canceled.", ephemeral=True)
            await message.delete()

        cancel_button.callback = cancel_callback
        view.add_item(cancel_button)

        await message.edit(content=None, embed=embed, view=view)
        prompt_msg = await ctx.send("Please mention the role that you would like to be the admin role or provide the RoleID.")
        
        def check_admin_role(m):
            return m.author == ctx.author and (m.role_mentions or m.content.isdigit())

        try:
            while True:
                try:
                    admin_msg = await self.bot.wait_for('message', check=check_admin_role, timeout=300.0)
                    await prompt_msg.delete()
                    await admin_msg.delete()

                    if admin_msg.role_mentions:
                        admin_role = admin_msg.role_mentions[0]
                    else:
                        try:
                            admin_role = ctx.guild.get_role(int(admin_msg.content))
                        except ValueError:
                            admin_role = None

                    if admin_role is None:
                        await ctx.send("Invalid role ID provided. Please try again.", delete_after=10)
                        continue

                    break
                except (ValueError, discord.NotFound, discord.HTTPException) as e:
                    await ctx.send(f"Error: {str(e)}. Please try again.", timeout=10)
                except asyncio.TimeoutError:
                    await ctx.send("You took too long to respond. Please try the setup command again.")
                    return

            embed.set_field_at(0, name=":white_check_mark: Step 1: Admin Role", value=f"Admin Role set to {admin_role.mention}")
            embed.set_field_at(1, name=":arrow_right: Step 2: Default Role", value="Please mention the role that you would like to be the default role or provide the RoleID.")
            embed.set_field_at(2, name=":hourglass: Step 3: Ping Role", value="Please mention the role that you would like to be the ping role or provide the RoleID.")
            await message.edit(content=None, embed=embed, view=view)

            def check_default_role(m):
                return m.author == ctx.author and (m.role_mentions or m.content.isdigit())

            while True:
                try:
                    prompt_msg = await ctx.send("Please mention the role that you would like to be the default role or provide the RoleID.")
                    role_msg = await self.bot.wait_for('message', check=check_default_role, timeout=60.0)
                    await prompt_msg.delete()
                    await role_msg.delete()
                    try:
                        default_role = ctx.guild.get_role(int(role_msg.content))
                    except ValueError:
                        default_role = None

                    if default_role is None:
                        await ctx.send("Invalid role ID provided. Please try again.", delete_after=10)
                        continue
                        default_role = ctx.guild.get_role(int(role_msg.content))
                
                    if default_role is None:
                        raise ValueError("Invalid role ID provided.")
                
                    break
                except (ValueError, discord.NotFound, discord.HTTPException) as e:
                    await ctx.send(f"Error: {str(e)}. Please try again.", timeout=10)
                except asyncio.TimeoutError:
                    await ctx.send("You took too long to respond. Please try the setup command again.")
                    return

            embed.set_field_at(1, name=":white_check_mark: Step 2: Default Role", value=f"Default Role set to {default_role.mention}")
            await message.edit(content=None, embed=embed, view=view)


            def check_ping_role(m):
                return m.author == ctx.author and (m.role_mentions or m.content.isdigit())

            while True:
                try:
                    prompt_msg = await ctx.send("Please mention the role that you would like to be the ping role or provide the RoleID.")
                    role_msg = await self.bot.wait_for('message', check=check_ping_role, timeout=60.0)
                    await prompt_msg.delete()
                    await role_msg.delete()

                    if role_msg.role_mentions:
                        ping_role = role_msg.role_mentions[0]
                    else:
                        try:
                            ping_role = ctx.guild.get_role(int(role_msg.content))
                        except ValueError:
                            ping_role = None

                    if ping_role is None:
                        await ctx.send("Invalid role ID provided. Please try again.", delete_after=10)
                        continue

                    break
                except (ValueError, discord.NotFound, discord.HTTPException) as e:
                    await ctx.send(f"Error: {str(e)}. Please try again.", timeout=10)
                except asyncio.TimeoutError:
                    await ctx.send("You took too long to respond. Please try the setup command again.")
                    return

            embed.set_field_at(2, name=":white_check_mark: Step 3: Ping Role", value=f"Ping Role set to {ping_role.mention}")
            await message.edit(content=None, embed=embed, view=view)

            create_default_config(ctx.guild.id, admin_role.id, default_role.id, ping_role.id)
            await ctx.send("Setup complete! The bot is now ready to use.")

            # Disable the cancel button after setup is complete
            cancel_button.disabled = True
            await message.edit(view=view)
        except asyncio.TimeoutError:
            await ctx.send("You took too long to respond. Please try the setup command again.")
            return

async def setup(bot):
    await bot.add_cog(Setup(bot))