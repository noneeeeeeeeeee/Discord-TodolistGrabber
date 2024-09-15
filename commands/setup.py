import discord
from discord.ext import commands
from modules.setconfig import create_default_config

class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def setup(self, ctx):
        print("Starting setup command")
        message = await ctx.send("Starting Setup...")
        print(f"Message sent: {message}")
        
        embed = discord.Embed(
            title="Setup Wizard",
            description=f"This will guide you through the basic setup for the discord bot to work properly.",
            color=discord.Color.blue()
        )
        
        def check_admin_user(m):
            return m.author == ctx.author and m.mentions

        await message.edit(content=None, embed=embed)
        await ctx.send("Please mention the User that you would like to be the admin.")
        admin_msg = await self.bot.wait_for('message', check=check_admin_user)
        print(f"Admin message received: {admin_msg}")
        admin_user = admin_msg.mentions[0]

        embed.add_field(name=":white_check_mark: Step 1: Admin User", value=f"Admin User set to {admin_user.mention}")
        embed.add_field(name=":arrow_right: Step 2: Default Role", value="Please mention the role that you would like to be the default role.")
        await message.edit(content=None, embed=embed)

        def check_default_role(m):
            return m.author == ctx.author and m.role_mentions

        await ctx.send("Please mention the role that you would like to be the default role.")
        role_msg = await self.bot.wait_for('message', check=check_default_role)
        print(f"Role message received: {role_msg}")
        default_role = role_msg.role_mentions[0]

        embed.set_field_at(1, name=":white_check_mark: Step 2: Default Role", value=f"Default Role set to {default_role.mention}")
        await message.edit(content=None, embed=embed)

        create_default_config(ctx.guild.id, admin_user.id, default_role.id)
        print("Default config created")

        await message.edit(content=None, embed=embed)

async def setup(bot):
    await bot.add_cog(Setup(bot))