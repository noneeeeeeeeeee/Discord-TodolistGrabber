import discord
from discord.ext import commands, tasks
import random

class MOTDPresence(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.motd_list = self.load_motd_list()
        self.change_presence_task.start()

    def load_motd_list(self):
        with open('modules/sentenceslist/MOTD_List.txt', 'r', encoding='utf-8') as f:
            motd_list = [line.strip() for line in f if line.strip()]
        return motd_list

    @tasks.loop(hours=6)
    async def change_presence_task(self):
        motd = random.choice(self.motd_list)
        await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=motd))

    @change_presence_task.before_loop
    async def before_change_presence_task(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(MOTDPresence(bot))