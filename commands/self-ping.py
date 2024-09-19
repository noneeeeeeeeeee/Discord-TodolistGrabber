from discord.ext import commands
import discord

class PingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="self-ping", description="Ping yourself multiple times (max 1000)")
    async def self_ping(self, ctx: commands.Context, pings: int):
        if pings < 1:
            await ctx.send("Please provide a number greater than 0.")
            return
        if pings > 100:
            await ctx.send("Maximum pings allowed is 100.")
            return


        # for i in range(pings):
        #    await ctx.send(f"Ping {ctx.author.mention}! ({i + 1}/{pings})")
        await ctx.send("This command is currently on hold.")


async def setup(bot):
    await bot.add_cog(PingCog(bot))
