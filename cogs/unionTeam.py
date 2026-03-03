# cogs/unionTeam.py
import discord
from discord.ext import commands
import aiohttp
from typing import Optional, List, Dict
from datetime import datetime

from config import ER_KEY
from db import SessionLocal
from models import User
from data import CURRENT_SEASON, CURRENT_SEASON_NUM

ER_BASE = "https://open-api.bser.io/v1"

# 유니온 팀 티어 매핑
UNION_TIER_MAP = {
    1: ("S", 0xFF6B6B, "<:UnionS:1475215908665299035>"),
    2: ("A", 0xFFA500, "<:UnionA:1475215920313139261>"),
    3: ("B", 0x5865F2, "<:UnionB:1475215913778413609>"),
    4: ("C", 0x43B581, "<:UnionC:1475215912083652760>"),
    5: ("D", 0x7289DA, "<:UnionD:1475215904789762169>"),
}


class UnionTeamCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = ER_KEY
        self.base_url = ER_BASE

    def get_active_nickname(self, user_id: str) -> Optional[str]:
        """DB에서 활성화된 닉네임 가져오기"""
        session = SessionLocal()
        try:
            user = session.get(User, user_id)
            if user and hasattr(user, 'active_er_nickname'):
                return user.active_er_nickname
            return None
        finally:
            session.close()

    async def fetch_user_id(self, nickname: str) -> Optional[str]:
        """닉네임으로 유저 UID 조회"""
        headers = {"x-api-key": self.api_key}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/user/nickname",
                params={"query": nickname},
                headers=headers
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                user = data.get("user", {})
                # API 명세 응답 키는 "uid" (userId 아님)
                return user.get("userId")

    async def fetch_union_teams(self, user_id: str, season_id: int) -> Optional[List[Dict]]:
        """유니온 팀 정보 조회"""
        headers = {"x-api-key": self.api_key}
        url = f"{self.base_url}/unionTeam/uid/{user_id}/{season_id}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("teams")

    def calculate_win_rate(self, team_data: Dict) -> float:
        """팀의 총 승률 계산"""
        tier_keys = [
            ("ssstt", "ssstw"), ("sstt", "sstw"), ("stt", "stw"),
            ("aaatt", "aaatw"), ("aatt", "aatw"), ("att", "atw"),
            ("bbbtt", "bbbtw"), ("bbtt", "bbtw"), ("btt", "btw"),
            ("ccctt", "ccctw"), ("cctt", "cctw"), ("ctt", "ctw"),
            ("dddtt", "dddtw"), ("ddtt", "ddtw"), ("dtt", "dtw"),
            ("ett", "etw"),
            ("ffftt", "ffftw"), ("fftt", "fftw"), ("ftt", "ftw"),
        ]

        total_wins = 0
        total_games = 0
        for games_key, wins_key in tier_keys:
            total_games += team_data.get(games_key, 0)
            total_wins  += team_data.get(wins_key, 0)

        return (total_wins / total_games * 100) if total_games else 0.0

    def get_tier_info(self, tier_num: int) -> tuple:
        return UNION_TIER_MAP.get(tier_num, ("?", 0x808080, "❓"))

    def create_union_team_embed(self, team_data: Dict, season_num: int) -> discord.Embed:
        """유니온 팀 정보를 임베드로 생성"""
        team_name    = team_data.get("tnm", "Unknown Team")
        current_tier = team_data.get("ti", 0)
        highest_tier = team_data.get("ssti", 0)

        tier_name, tier_color, tier_emoji       = self.get_tier_info(current_tier)
        highest_tier_name, _, _                 = self.get_tier_info(highest_tier)

        win_rate = self.calculate_win_rate(team_data)

        embed = discord.Embed(
            title=f"{tier_emoji} {team_name}",
            description=f"**시즌 {season_num}** 유니온 팀 정보",
            color=tier_color,
            timestamp=datetime.now()
        )

        embed.add_field(
            name="티어 정보",
            value=f"현재: **{tier_name} 티어** | 최고: **{highest_tier_name} 티어**",
            inline=False
        )

        s_tickets   = team_data.get("stt", 0)
        ss_tickets  = team_data.get("sstt", 0)
        sss_tickets = team_data.get("ssstt", 0)

        embed.add_field(
            name="티켓 보유",
            value=f"S: **{s_tickets}** | SS: **{ss_tickets}** | SSS: **{sss_tickets}**",
            inline=False
        )

        embed.add_field(
            name="전체 승률",
            value=f"**{win_rate:.1f}%**",
            inline=True
        )

        top_tiers = [
            ("SSS 티어", "ssstt", "ssstw"),
            ("SS 티어",  "sstt",  "sstw"),
            ("S 티어",   "stt",   "stw"),
        ]
        performance = []
        for label, games_key, wins_key in top_tiers:
            games = team_data.get(games_key, 0)
            wins  = team_data.get(wins_key, 0)
            if games > 0:
                rate = wins / games * 100
                performance.append(f"{label}: {wins}승 {games}경기 ({rate:.0f}%)")

        if performance:
            embed.add_field(
                name="주요 티어 성적",
                value="\n".join(performance),
                inline=False
            )

        created_time = team_data.get("cdt", 0)
        updated_time = team_data.get("udt", 0)
        if created_time and updated_time:
            created = datetime.fromtimestamp(created_time / 1000).strftime("%Y.%m.%d")
            updated = datetime.fromtimestamp(updated_time / 1000).strftime("%Y.%m.%d %H:%M")
            embed.add_field(
                name="팀 정보",
                value=f"생성: {created} | 업데이트: {updated}",
                inline=False
            )

        embed.set_footer(text="이리와 봇 · 유니온 팀 정보")
        return embed

    @commands.command(name="유니온", aliases=["ㅇㄴㅇㄴ", "union"])
    async def union_team_info(self, ctx: commands.Context, *, nickname: str = None):
        """유니온 팀 정보 조회"""
        if not nickname:
            nickname = self.get_active_nickname(str(ctx.author.id))
            if not nickname:
                embed = discord.Embed(
                    title="❌ 오류",
                    description="닉네임을 입력하거나 먼저 `ㅇ등록 [닉네임]`으로 등록해주세요.",
                    color=0xFF6B6B
                )
                return await ctx.reply(embed=embed)

        loading_msg = await ctx.reply(f"🔍 **{nickname}** 님의 유니온 팀 정보를 불러오는 중...")

        try:
            user_api_id = await self.fetch_user_id(nickname)
            if not user_api_id:
                return await loading_msg.edit(
                    content=f"❌ **{nickname}** 닉네임을 찾을 수 없습니다."
                )

            # fetch_seasons() 호출은 결과를 사용하지 않으므로 제거
            # data.py의 CURRENT_SEASON_NUM / CURRENT_SEASON 을 그대로 사용
            teams_data = await self.fetch_union_teams(user_api_id, CURRENT_SEASON_NUM)

            if not teams_data:
                return await loading_msg.edit(
                    content=f"❌ **{nickname}** 님의 유니온 팀 정보가 없습니다."
                )

            embeds = [
                self.create_union_team_embed(team, CURRENT_SEASON)
                for team in teams_data[:10]
            ]
            await loading_msg.edit(content=None, embeds=embeds)

        except Exception as e:
            await loading_msg.edit(content=f"❌ 오류 발생: {str(e)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(UnionTeamCog(bot))