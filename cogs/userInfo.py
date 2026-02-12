# cogs/user_info.py
import discord
from discord.ext import commands

import aiohttp
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

from config import ER_KEY
from data import Character_Names
from db import SessionLocal
from models import User


# ---------- View ----------
class SeasonModeView(discord.ui.View):
    """시즌 및 게임 모드 선택 드롭다운"""

    def __init__(
        self,
        cog: "UserInfoCog",
        user_discord_id: str,
        nickname: str,
        season_options: List[discord.SelectOption],
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_discord_id = user_discord_id
        self.nickname = nickname

        self.selected_season: Optional[int] = None  # None = 전체
        self.selected_mode: str = "all"             # all / rank / normal

        self.season_select.options = season_options
        # 기본값 표시
        if self.season_select.options:
            self.season_select.options[0].default = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_discord_id:
            await interaction.response.send_message(
                "❌ 명령어를 사용한 사람만 선택할 수 있습니다.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.select(placeholder="📅 시즌 선택", min_values=1, max_values=1)
    async def season_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        v = select.values[0]
        self.selected_season = None if v == "all" else int(v)
        await interaction.response.defer()
        await self.update_stats(interaction)

    @discord.ui.select(
        placeholder="🎮 게임 모드 선택",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="전체 모드", value="all", emoji="🌐", default=True),
            discord.SelectOption(label="랭크 게임만", value="rank", emoji="🏆"),
            discord.SelectOption(label="일반 게임만", value="normal", emoji="⚔️"),
        ],
    )
    async def mode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_mode = select.values[0]
        await interaction.response.defer()
        await self.update_stats(interaction)

    async def update_stats(self, interaction: discord.Interaction):
        embed = await self.cog.create_stats_embed(
            self.nickname,
            season=self.selected_season,
            mode=self.selected_mode,
        )
        if embed:
            await interaction.message.edit(embed=embed, view=self)


