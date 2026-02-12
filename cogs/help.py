# cogs/help.py
import discord
from discord.ext import commands

from datetime import datetime

class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="도움", aliases=["ㄷㅇ"])
    async def record_help(self, ctx: commands.Context):
        embed = discord.Embed(
            title="이리와 전적 검색 도움말",
            description="사용 가능한 명령어 목록",
            color=0x0fb9b1,
            timestamp=datetime.now()
        )
        embed.add_field(
            name="유저 등록",
            value=(
                "`ㅇ등록 [닉네임]` - 닉네임 등록\n"
                "`ㅇ삭제` - 등록된 닉네임 삭제\n"
            ),
            inline=False
        )
        embed.add_field(
            name="전적 검색",
            value=(
                "`ㅇ전적 [닉네임]` - 전적 검색\n"
                "`ㅇ최근게임 [닉네임]` - 마지막 게임 전적 검색\n"
            ),
            inline=False
        )
        
        embed.add_field(
            name="기타",
            value=(
                "`ㅇ도움` - 도움말 표시\n"
                "`ㅇㅈㅈ` - 전적\n"
                "`ㅇㅊㄱㄱ` - 최근겜\n"
            ),
            inline=False
        )
        
        embed.set_footer(text="이리와 봇 - 명령어")
        await ctx.reply(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
