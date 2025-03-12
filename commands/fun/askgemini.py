import discord
from discord.ext import commands
import google.generativeai as genai
import os
import time
from modules.enviromentfilegenerator import check_and_load_env_file

check_and_load_env_file()


class AskGemini(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_usage = {}
        self.model = genai.GenerativeModel("gemini-1.5-flash")

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
            response = self.model.generate_content(prompt)
            answer = (
                response.text if response.text else "No valid response from Gemini API."
            )
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
        genai.configure(api_key=os.getenv("GeminiApiKey"))
        await bot.add_cog(AskGemini(bot))
    except Exception as e:
        print(f"Failed to load AskGemini cog: {str(e)}. It will now be disabled")
