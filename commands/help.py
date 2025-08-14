import discord
from discord.ext import commands
from modules.readversion import read_current_version
from modules.setconfig import json_get, check_guild_config_available


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="help", description="Displays the categorized help menu."
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def help(self, ctx: commands.Context, category: str = None):
        """
        Usage:
            !help                -> Full categorized help
            !help noticeboard    -> Details for the 'Noticeboard' category
        """
        guild_id = getattr(ctx.guild, "id", None)
        cfg = {}
        if guild_id and check_guild_config_available(guild_id):
            cfg = json_get(guild_id)

        nb_enabled = cfg.get("Noticeboard", {}).get("Enabled", True)
        music_enabled = cfg.get("Music", {}).get("Enabled", False)

        # Define categories and items: (command, short description, example usage)
        categories = {
            "General": [
                ("/help", "Show this help menu", "/help"),
                ("/ping", "Check bot latency", "/ping"),
                (
                    "/settings",
                    "Open interactive settings menu (admins only)",
                    "/settings",
                ),
            ],
            "Noticeboard": (
                [
                    (
                        "!noticeboard",
                        "View or manage the noticeboard (basic panel)",
                        "!noticeboard",
                    ),
                    (
                        "/settings category:Noticeboard",
                        "Edit noticeboard settings via interactive menu",
                        "/settings",
                    ),
                ]
                if nb_enabled
                else []
            ),
            "Music": (
                [
                    ("/play", "Play a track", "/play <url or search>"),
                    ("/queue", "Show current queue", "/queue"),
                    ("/skip", "Skip current track", "/skip"),
                    ("/volume", "Set player volume (0.0 - 1.0)", "/volume <0.5>"),
                ]
                if music_enabled
                else []
            ),
            "Admin": [
                ("!setup", "Run setup wizard (server administrators)", "!setup"),
                (
                    "/checkupdates",
                    "Check for bot OTA updates (owner/authorized)",
                    "/checkupdates",
                ),
            ],
            "Fun": [
                (
                    "/askgemini",
                    "Ask Gemini (AI) — rate limited per user",
                    "/askgemini <question>",
                ),
                (
                    "/self-ping",
                    "Ping yourself N times (small limits)",
                    "/self-ping <count>",
                ),
            ],
            "Utilities": [
                ("/apistatus", "Check backend API status", "/apistatus"),
                ("/motd", "Manage/view bot MOTD presence (owner)", "/motd help"),
            ],
        }

        # If a category is requested, show expanded view for that category only
        if category:
            cat = category.strip().title()
            if cat not in categories or not categories[cat]:
                desc = f"No commands found for category `{category}`."
            else:
                lines = []
                for cmd, desc_text, example in categories[cat]:
                    lines.append(f"**{cmd}** — {desc_text}\nUsage: `{example}`")
                desc = "\n\n".join(lines)
            embed = discord.Embed(
                title=f"Help — {cat}",
                description=desc,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Bot Version: {read_current_version()}")
            if hasattr(ctx, "interaction") and ctx.interaction:
                await ctx.interaction.response.send_message(
                    embed=embed, ephemeral=False
                )
            else:
                await ctx.send(embed=embed)
            return

        # Build full categorized embed
        embed = discord.Embed(
            title="Help Menu — Command Categories",
            description="Use `/help <category>` to see details for a specific category.\n\n"
            f"Noticeboard: {'✅ Enabled' if nb_enabled else '❌ Disabled'} • Music: {'✅ Enabled' if music_enabled else '❌ Disabled'}",
            color=discord.Color.blue(),
        )

        for cat_name, items in categories.items():
            if not items:
                continue
            lines = [
                f"**{cmd}** — {short}\n`{example}`" for cmd, short, example in items
            ]
            embed.add_field(name=cat_name, value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Bot Version: {read_current_version()}")

        # Respond for both text and slash seamlessly
        if hasattr(ctx, "interaction") and ctx.interaction:
            # interaction may already have been responded to by permissions — try to send appropriately
            try:
                await ctx.interaction.response.send_message(embed=embed)
            except Exception:
                await ctx.send(embed=embed)
        else:
            await ctx.send(embed=embed)

    @help.error
    async def help_error(self, ctx, error):
        # Unified cooldown handling for both text and slash calls
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = round(error.retry_after, 1)
            msg = f":hourglass: Please wait {retry_after} seconds before using this command again."
            if hasattr(ctx, "interaction") and ctx.interaction:
                try:
                    await ctx.interaction.response.send_message(msg, ephemeral=True)
                except Exception:
                    await ctx.send(msg)
            else:
                await ctx.send(msg)
        else:
            msg = ":x: An unexpected error occurred."
            if hasattr(ctx, "interaction") and ctx.interaction:
                try:
                    await ctx.interaction.response.send_message(msg, ephemeral=True)
                except Exception:
                    await ctx.send(msg)
            else:
                await ctx.send(msg)


async def setup(bot):
    await bot.add_cog(Help(bot))
