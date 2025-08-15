import discord
from discord.ext import commands
import os
import json
from modules.enviromentfilegenerator import check_and_load_env_file
from discord.ui import Button, View
from modules.setconfig import check_guild_config_available, cache_read_latest


class WorkHistory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="workhistory",
        description="Show past week's work and upcoming dues.",
    )
    async def workhistory(self, ctx: commands.Context):
        """
        Summarize the past week's and upcoming tasks from the latest cached snapshot.
        """
        guild_id = ctx.guild.id if ctx.guild else None
        if not guild_id or not check_guild_config_available(guild_id):
            await ctx.send("Config not found. Please run !setup first.")
            return

        # read latest task cache (all)
        try:
            task_data_str = cache_read_latest("all")
            if not task_data_str:
                await ctx.send("No cached task data available.")
                return
            task_data = json.loads(task_data_str)
        except Exception:
            await ctx.send("Failed to read cached data.")
            return

        # Build summary for the last 7 days and upcoming 7 days
        from datetime import datetime, timedelta

        today = datetime.now().date()
        week_ago = today - timedelta(days=7)
        upcoming_limit = today + timedelta(days=7)

        past_lines = []
        upcoming_lines = []

        for date_str, tasks in task_data.get("data", {}).items():
            if date_str == "unknown-due":
                continue
            try:
                d = datetime.strptime(date_str, "%A, %d-%m-%Y").date()
            except Exception:
                continue
            if week_ago <= d <= today:
                for t in tasks:
                    past_lines.append(
                        f"{d.strftime('%d %b')} — [{t['task']}] {t['subject']}: {t['description']}"
                    )
            if today < d <= upcoming_limit:
                for t in tasks:
                    upcoming_lines.append(
                        f"{d.strftime('%d %b')} — [{t['task']}] {t['subject']}: {t['description']}"
                    )

        embed = discord.Embed(
            title="Work History (7d past / 7d upcoming)", color=discord.Color.blue()
        )
        if past_lines:
            embed.add_field(
                name="Past 7 days",
                value="\n".join(past_lines[:25]),
                inline=False,
            )
        else:
            embed.add_field(
                name="Past 7 days",
                value="No completed/recorded work.",
                inline=False,
            )

        if upcoming_lines:
            embed.add_field(
                name="Upcoming (next 7 days)",
                value="\n".join(upcoming_lines[:25]),
                inline=False,
            )
        else:
            embed.add_field(
                name="Upcoming (next 7 days)",
                value="No upcoming work.",
                inline=False,
            )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(WorkHistory(bot))
