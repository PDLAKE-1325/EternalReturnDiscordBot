# cogs/unionTeam.py
import discord
from discord.ext import commands
import aiohttp
from typing import Optional, List, Dict
from datetime import datetime

from config import ER_KEY
from db import SessionLocal
from models import User
from data import Character_Names, Weapon_Types, CURRENT_SEASON_NUM

ER_BASE    = "https://open-api.bser.io/v1"
UNION_MATCHING_MODE = 8

# 시즌 ID → 한글 표기
SEASON_NAMES: Dict[int, str] = {
    1: "EA 시즌 1",    2: "EA 프리시즌 2", 3: "EA 시즌 2",
    4: "EA 프리시즌 3", 5: "EA 시즌 3",    6: "EA 프리시즌 4",
    7: "EA 시즌 4",    8: "EA 프리시즌 5", 9: "EA 시즌 5",
    10: "EA 프리시즌 6", 11: "EA 시즌 6",  12: "EA 프리시즌 7",
    13: "EA 시즌 7",   14: "EA 프리시즌 8", 15: "EA 시즌 8",
    16: "EA 프리시즌 9", 17: "EA 시즌 9",  18: "프리시즌 1",
    19: "시즌 1",      20: "프리시즌 2",   21: "시즌 2",
    22: "프리시즌 3",   23: "시즌 3",      24: "프리시즌 4",
    25: "시즌 4",      26: "프리시즌 5",   27: "시즌 5",
    28: "프리시즌 6",   29: "시즌 6",      30: "프리시즌 7",
    31: "시즌 7",      32: "프리시즌 8",   33: "시즌 8",
    34: "프리시즌 9",   35: "시즌 9",      36: "프리시즌 10",
    37: "시즌 10",
}

def get_season_name(season_id: int) -> str:
    if season_id in SEASON_NAMES:
        return SEASON_NAMES[season_id]
    if season_id > 37:
        offset = season_id - 37
        num    = 10 + (offset + 1) // 2
        return f"프리시즌 {num}" if offset % 2 == 1 else f"시즌 {num}"
    return f"시즌 {season_id}"


UNION_TIER_MAP: Dict[int, tuple] = {
    1: ("S", 0xFF6B6B, "<:UnionS:1475215908665299035>"),
    2: ("A", 0xFFA500, "<:UnionA:1475215920313139261>"),
    3: ("B", 0x5865F2, "<:UnionB:1475215913778413609>"),
    4: ("C", 0x43B581, "<:UnionC:1475215912083652760>"),
    5: ("D", 0x7289DA, "<:UnionD:1475215904789762169>"),
}

RANK_MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}

WIN_TIER_KEYS = [
    ("SSS", "ssstw"), ("SS",  "sstw"),  ("S",   "stw"),
    ("AAA", "aaatw"), ("AA",  "aatw"),  ("A",   "atw"),
    ("BBB", "bbbtw"), ("BB",  "bbtw"),  ("B",   "btw"),
    ("CCC", "ccctw"), ("CC",  "cctw"),  ("C",   "ctw"),
    ("DDD", "dddtw"), ("DD",  "ddtw"),  ("D",   "dtw"),
    ("E",   "etw"),
    ("FFF", "ffftw"), ("FF",  "fftw"),  ("F",   "ftw"),
]


# ------------------------------------------------------------------ #
#  드롭다운 View
# ------------------------------------------------------------------ #

class UnionSeasonView(discord.ui.View):
    def __init__(self, cog: "UnionTeamCog", author_id: str,
                 nickname: str, seasons: List[Dict]):
        super().__init__(timeout=300)
        self.cog       = cog
        self.author_id = author_id
        self.nickname  = nickname
        self.seasons   = seasons          # [{seasonID, _games, _teams, isCurrent}, ...]
        self.selected  = seasons[0]
        self.message: Optional[discord.Message] = None
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        if len(self.seasons) <= 1:
            return  # 시즌 1개면 드롭다운 불필요

        cur_id = self.selected["seasonID"]
        options = [
            discord.SelectOption(
                label=get_season_name(s["seasonID"]),
                value=str(s["seasonID"]),
                emoji="🟢" if s["seasonID"] == CURRENT_SEASON_NUM else "⚪",
                default=(s["seasonID"] == cur_id),
            )
            for s in self.seasons[:25]
        ]
        sel = discord.ui.Select(placeholder="📅 시즌 선택", options=options)
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.author_id:
            return await interaction.response.send_message(
                "❌ 명령어를 사용한 사람만 선택할 수 있습니다.", ephemeral=True
            )
        await interaction.response.defer()

        sid = int(interaction.data["values"][0])
        self.selected = next((s for s in self.seasons if s["seasonID"] == sid), self.selected)

        self._rebuild()
        embed = self.cog.build_embed(self.selected, self.nickname)
        if self.message:
            await self.message.edit(embed=embed, view=self)


