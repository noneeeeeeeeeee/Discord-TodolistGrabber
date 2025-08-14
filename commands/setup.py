import discord
from discord.ext import commands
from discord.ui import Button, View
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
                description=(
                    "This bot hasn't been setup by the administrator, please try again later."
                    if not os.path.exists(f"./config/{ctx.guild.id}.json")
                    else "You have insufficient permissions to run this command."
                ),
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            return

        config_path = f"./config/{ctx.guild.id}.json"

        if os.path.exists(config_path):
            embed = discord.Embed(
                title="Setup Wizard",
                description="A configuration for this server already exists. Do you want to resetup?",
                color=discord.Color.red(),
            )
            view = View(timeout=30)
            no_button = Button(label="No", style=discord.ButtonStyle.red)

            async def yes_callback(interaction):
                no_button.disabled = True
                await message.edit(view=view)
                await interaction.response.defer()
                await self.start_setup(ctx)

            async def no_callback(interaction):
                await interaction.response.send_message(
                    "Setup aborted.", ephemeral=True
                )
                no_button.disabled = True
                await message.edit(view=view)

            async def on_timeout():
                no_button.disabled = True
                await message.edit(view=view)

            view.on_timeout = on_timeout
            no_button.callback = no_callback
            view.add_item(no_button)

            message = await ctx.send(embed=embed, view=view)
        else:
            await self.start_setup(ctx)

    async def start_setup(self, ctx, message=None):
        if message is None:
            message = await ctx.send("Starting Setup...")

        setup_steps = [
            {
                "name": "Admin Role",
                "prompt": "Please mention the role that you would like to be the admin role or provide the RoleID.",
            },
            {
                "name": "Default Role",
                "prompt": "Please mention the role that you would like to be the default role or provide the RoleID.",
            },
            {
                "name": "Ping Role",
                "prompt": "Please mention the role that you would like to be the ping role or provide the RoleID.",
            },
            {
                "name": "Dj Role",
                "prompt": "Please mention the role that you would like to be the DJ role or provide the RoleID.",
            },
        ]

        embed = discord.Embed(
            title="Setup Wizard",
            description="Setup Has been Deprecated.. Please wait for a new setup in later versions",
            color=discord.Color.blue(),
        )

        for step in setup_steps:
            embed.add_field(
                name=f":hourglass: Step: {step['name']}",
                value=step["prompt"],
                inline=False,
            )

        view = View(timeout=300)
        cancel_button = Button(label="Cancel", style=discord.ButtonStyle.red)

        async def cancel_callback(interaction):
            await interaction.response.send_message("Setup canceled.", ephemeral=True)
            await message.delete()

        cancel_button.callback = cancel_callback
        view.add_item(cancel_button)

        await message.edit(content=None, embed=embed, view=view)

        # Store role IDs
        admin_role_id = None
        default_role_id = None
        ping_role_id = None
        dj_role_id = None

        for i, step in enumerate(setup_steps):
            prompt_msg = await ctx.send(step["prompt"])

            while True:

                def check_role(m):
                    return m.author == ctx.author and (
                        m.role_mentions or m.content.isdigit()
                    )

                try:
                    role_msg = await self.bot.wait_for(
                        "message", check=check_role, timeout=300.0
                    )

                    if role_msg.role_mentions:
                        role = role_msg.role_mentions[0]
                    else:
                        try:
                            role = ctx.guild.get_role(int(role_msg.content))
                        except ValueError:
                            role = None

                    if role is None:
                        await prompt_msg.delete()
                        prompt_msg = await ctx.send(step["prompt"])
                        await ctx.send(
                            "Invalid role ID provided. Please try again.",
                            delete_after=10,
                        )
                        await role_msg.delete()
                    else:
                        await prompt_msg.delete()
                        await role_msg.delete()

                        # Assign role ID to corresponding variable
                        if step["name"] == "Admin Role":
                            admin_role_id = role.id
                        elif step["name"] == "Default Role":
                            default_role_id = role.id
                        elif step["name"] == "Ping Role":
                            ping_role_id = role.id
                        elif step["name"] == "Dj Role":
                            dj_role_id = role.id

                        embed.set_field_at(
                            i,
                            name=f":white_check_mark: Step {i+1}: {step['name']}",
                            value=f"{step['name']} set to {role.mention}",
                            inline=False,
                        )
                        await message.edit(content=None, embed=embed, view=view)
                        break

                except asyncio.TimeoutError:
                    await ctx.send(
                        "You took too long to respond. Please try the setup command again."
                    )
                    return

        await ctx.send("Setup incomplete")

        cancel_button.disabled = True
        await message.edit(view=view)


async def setup(bot):
    await bot.add_cog(Setup(bot))
