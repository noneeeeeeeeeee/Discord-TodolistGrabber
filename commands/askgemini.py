import discord
from discord.ext import commands
import google.generativeai as genai
import os
import time
from modules.enviromentfilegenerator import check_and_load_env_file

# Ensure environment variables are loaded
check_and_load_env_file()

# Configure the Generative AI client
genai.configure(api_key=os.getenv('GeminiApiKey'))

class AskGemini(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_usage = {}  # Dictionary to track usage limits
        self.model = genai.GenerativeModel("gemini-1.5-flash")

    @commands.hybrid_command(name='askgemini', description='Ask a question to the Gemini API.')
    async def askgemini(self, ctx: commands.Context, *, prompt: str = None):
        """Handle the /askgemini and !askgemini commands."""
        if prompt is None:
            # If prompt is not provided, send an error message
            embed = discord.Embed(
                title="Missing Argument",
                description="Please provide a prompt to ask Gemini.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        # Check usage limit
        current_time = time.time()
        user_id = ctx.author.id
        if user_id not in self.user_usage:
            self.user_usage[user_id] = {'count': 0, 'last_reset': current_time}

        user_data = self.user_usage[user_id]

        # Reset usage count if 30 minutes have passed
        if current_time - user_data['last_reset'] > 1800:  # 1800 seconds = 30 minutes
            user_data['count'] = 0
            user_data['last_reset'] = current_time

        if user_data['count'] >= 5:
            embed = discord.Embed(
                title="Usage Limit Reached",
                description="You have reached your limit of 5 requests per 30 minutes. Please try again later.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        # Notify user that the request is being processed
        initial_message = await ctx.send("Generating response, please wait...")

        # Proceed with API request
        try:
            # Call the API, passing the prompt directly as a string
            response = self.model.generate_content(prompt)
            answer = response.text if response.text else "No valid response from Gemini API."  # Set a default response
        except Exception as e:
            # Handle exceptions during API request
            answer = f"An error occurred while contacting the Gemini API: {str(e)}"

        # Build the response embed
        embed = discord.Embed(
            title="Gemini Response",
            description=answer,
            color=discord.Color.green() if 'error' not in answer.lower() else discord.Color.red()
        )

        user_data['count'] += 1
        embed.set_footer(text=f"Requests remaining: {5 - user_data['count']}/5")
        await initial_message.edit(content=None, embed=embed)

    @commands.Cog.listener()
    async def on_ready(self):
        # Sync the slash commands with Discord
        await self.bot.tree.sync()

# Correct `setup` function
async def setup(bot):
    await bot.add_cog(AskGemini(bot))