# ------------------------------------------------------------------ #
#  Cog
# ------------------------------------------------------------------ #

class UnionTeamCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot     = bot
        self.api_key = ER_KEY

    # ---- DB ----

    def get_active_nickname(self, user_id: str) -> Optional[str]:
        session = SessionLocal()
        try:
            user = session.get(User, user_id)
            return user.active_er_nickname if user and hasattr(user, "active_er_nickname") else None
        finally:
            session.close()

    # ---- API 공통 ----

    @property
    def _headers(self) -> dict:
        return {"x-api-key": self.api_key}

    async def _get(self, url: str, **params) -> Optional[dict]:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=self._headers,
                             params=params if params else None) as r:
                return await r.json() if r.status == 200 else None

    # ---- API 개별 ----

    async def fetch_user_id(self, nickname: str) -> Optional[str]:
        data = await self._get(f"{ER_BASE}/user/nickname", query=nickname)
        return data["user"]["userId"] if data and data.get("user") else None



    async def fetch_union_teams(self, user_id: str, season_id: int) -> List[Dict]:
        data = await self._get(f"{ER_BASE}/unionTeam/uid/{user_id}/{season_id}")
        return (data.get("teams") or []) if data else []

    async def fetch_user_games(self, user_id: str) -> List[Dict]:
        data = await self._get(f"{ER_BASE}/user/games/uid/{user_id}")
        return (data.get("userGames") or []) if data else []

    # ---- 시즌 목록 구성 ----

    async def build_season_list(self, user_id: str, union_games: List[Dict]) -> List[Dict]:
        """
        유니온은 시즌 6(ID 29)부터 도입. 시즌만 해당(프리시즌 제외).
        CURRENT_SEASON_NUM부터 29까지 2씩 내려가며 조회.
        """
        result: List[Dict] = []
        sid = CURRENT_SEASON_NUM
        while sid >= 29:
            games = [g for g in union_games if g.get("seasonId") == sid]
            teams = await self.fetch_union_teams(user_id, sid)
            if games or teams:
                result.append({
                    "seasonID":  sid,
                    "isCurrent": 1 if sid == CURRENT_SEASON_NUM else 0,
                    "_games":    games,
                    "_teams":    teams or [],
                })
            sid -= 2
        return result

    # ---- 임베드 빌더 ----

    def _tier_info(self, tier_num: int) -> tuple:
        return UNION_TIER_MAP.get(tier_num, ("?", 0x808080, "❓"))

    def build_embed(self, season_entry: Dict, nickname: str) -> discord.Embed:
        season_id = season_entry["seasonID"]
        teams     = season_entry.get("_teams") or []
        games     = season_entry.get("_games") or []
        is_cur    = season_entry.get("isCurrent", 0) == 1

        primary   = teams[0] if teams else {}
        _, color, _ = self._tier_info(primary.get("ti", 0))

        embed = discord.Embed(
            title=f"🤝 {nickname}의 유니온 정보",
            description=(
                f"**{get_season_name(season_id)}**"
                + (" `현재 시즌`" if is_cur else "")
            ),
            color=color,
            timestamp=datetime.now(),
        )

        # ── 팀 정보 ──
        if teams:
            for team in teams:
                t_name = team.get("tnm", "Unknown")
                tn, _, te = self._tier_info(team.get("ti", 0))
                hn, _, _  = self._tier_info(team.get("ssti", 0))

                total_wins = sum(team.get(k, 0) for _, k in WIN_TIER_KEYS)

                wins_parts = [
                    f"`{label}` {team.get(key, 0)}승"
                    for label, key in WIN_TIER_KEYS
                    if team.get(key, 0)
                ]
                wins_row = " · ".join(wins_parts) if wins_parts else "기록 없음"

                lines = [
                    f"{te} **{t_name}**",
                    f"현재 티어: **{tn}** | 최고 티어: **{hn}**",
                    f"티켓 — S: `{team.get('stt',0)}` "
                    f"SS: `{team.get('sstt',0)}` "
                    f"SSS: `{team.get('ssstt',0)}`",
                    f"총 승리: **{total_wins}승**",
                    wins_row,
                ]

                cdt, udt = team.get("cdt", 0), team.get("udt", 0)
                if cdt and udt:
                    lines.append(
                        f"생성: {datetime.fromtimestamp(cdt/1000):%Y.%m.%d} | "
                        f"업데이트: {datetime.fromtimestamp(udt/1000):%Y.%m.%d %H:%M}"
                    )

                embed.add_field(name="🏅 팀 정보", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🏅 팀 정보", value="유니온 팀 없음 (탈퇴/미가입)", inline=False)

        # ── 대전 기록 ──
        if games:
            recent = sorted(games, key=lambda g: g.get("startDtm", ""), reverse=True)[:10]
            total  = len(recent)
            wins   = sum(1 for g in recent if g.get("victory", 0))

            avg_rank = sum(g.get("gameRank", 0) for g in recent) / total
            avg_kill = sum(g.get("playerKill", 0) for g in recent) / total
            avg_dmg  = sum(g.get("damageToPlayer", 0) for g in recent) / total

            embed.add_field(
                name=f"📊 최근 {total}경기 요약",
                value=(
                    f"**{wins}승 {total-wins}패** (승률 **{wins/total*100:.0f}%**)\n"
                    f"평균 순위 **{avg_rank:.1f}위** | "
                    f"평균 킬 **{avg_kill:.1f}** | "
                    f"평균 딜 **{avg_dmg:,.0f}**"
                ),
                inline=False,
            )

            lines = []
            for g in recent:
                rank  = g.get("gameRank", 0)
                medal = RANK_MEDAL.get(rank, f"{rank}위")
                win   = " ✅" if g.get("victory", 0) else ""
                char  = Character_Names.get(g.get("characterNum", 0), "?")
                wpn   = Weapon_Types.get(g.get("bestWeapon", 0), "?")
                k, a, d = g.get("playerKill",0), g.get("playerAssistant",0), g.get("playerDeaths",0)
                dmg   = g.get("damageToPlayer", 0)
                lines.append(f"{medal}{win} {char}({wpn}) K/A/D `{k}/{a}/{d}` 딜 `{dmg:,}`")

            embed.add_field(name="🗒️ 경기 목록", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🗒️ 대전 기록", value="이 시즌 유니온 경기 없음", inline=False)

        embed.set_footer(text="이리와 봇 · 유니온 팀 정보")
        return embed

    # ---- 커맨드 ----

    @commands.command(name="유니온", aliases=["ㅇㄴㅇ", "union"])
    async def union_team_info(self, ctx: commands.Context, *, nickname: str = None):
        """유니온 팀 정보 + 대전 기록 조회"""
        author_id = str(ctx.author.id)

        if not nickname:
            if ctx.invoked_with == "ㅇㄴㅇ":
                return
            nickname = self.get_active_nickname(author_id)
            if not nickname:
                return await ctx.reply(embed=discord.Embed(
                    title="❌ 오류",
                    description="`ㅇ등록 [닉네임]`으로 먼저 등록하거나 닉네임을 입력해주세요.",
                    color=0xFF6B6B,
                ))

        loading = await ctx.reply(f"🔍 **{nickname}** 님의 유니온 정보를 불러오는 중...")

        try:
            user_id = await self.fetch_user_id(nickname)
            if not user_id:
                return await loading.edit(content=f"❌ **{nickname}** 닉네임을 찾을 수 없습니다.")

            all_games   = await self.fetch_user_games(user_id)
            union_games = [g for g in all_games if g.get("matchingMode") == UNION_MATCHING_MODE]

            if not union_games:
                return await loading.edit(content=f"❌ **{nickname}** 님의 유니온 대전 기록이 없습니다.")

            seasons = await self.build_season_list(user_id, union_games)
            if not seasons:
                return await loading.edit(content=f"❌ **{nickname}** 님의 유니온 정보를 구성할 수 없습니다.")

            embed = self.build_embed(seasons[0], nickname)
            view  = UnionSeasonView(self, author_id, nickname, seasons)

            await loading.delete()
            msg = await ctx.send(embed=embed, view=view)
            view.message = msg

        except Exception as e:
            import traceback; traceback.print_exc()
            await loading.edit(content=f"❌ 오류 발생: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(UnionTeamCog(bot))