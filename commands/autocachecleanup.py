import asyncio
import os
import time
from modules.cache import cachecleanup, delete_redundant_cache_files
from datetime import datetime, timedelta


class AutoCacheCleanup:
    def __init__(self, bot):
        self.bot = bot

    async def start_cache_cleanup(self):
        ping_daily_time = "12:00"
        ping_time = datetime.strptime(ping_daily_time, "%H:%M")
        while True:
            current_time = datetime.now() + timedelta(hours=7)
            if (
                current_time.hour == ping_time.hour
                and current_time.minute == ping_time.minute
            ):
                print(
                    f"Running cache cleanup at {current_time.strftime('%H:%M:%S')} GMT+7"
                )
                cachecleanup()
                delete_redundant_cache_files()
                await asyncio.sleep(86400)
            else:
                await asyncio.sleep(60)


async def setup(bot):
    cache_cleanup = AutoCacheCleanup(bot)
    bot.loop.create_task(cache_cleanup.start_cache_cleanup())
    bot.loop.create_task(cache_cleanup.start_cache_cleanup())
