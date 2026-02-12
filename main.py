import asyncio
import discord
from discord.ext import commands

# DB 임포트
from db import init_db

from config import DISCORD_TOKEN, PREFIXES, GAME_STATUS

# Intents 설정
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=PREFIXES, 
    help_command=None, 
    intents=intents)


@bot.event
async def on_ready():
    print(f"봇 로그인 완료: {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Game(name=GAME_STATUS)
    )

async def load_cogs():
    extensions = [
        "cogs.help",
        "cogs.record",
        "cogs.account",
        "cogs.userInfo",
        "cogs.userRank",
    ]

    for ext in extensions:
        try:
            await bot.load_extension(ext)
            print(f"[EXT] 로드 완료: {ext}")
        except Exception as e:
            print(f"[EXT] 로드 실패: {ext} - {e}")


async def main():
    init_db()
    async with bot:
        await load_cogs()
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

