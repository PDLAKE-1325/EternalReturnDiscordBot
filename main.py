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
    # guild = discord.Object(id=1467018703144222753)

    # # 1) 길드(서버) 명령어 삭제
    # bot.tree.clear_commands(guild=guild)
    # await bot.tree.sync(guild=guild)
    # print("-길드 슬래시 명령어 삭제 + sync 완료")

    # # 2) 글로벌 명령어 삭제
    # bot.tree.clear_commands(guild=None)
    # await bot.tree.sync()
    # print("-글로벌 슬래시 명령어 삭제 + sync 완료")
    print(f"봇 로그인 완료: {bot.user} (ID: {str(bot.user.id)[:5]}****)")
    await bot.change_presence(
        activity=discord.Game(name=GAME_STATUS)
    )
    
@bot.event
async def on_message(message):
    pass

async def load_cogs():
    extensions = [
        "cogs.help",
        "cogs.record",
        "cogs.account",
        "cogs.userRank",
        "cogs.router",
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

