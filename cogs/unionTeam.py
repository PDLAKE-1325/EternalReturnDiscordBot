# cogs/unionTeam.py
import discord
from discord.ext import commands
import aiohttp
import asyncio
from typing import Optional, List, Dict
from datetime import datetime

from config import ER_KEY
from db import SessionLocal
from models import User
from data import Character_Names, Weapon_Types, CURRENT_SEASON, CURRENT_SEASON_NUM

ER_BASE    = "https://open-api.bser.io/v1"
ER_BASE_V2 = "https://open-api.bser.io/v2"

# 유니온 게임 matchingMode
UNION_MATCHING_MODE = 8

UNION_TIER_MAP = {
    1: ("S",   0xFF6B6B, "<:UnionS:1475215908665299035>"),
    2: ("A",   0xFFA500, "<:UnionA:1475215920313139261>"),
    3: ("B",   0x5865F2, "<:UnionB:1475215913778413609>"),
    4: ("C",   0x43B581, "<:UnionC:1475215912083652760>"),
    5: ("D",   0x7289DA, "<:UnionD:1475215904789762169>"),
}

RANK_MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


# ------------------------------------------------------------------ #
#  시즌 선택 드롭다운 View
# ------------------------------------------------------------------ #

class UnionSeasonSelectView(discord.ui.View):
    def __init__(self, cog: "UnionTeamCog", ctx, author_id: str,
                 nickname: str, user_api_id: str,
                 available_seasons: List[Dict]):
        super().__init__(timeout=300)
        self.cog           = cog
        self.ctx           = ctx
        self.author_id     = author_id
        self.nickname      = nickname
        self.user_api_id   = user_api_id
        self.available_seasons = available_seasons   # [{seasonID, _teamData, _games, ...}, ...]
        self.selected_season   = available_seasons[0] if available_seasons else None
        self.message           = None
        self._rebuild_select()

    # ---- UI 빌더 ----

    def _rebuild_select(self):
        self.clear_items()
        if not self.available_seasons:
            return

        cur_id = self.selected_season["seasonID"] if self.selected_season else None
        options = []
        for s in self.available_seasons[:25]:
            sid   = s["seasonID"]
            label = f"시즌 {sid}"
            is_current = s.get("isCurrent", 0) == 1
            options.append(discord.SelectOption(
                label=label,
                value=str(sid),
                emoji="🟢" if is_current else "⚪",
                default=(sid == cur_id),
            ))

        sel = discord.ui.Select(placeholder="📅 시즌 선택", options=options)
        sel.callback = self._on_select
        self.add_item(sel)

    # ---- 콜백 ----

    async def _on_select(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.author_id:
            return await interaction.response.send_message(
                "❌ 명령어를 사용한 사람만 선택할 수 있습니다.", ephemeral=True
            )

        await interaction.response.defer()

        sid = int(interaction.data["values"][0])
        self.selected_season = next(
            (s for s in self.available_seasons if s["seasonID"] == sid), None
        )
        if not self.selected_season:
            return

        embed = self.cog.build_embed(self.selected_season, self.nickname)
        self._rebuild_select()

        if self.message:
            await self.message.edit(embed=embed, view=self)


# ------------------------------------------------------------------ #
#  Cog
# ------------------------------------------------------------------ #

class UnionTeamCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot      = bot
        self.api_key  = ER_KEY
        self._seasons_cache: Optional[List[Dict]] = None

    # ---------------------------------------------------------------- #
    #  DB
    # ---------------------------------------------------------------- #

    def get_active_nickname(self, user_id: str) -> Optional[str]:
        session = SessionLocal()
        try:
            user = session.get(User, user_id)
            return user.active_er_nickname if user and hasattr(user, "active_er_nickname") else None
        finally:
            session.close()

    # ---------------------------------------------------------------- #
    #  API
    # ---------------------------------------------------------------- #

    @property
    def _headers(self):
        return {"x-api-key": self.api_key}

    async def _get(self, url: str, **params) -> Optional[dict]:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=self._headers, params=params or None) as r:
                if r.status != 200:
                    return None
                return await r.json()

    async def fetch_uid(self, nickname: str) -> Optional[str]:
        data = await self._get(f"{ER_BASE}/user/nickname", query=nickname)
        if not data:
            return None
        user = data.get("user", {})
        return user.get("userId")

    async def fetch_seasons(self) -> List[Dict]:
        if self._seasons_cache:
            return self._seasons_cache
        data = await self._get(f"{ER_BASE_V2}/data/Season")
        if data and data.get("data"):
            self._seasons_cache = data["data"]
        return self._seasons_cache or []

    async def fetch_union_teams(self, uid: str, season_id: int) -> Optional[List[Dict]]:
        data = await self._get(f"{ER_BASE}/unionTeam/uid/{uid}/{season_id}")
        return data.get("teams") if data else None

    async def fetch_user_games(self, uid: str) -> List[Dict]:
        """최근 90일 전체 경기 목록"""
        data = await self._get(f"{ER_BASE}/user/games/uid/{uid}")
        return data.get("userGames", []) if data else []

    # ---------------------------------------------------------------- #
    #  시즌 조회 (프로그레시브)
    # ---------------------------------------------------------------- #

    async def _season_has_union(self, uid: str, season_id: int, all_games: List[Dict]) -> Optional[Dict]:
        """
        해당 시즌에 유니온 게임이 있으면 팀 데이터 + 게임 목록 반환.
        all_games: 이미 가져온 전체 게임 목록 (matchingMode=8, seasonId 필터)
        """
        games = [
            g for g in all_games
            if g.get("matchingMode") == UNION_MATCHING_MODE
            and g.get("seasonId") == season_id
        ]
        if not games:
            return None

        teams = await self.fetch_union_teams(uid, season_id)
        return {"games": games, "teams": teams}

    async def load_seasons_progressive(
        self, uid: str, view: UnionSeasonSelectView
    ):
        """백그라운드에서 나머지 시즌 계속 조회하며 드롭다운 업데이트"""
        all_seasons  = await self.fetch_seasons()
        all_games    = view._all_games  # 이미 fetch한 게임 목록 재사용
        existing_ids = {s["seasonID"] for s in view.available_seasons}

        sorted_seasons = sorted(all_seasons, key=lambda x: x["seasonID"], reverse=True)

        for season in sorted_seasons:
            sid = season["seasonID"]
            if sid in existing_ids:
                continue
            await asyncio.sleep(0.3)

            result = await self._season_has_union(uid, sid, all_games)
            if not result:
                continue

            entry = {**season, "_games": result["games"], "_teams": result["teams"]}
            view.available_seasons.append(entry)
            view.available_seasons.sort(key=lambda x: x["seasonID"], reverse=True)
            existing_ids.add(sid)

            view._rebuild_select()
            if view.message:
                try:
                    await view.message.edit(view=view)
                except Exception:
                    pass

    # ---------------------------------------------------------------- #
    #  헬퍼
    # ---------------------------------------------------------------- #

    def _get_tier_info(self, tier_num: int) -> tuple:
        return UNION_TIER_MAP.get(tier_num, ("?", 0x808080, "❓"))

    def _calc_win_rate(self, team: Dict) -> float:
        win_keys = [
            "ssstw","sstw","stw",
            "aaatw","aatw","atw",
            "bbbtw","bbtw","btw",
            "ccctw","cctw","ctw",
            "dddtw","ddtw","dtw",
            "etw","ffftw","fftw","ftw",
        ]
        wins  = sum(team.get(k, 0) for k in win_keys)
        # 게임 수 필드가 없으므로 게임 기록으로 추산
        return wins

    def _fmt_dtm(self, ms: int) -> str:
        try:
            return datetime.fromtimestamp(ms / 1000).strftime("%Y.%m.%d")
        except Exception:
            return "?"

    # ---------------------------------------------------------------- #
    #  임베드 빌더
    # ---------------------------------------------------------------- #

    def build_embed(self, season_entry: Dict, nickname: str) -> discord.Embed:
        season_id = season_entry["seasonID"]
        teams     = season_entry.get("_teams") or []
        games     = season_entry.get("_games") or []
        is_cur    = season_entry.get("isCurrent", 0) == 1

        # ── 팀 정보 (첫 번째 팀 기준 색상) ──
        primary_team  = teams[0] if teams else {}
        cur_tier      = primary_team.get("ti", 0)
        _, tier_color, _ = self._get_tier_info(cur_tier)

        embed = discord.Embed(
            title=f"🤝 {nickname}의 유니온 정보",
            description=f"**시즌 {season_id}** {'`현재 시즌`' if is_cur else ''}",
            color=tier_color,
            timestamp=datetime.now(),
        )

        # ── 팀 섹션 ──
        if teams:
            for team in teams:
                t_name    = team.get("tnm", "Unknown")
                t_cur     = team.get("ti", 0)
                t_high    = team.get("ssti", 0)
                tn, _, te = self._get_tier_info(t_cur)
                hn, _, _  = self._get_tier_info(t_high)

                s_tkt  = team.get("stt", 0)
                ss_tkt = team.get("sstt", 0)
                sss_tkt= team.get("ssstt", 0)

                total_wins = self._calc_win_rate(team)

                # 티어별 승리 (1승 이상만)
                all_tiers = [
                    ("SSS","ssstw"),("SS","sstw"),("S","stw"),
                    ("AAA","aaatw"),("AA","aatw"),("A","atw"),
                    ("BBB","bbbtw"),("BB","bbtw"),("B","btw"),
                    ("CCC","ccctw"),("CC","cctw"),("C","ctw"),
                    ("DDD","dddtw"),("DD","ddtw"),("D","dtw"),
                    ("E","etw"),
                    ("FFF","ffftw"),("FF","fftw"),("F","ftw"),
                ]
                wins_parts = []
                for label, key in all_tiers:
                    w = team.get(key, 0)
                    if w:
                        wins_parts.append(f"`{label}` {w}승")

                wins_rows = " · ".join(wins_parts) if wins_parts else "기록 없음"

                cdt = team.get("cdt", 0)
                udt = team.get("udt", 0)
                date_str = ""
                if cdt and udt:
                    date_str = (
                        f"생성: {self._fmt_dtm(cdt)} | "
                        f"업데이트: {datetime.fromtimestamp(udt/1000).strftime('%Y.%m.%d %H:%M')}"
                    )

                val = (
                    f"{te} **{t_name}**\n"
                    f"현재 티어: **{tn}** | 최고 티어: **{hn}**\n"
                    f"티켓 — S: `{s_tkt}` SS: `{ss_tkt}` SSS: `{sss_tkt}`\n"
                    f"총 승리: **{total_wins}승**\n"
                    f"{wins_rows}"
                )
                if date_str:
                    val += f"\n{date_str}"

                embed.add_field(name="🏅 팀 정보", value=val, inline=False)
        else:
            embed.add_field(name="🏅 팀 정보", value="유니온 팀 정보 없음", inline=False)

        # ── 대전 기록 섹션 (최근 10경기) ──
        if games:
            recent = sorted(games, key=lambda g: g.get("startDtm", ""), reverse=True)[:10]

            total   = len(recent)
            wins    = sum(1 for g in recent if g.get("victory", 0))
            avg_rank= sum(g.get("gameRank", 0) for g in recent) / total if total else 0
            avg_kill= sum(g.get("playerKill", 0) for g in recent) / total if total else 0
            avg_dmg = sum(g.get("damageToPlayer", 0) for g in recent) / total if total else 0

            embed.add_field(
                name=f"📊 최근 {total}경기 요약",
                value=(
                    f"승: **{wins}** / 패: **{total - wins}** "
                    f"(승률 **{wins/total*100:.0f}%**)\n"
                    f"평균 순위 **{avg_rank:.1f}위** | "
                    f"평균 킬 **{avg_kill:.1f}** | "
                    f"평균 딜 **{avg_dmg:,.0f}**"
                ),
                inline=False,
            )

            lines = []
            for g in recent:
                rank    = g.get("gameRank", 0)
                kill    = g.get("playerKill", 0)
                assist  = g.get("playerAssistant", 0)
                death   = g.get("playerDeaths", 0)
                char    = Character_Names.get(g.get("characterNum", 0), "?")
                weapon  = Weapon_Types.get(g.get("bestWeapon", 0), "?")
                dmg     = g.get("damageToPlayer", 0)
                medal   = RANK_MEDAL.get(rank, f"{rank}위")
                win_tag = " ✅" if g.get("victory", 0) else ""

                lines.append(
                    f"{medal}{win_tag} {char}({weapon}) "
                    f"K/A/D `{kill}/{assist}/{death}` 딜 `{dmg:,}`"
                )

            embed.add_field(
                name="🗒️ 경기 목록",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(name="🗒️ 대전 기록", value="이 시즌 유니온 경기 없음", inline=False)

        embed.set_footer(text="이리와 봇 · 유니온 팀 정보")
        return embed

    # ---------------------------------------------------------------- #
    #  커맨드
    # ---------------------------------------------------------------- #

    @commands.command(name="유니온", aliases=["ㅇㄴㅇㄴ", "union"])
    async def union_team_info(self, ctx: commands.Context, *, nickname: str = None):
        """유니온 팀 정보 + 대전 기록 조회"""
        author_id = str(ctx.author.id)

        if not nickname:
            nickname = self.get_active_nickname(author_id)
            if not nickname:
                return await ctx.reply(embed=discord.Embed(
                    title="❌ 오류",
                    description="`ㅇ등록 [닉네임]`으로 먼저 등록하거나 닉네임을 입력해주세요.",
                    color=0xFF6B6B,
                ))

        loading = await ctx.reply(f"🔍 **{nickname}** 님의 유니온 정보를 불러오는 중...")

        try:
            uid = await self.fetch_uid(nickname)
            if not uid:
                return await loading.edit(content=f"❌ **{nickname}** 닉네임을 찾을 수 없습니다.")

            # ── 전체 경기 목록 한 번만 fetch ──
            all_games = await self.fetch_user_games(uid)
            union_games = [g for g in all_games if g.get("matchingMode") == UNION_MATCHING_MODE]

            if not union_games:
                return await loading.edit(content=f"❌ **{nickname}** 님의 유니온 대전 기록이 없습니다.")

            # ── 현재 시즌부터 먼저 조회 ──
            all_seasons = await self.fetch_seasons()
            sorted_seasons = sorted(all_seasons, key=lambda x: x["seasonID"], reverse=True)

            available: List[Dict] = []
            for season in sorted_seasons[:5]:   # 최근 5시즌 선조회
                sid    = season["seasonID"]
                result = await self._season_has_union(uid, sid, union_games)
                if result:
                    available.append({
                        **season,
                        "_games": result["games"],
                        "_teams": result["teams"],
                    })

            if not available:
                # 유니온 게임은 있는데 팀 정보가 없는 경우, 게임 기록만으로 첫 시즌 구성
                sid_set = sorted({g.get("seasonId") for g in union_games}, reverse=True)
                for sid in sid_set[:3]:
                    s_info = next((s for s in all_seasons if s["seasonID"] == sid), {"seasonID": sid})
                    available.append({
                        **s_info,
                        "_games": [g for g in union_games if g.get("seasonId") == sid],
                        "_teams": [],
                    })

            if not available:
                return await loading.edit(content=f"❌ **{nickname}** 님의 유니온 정보를 구성할 수 없습니다.")

            # ── 첫 임베드 ──
            first_embed = self.build_embed(available[0], nickname)
            view        = UnionSeasonSelectView(self, ctx, author_id, nickname, uid, available)
            view._all_games = union_games   # 백그라운드 조회용 공유

            await loading.delete()
            msg = await ctx.send(embed=first_embed, view=view)
            view.message = msg

            # ── 나머지 시즌 백그라운드 조회 ──
            asyncio.create_task(self.load_seasons_progressive(uid, view))

        except Exception as e:
            import traceback; traceback.print_exc()
            await loading.edit(content=f"❌ 오류 발생: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(UnionTeamCog(bot))