# ---------- Cog ----------
class UserInfoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = ER_KEY
        self.base_url = "https://open-api.bser.io/v1"
        self.character_names = Character_Names
        self._season_cache: Optional[List[Dict[str, Any]]] = None

    # ---- DB ----
    def get_active_nickname(self, user_id: str) -> Optional[str]:
        session = SessionLocal()
        try:
            user = session.get(User, user_id)
            if user and hasattr(user, "active_er_nickname"):
                return user.active_er_nickname
            return None
        finally:
            session.close()

    # ---- Utils ----
    def get_character_image_url(self, char_num: int) -> str:
        return f"https://static.api.bser.io/attachments/Characters/{char_num}.png"

    def get_character_name(self, char_num: int) -> str:
        return self.character_names.get(char_num, f"캐릭터{char_num}")

    def get_tier_info(self, mmr: int) -> Tuple[str, int]:
        if mmr >= 5000: return "Immortal", 0xFF6B9D
        if mmr >= 4500: return "Titan", 0xB19CD9
        if mmr >= 4000: return "Diamond", 0x6CD5FF
        if mmr >= 3500: return "Platinum", 0x00D9FF
        if mmr >= 3000: return "Gold", 0xFFD700
        if mmr >= 2500: return "Silver", 0xC0C0C0
        if mmr >= 2000: return "Bronze", 0xCD7F32
        return "Iron", 0x87764F

    def _headers(self) -> Dict[str, str]:
        return {"x-api-key": self.api_key}

    # ---- API ----
    async def fetch_user_id(self, nickname: str) -> Optional[int]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/user/nickname",
                params={"query": nickname},
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                user = data.get("user")
                return user.get("userId") if user else None

    async def fetch_seasons(self) -> List[Dict[str, Any]]:
        """시즌 목록 (캐시)"""
        if self._season_cache is not None:
            return self._season_cache

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/data/season",
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    self._season_cache = []
                    return []
                data = await resp.json()

        seasons = data.get("data") or data.get("seasons") or []
        # 최신 시즌이 위로 오게 정렬(혹시 이미 정렬이면 그대로)
        seasons = sorted(seasons, key=lambda x: x.get("seasonID", 0), reverse=True)
        self._season_cache = seasons
        return seasons

    async def fetch_user_games_page(self, user_id: int, next_param: Optional[int] = None) -> Optional[dict]:
        params = {}
        if next_param is not None:
            params["next"] = next_param

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/user/games/uid/{user_id}",
                params=params,
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()

    async def fetch_all_user_games(self, user_id: int, hard_cap: int = 4000) -> List[dict]:
        """
        '전체 전적'용: next 페이징 끝까지 가져오기
        hard_cap으로 무한/과다 호출 방지 (필요하면 늘려)
        """
        all_games: List[dict] = []
        next_param: Optional[int] = None

        while True:
            page = await self.fetch_user_games_page(user_id, next_param=next_param)
            if not page:
                break

            games = page.get("userGames") or []
            if not games:
                break

            all_games.extend(games)
            if len(all_games) >= hard_cap:
                all_games = all_games[:hard_cap]
                break

            # API가 next를 어떤 키로 주는지 케이스가 있어서 둘 다 처리
            next_param = page.get("next") or page.get("nextParam") or page.get("next_param")
            if next_param is None:
                break

        return all_games

    # ---- Embed Builder ----
    async def create_stats_embed(self, nickname: str, season: Optional[int] = None, mode: str = "all") -> Optional[discord.Embed]:
        user_id = await self.fetch_user_id(nickname)
        if not user_id:
            return None

        # ✅ 전체 전적: 페이징으로 끝까지
        all_games = await self.fetch_all_user_games(user_id)
        if not all_games:
            return None

        # 필터링
        filtered_games = []
        for g in all_games:
            if season is not None and g.get("seasonId") != season:
                continue

            # 너 코드에선 2=랭크, 0=일반으로 처리했는데,
            # 혹시 값이 다를 수 있으니 "랭크 여부" 필드는 실제 응답 보고 확정하는 게 정확함.
            team_mode = g.get("matchingTeamMode")

            if mode == "rank" and team_mode != 2:
                continue
            if mode == "normal" and team_mode == 2:
                continue

            filtered_games.append(g)

        if not filtered_games:
            return None

        total_games = len(filtered_games)
        wins = sum(1 for g in filtered_games if g.get("gameRank") == 1)
        top2 = sum(1 for g in filtered_games if (g.get("gameRank") or 99) <= 2)
        top3 = sum(1 for g in filtered_games if (g.get("gameRank") or 99) <= 3)

        total_kills = sum(g.get("playerKill", 0) for g in filtered_games)
        total_deaths = sum(g.get("playerDeaths", 0) for g in filtered_games)
        total_assists = sum(g.get("playerAssistant", 0) for g in filtered_games)

        avg_rank = sum(g.get("gameRank", 0) for g in filtered_games) / total_games
        avg_kills = total_kills / total_games
        avg_deaths = total_deaths / total_games if total_games else 0
        avg_assists = total_assists / total_games

        # Most used characters
        char_stats: Dict[int, Dict[str, int]] = {}
        for g in filtered_games:
            c = g.get("characterNum")
            if c is None:
                continue
            if c not in char_stats:
                char_stats[c] = {"games": 0, "maxKills": 0, "top3": 0}
            char_stats[c]["games"] += 1
            char_stats[c]["maxKills"] = max(char_stats[c]["maxKills"], g.get("playerKill", 0))
            if (g.get("gameRank") or 99) <= 3:
                char_stats[c]["top3"] += 1

        top_chars = sorted(char_stats.items(), key=lambda x: x[1]["games"], reverse=True)[:3]

        # 최신 랭크게임 mmrAfter 잡기 (가능하면 gameId/startDtm 기준 최신)
        rank_games = [g for g in filtered_games if g.get("matchingTeamMode") == 2 and g.get("mmrAfter") is not None]
        # gameId가 있으면 그걸로 최신 정렬
        rank_games.sort(key=lambda x: x.get("gameId", 0), reverse=True)
        current_mmr = rank_games[0].get("mmrAfter") if rank_games else None

        tier_name, tier_color = self.get_tier_info(current_mmr) if current_mmr is not None else ("Unranked", 0x808080)

        filter_text = []
        if season is not None:
            filter_text.append(f"Season {season}")
        if mode == "rank":
            filter_text.append("🏆 랭크")
        elif mode == "normal":
            filter_text.append("⚔️ 일반")
        filter_text = (" | " + " | ".join(filter_text)) if filter_text else ""

        embed = discord.Embed(
            title=f"⚔️ {nickname} 스탯",
            description=f"**{tier_name}**" + (f" (MMR: {current_mmr})" if current_mmr is not None else "") + filter_text,
            color=tier_color,
            timestamp=datetime.now(),
        )

        if top_chars:
            embed.set_thumbnail(url=self.get_character_image_url(top_chars[0][0]))

        embed.add_field(
            name="📊 Play",
            value=(
                f"**게임 수:** {total_games}회\n"
                f"**승리:** {wins}회 ({wins/total_games*100:.1f}%)\n"
                f"**Top 3:** {top3}회 ({top3/total_games*100:.1f}%)"
            ),
            inline=True,
        )
        embed.add_field(
            name="📈 Average",
            value=(
                f"**평균 순위:** {avg_rank:.2f}위\n"
                f"**킬:** {avg_kills:.2f}\n"
                f"**데스:** {avg_deaths:.2f}\n"
                f"**어시스트:** {avg_assists:.2f}"
            ),
            inline=True,
        )
        embed.add_field(
            name="🏆 Top Placements (Ratio)",
            value=(
                f"**Top 1:** {wins}회 ({wins/total_games*100:.0f}%)\n"
                f"**Top 2:** {top2}회 ({top2/total_games*100:.0f}%)\n"
                f"**Top 3:** {top3}회 ({top3/total_games*100:.0f}%)"
            ),
            inline=False,
        )

        lines = []
        for i, (char_num, st) in enumerate(top_chars, 1):
            name = self.get_character_name(char_num)
            games = st["games"]
            max_kills = st["maxKills"]
            top3_ratio = (st["top3"] / games * 100) if games else 0
            lines.append(f"**{i}. {name}**\n└ {games}게임 | Max Kills: {max_kills} | Top3: {top3_ratio:.0f}%")
        embed.add_field(name="⭐ Most Used Characters", value="\n".join(lines) if lines else "데이터 없음", inline=False)

        embed.set_footer(text=f"이리와 봇 - 총 {total_games}게임 분석")
        return embed

    # ---- Command ----
    @commands.command(name="스텟", aliases=["tmxps", "stat", "stats"])
    async def show_stats(self, ctx: commands.Context, *, nickname: str = None):
        user_discord_id = str(ctx.author.id)

        if not nickname:
            nickname = self.get_active_nickname(user_discord_id)

        if not nickname:
            embed = discord.Embed(
                title="⚔️ 스탯 조회",
                description="닉네임을 입력하거나 먼저 등록해주세요!",
                color=0x0fb9b1,
            )
            embed.add_field(
                name="사용법",
                value="!스텟 [닉네임] 또는\n!닉네임등록 [닉네임] 후 !스텟",
                inline=False,
            )
            await ctx.reply(embed=embed)
            return

        loading_msg = await ctx.reply(f"📊 **{nickname}** 님의 스탯을 조회 중...")

        try:
            embed = await self.create_stats_embed(nickname)
            if not embed:
                err = discord.Embed(
                    title="❌ 조회 실패",
                    description=f"**{nickname}** 님의 정보를 찾을 수 없습니다.",
                    color=0xFF0000,
                )
                await loading_msg.edit(content=None, embed=err)
                return

            # ✅ 시즌 옵션을 API에서 자동 생성
            seasons = await self.fetch_seasons()
            # 너무 길어지면 최근 N개만 (원하면 늘려)
            seasons = seasons[:25]

            season_options = [discord.SelectOption(label="전체 시즌", value="all", emoji="📊")]
            for s in seasons:
                sid = s.get("seasonID")
                name = s.get("seasonName", f"Season {sid}")
                is_current = s.get("isCurrent", 0) == 1
                # label은 보기 좋게
                label = f"{name}" + (" (Current)" if is_current else "")
                season_options.append(discord.SelectOption(label=label, value=str(sid), emoji="📅"))

            view = SeasonModeView(self, user_discord_id, nickname, season_options)
            await loading_msg.edit(content=None, embed=embed, view=view)

        except Exception as e:
            err = discord.Embed(
                title="⚠️ 오류 발생",
                description=f"```{str(e)}```",
                color=0xFF9900,
            )
            await loading_msg.edit(content=None, embed=err)


async def setup(bot: commands.Bot):
    await bot.add_cog(UserInfoCog(bot))
