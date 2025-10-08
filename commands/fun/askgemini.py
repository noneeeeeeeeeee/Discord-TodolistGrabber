import asyncio
import discord
from discord.ext import commands
from google import genai
from google.genai import types
import json
import os
import time
from typing import Optional, List
from modules.enviromentfilegenerator import check_and_load_env_file

check_and_load_env_file()


GEMINI_MODEL = "gemini-2.5-flash"


def _load_gemini_keys() -> List[str]:
    raw_keys = os.getenv("GeminiApiKeys", "").strip()
    if not raw_keys:
        return []

    parsed = None
    try:
        parsed = json.loads(raw_keys)
    except json.JSONDecodeError:
        parsed = None

    candidates: List[str] = []
    if isinstance(parsed, list):
        candidates = [str(item).strip() for item in parsed if str(item).strip()]
    elif isinstance(parsed, str):
        if parsed.strip():
            candidates = [parsed.strip()]
    else:
        candidates = [frag.strip() for frag in raw_keys.split(",") if frag.strip()]

    seen = set()
    deduped: List[str] = []
    for key in candidates:
        if key and key not in seen:
            deduped.append(key)
            seen.add(key)
    return deduped


def _get_primary_gemini_key() -> Optional[str]:
    keys = _load_gemini_keys()
    return keys[0] if keys else None


class AskGemini(commands.Cog):
    def __init__(self, bot, client: Optional[genai.Client]):
        self.bot = bot
        self.user_usage = {}
        self._client = client
        self._generation_config = (
            types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            )
            if client
            else None
        )

    @commands.hybrid_command(
        name="askgemini", description="Ask a question to the Gemini API."
    )
    async def askgemini(self, ctx: commands.Context, *, prompt: str = None):
        if prompt is None:
            embed = discord.Embed(
                title="Missing Argument",
                description="Please provide a prompt to ask Gemini.",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            return

        if self._client is None:
            embed = discord.Embed(
                title="Gemini Not Configured",
                description="No Gemini API key is available; please contact the bot owner.",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            return

        current_time = time.time()
        user_id = ctx.author.id
        if user_id not in self.user_usage:
            self.user_usage[user_id] = {"count": 0, "last_reset": current_time}

        user_data = self.user_usage[user_id]

        if current_time - user_data["last_reset"] > 1800:
            user_data["count"] = 0
            user_data["last_reset"] = current_time

        if user_data["count"] >= 5:
            embed = discord.Embed(
                title="Usage Limit Reached",
                description="You have reached your limit of 5 requests per 30 minutes. Please try again later.",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
            return

        initial_message = await ctx.send("Generating response, please wait...")

        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
                config=self._generation_config,
            )
            answer = response.text if getattr(response, "text", "") else None
            if not answer:
                answer = "No valid response from Gemini API."
        except Exception as e:
            answer = f"An error occurred while contacting the Gemini API: {str(e)}"

        if len(answer) > 3999:
            await initial_message.edit(content=f"# Gemini Response\n{answer}")
        else:
            embed = discord.Embed(
                title="Gemini Response",
                description=answer,
                color=(
                    discord.Color.green()
                    if "error" not in answer.lower()
                    else discord.Color.red()
                ),
            )
            user_data["count"] += 1
            embed.set_footer(text=f"Requests remaining: {5 - user_data['count']}/5")
            await initial_message.edit(content=None, embed=embed)

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.tree.sync()


async def setup(bot):
    try:
        primary_key = _get_primary_gemini_key()
        if not primary_key:
            print(
                "Failed to load AskGemini cog: GeminiApiKeys is not configured. The command will be disabled."
            )
            return

        client = genai.Client(api_key=primary_key)
        await bot.add_cog(AskGemini(bot, client))
    except Exception as e:
        print(f"Failed to load AskGemini cog: {str(e)}. It will now be disabled")
