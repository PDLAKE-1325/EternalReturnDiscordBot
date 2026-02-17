# cogs/router.py
import discord
from discord.ext import commands

BOT_CHANNEL_ID = [1471544493231575246, 1471866112621936681]   # 명령어 채널
CHAT_CHANNEL_ID = [1467018703974432954, 1455023431559938234]  # 자유채팅 채널

class MessageRouter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # 1️⃣ 명령어 전용 채널
        if message.channel.id in BOT_CHANNEL_ID:
            await self.bot.process_commands(message)
            return

        # 2️⃣ 자유채팅 채널 → 자연어
        # if message.channel.id in CHAT_CHANNEL_ID:
        #     ai_cog = self.bot.get_cog("AIChat") 
        #     if not ai_cog:
        #         return

        #     reply = await ai_cog.ask_ai(message, message.content)

        #     if not reply.strip():
        #         return  # 빈 응답이면 아무것도 안 함

        #     await message.channel.reply(reply)
        #     return


        #3️⃣ 그 외 채널 → 기본적으로 명령어만 허용
        await self.bot.process_commands(message)

async def setup(bot):
    await bot.add_cog(MessageRouter(bot))
