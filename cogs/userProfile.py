# cogs/profile.py
import asyncio
import discord
from discord.ext import commands
import aiohttp
from typing import Optional, List, Dict
from datetime import datetime, timezone

from config import ER_KEY
from db import SessionLocal
from models import User
from data import Character_Names, CURRENT_SEASON_NUM

ER_BASE = "https://open-api.bser.io/v1"

RANK_MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = ER_KEY

    # ── DB ──────────────────────────────────────────────────────────

    def get_active_nickname(self, user_id: str) -> Optional[str]:
        session = SessionLocal()
        try:
            user = session.get(User, user_id)
            return user.active_er_nickname if user and hasattr(user, "active_er_nickname") else None
        finally:
            session.close()

    # ── API 공통 ─────────────────────────────────────────────────────

    @property
    def _headers(self) -> dict:
        return {"x-api-key": self.api_key}

    async def _get(self, url: str, **params) -> Optional[dict]:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers=self._headers,
                params=params if params else None,
            ) as r:
                return await r.json() if r.status == 200 else None

    # ── API 개별 ─────────────────────────────────────────────────────

    async def fetch_user_info(self, nickname: str) -> Optional[dict]:
        """닉네임으로 userId + 기본 정보 반환"""
        data = await self._get(f"{ER_BASE}/user/nickname", query=nickname)
        return data.get("user") if data and data.get("user") else None

    async def fetch_user_stats(self, user_id: str, season_id: int = 0) -> List[dict]:
        """
        시즌 별 캐릭터 스탯 (season_id=0 → 전 시즌 합산)
        반환: userStats 리스트
        """
        data = await self._get(f"{ER_BASE}/user/stats/{user_id}/{season_id}")
        return (data.get("userStats") or []) if data else []

    async def fetch_user_games(self, user_id: str) -> List[dict]:
        """최근 게임 목록 (최신순)"""
        data = await self._get(f"{ER_BASE}/user/games/uid/{user_id}")
        return (data.get("userGames") or []) if data else []

    # ── 유틸 ─────────────────────────────────────────────────────────

    @staticmethod
    def format_playtime(seconds: int) -> str:
        if seconds <= 0:
            return "정보 없음"
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        if h > 0:
            return f"{h:,}시간 {m}분"
        return f"{m}분"

    @staticmethod
    def parse_dtm(dtm) -> Optional[datetime]:
        """startDtm: ISO 문자열 또는 Unix ms 정수 모두 처리"""
        if not dtm:
            return None
        try:
            if isinstance(dtm, (int, float)):
                return datetime.fromtimestamp(dtm / 1000, tz=timezone.utc)
            return datetime.fromisoformat(str(dtm).replace("Z", "+00:00"))
        except Exception:
            return None

    # ── 임베드 빌더 ──────────────────────────────────────────────────

    def build_embed(
        self,
        nickname: str,
        user_info: dict,
        stats_list: List[dict],
        games: List[dict],
    ) -> discord.Embed:

        embed = discord.Embed(
            title=f"🎮  {nickname}",
            color=0x5865F2,
            timestamp=datetime.now(),
        )

        # ── 계정 레벨 (최근 게임의 accountLevel) ──
        account_level = games[0].get("accountLevel") if games else None
        if account_level:
            embed.add_field(name="계정 레벨", value=f"**Lv. {account_level}**", inline=True)
        else:
            dakgg_url = f"https://dak.gg/er/players/{discord.utils.escape_markdown(nickname)}"
            embed.add_field(
                name="계정 레벨",
                value=f"[dak.gg에서 확인]({dakgg_url})",
                inline=True,
            )

        # ── 플레이타임: stats season 0 totalSecondsPlayed 합산 ──
        total_seconds = 0
        for stat_entry in stats_list:
            for cs in (stat_entry.get("characterStats") or []):
                total_seconds += cs.get("totalSecondsPlayed", 0) or 0

        embed.add_field(
            name="플레이타임",
            value=f"**{self.format_playtime(total_seconds)}**" if total_seconds else "정보 없음",
            inline=True,
        )

        embed.add_field(name="\u200b", value="\u200b", inline=True)

        # ── 마지막 게임 날짜 (games = 최근 90일치, 최신순) ──
        if games:
            last_dt = self.parse_dtm(games[0].get("startDtm"))
            if last_dt:
                embed.add_field(
                    name="마지막 게임",
                    value=f"**{last_dt:%Y.%m.%d %H:%M}**",
                    inline=True,
                )

        # ── 모스트 캐릭터 (전 시즌 totalGames 합산) ──
        char_totals: Dict[int, int] = {}
        for stat_entry in stats_list:
            for cs in (stat_entry.get("characterStats") or []):
                code = cs.get("characterCode", 0)
                if not code:
                    continue
                char_totals[code] = char_totals.get(code, 0) + (cs.get("totalGames", 0) or 0)

        # stats에 없으면 games characterNum 카운트로 대체
        if not char_totals and games:
            for g in games:
                code = g.get("characterNum", 0)
                if code:
                    char_totals[code] = char_totals.get(code, 0) + 1

        top_chars = sorted(char_totals.items(), key=lambda x: x[1], reverse=True)[:3]

        if top_chars:
            lines = []
            for rank, (code, count) in enumerate(top_chars, start=1):
                char_name = Character_Names.get(code, f"#{code}")
                medal = RANK_MEDAL.get(rank, f"{rank}.")
                lines.append(f"{medal} **{char_name}** — {count:,}판")

            embed.add_field(
                name="모스트 캐릭터",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(name="모스트 캐릭터", value="기록 없음", inline=False)

        embed.set_footer(text="이리와 봇 · 플레이어 프로필")
        return embed

    # ── 커맨드 ───────────────────────────────────────────────────────

    @commands.command(name="프로필", aliases=["ㅍㄹㅍ"])
    async def profile(self, ctx: commands.Context, *, nickname: str = None):
        """플레이어 프로필 조회"""
        author_id = str(ctx.author.id)

        if not nickname:
            nickname = self.get_active_nickname(author_id)
            if not nickname:
                return await ctx.reply(
                    embed=discord.Embed(
                        title="❌ 오류",
                        description=(
                            "`ㅇ등록 [닉네임]` 으로 먼저 등록하거나\n"
                            "`ㅇ프로필 [닉네임]` 으로 닉네임을 직접 입력해주세요."
                        ),
                        color=0xFF6B6B,
                    )
                )

        loading = await ctx.reply(f"🔍 **{nickname}** 님의 프로필을 불러오는 중...")

        try:
            user_info = await self.fetch_user_info(nickname)
            if not user_info:
                return await loading.edit(
                    content=f"❌ **{nickname}** 닉네임을 찾을 수 없습니다."
                )

            user_id = str(user_info["userId"])

            # 1 RPS 제한 — 순차 요청
            await asyncio.sleep(1)
            stats_list = await self.fetch_user_stats(user_id, 0)  # 0 = 전 시즌
            await asyncio.sleep(1)
            games = await self.fetch_user_games(user_id)

            embed = self.build_embed(nickname, user_info, stats_list, games)
            await loading.edit(content=None, embed=embed)

        except Exception as e:
            import traceback; traceback.print_exc()
            await loading.edit(content=f"❌ 오류 발생: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ProfileCog(bot))