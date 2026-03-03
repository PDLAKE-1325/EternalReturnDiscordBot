# cogs/matchDetail.py
import discord
from discord.ext import commands
import aiohttp
from typing import Optional
from datetime import datetime

from config import ER_KEY
from data import Character_Names, Weapon_Types

ER_BASE = "https://open-api.bser.io/v1"

# API 명세 4.2 MatchingMode
MATCHING_MODE = {
    2: "일반",
    3: "랭크",
    4: "일반",
    6: "", # 코발트 4인큐
    8: "유니온",
    9: "일반_9",
}

# API 명세 4.3 MatchingTeamMode
MATCHING_TEAM_MODE = {
    1: "론울프",
    3: "스쿼드",
    4: "코발트 프로토콜",
}

RANK_MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}

# 팀 모드별 한 임베드에 묶을 팀 수
# 론울프(1인팀)는 4팀씩, 나머지는 2팀씩
TEAMS_PER_EMBED = {1: 4, 3: 2, 4: 2}


class MatchDetailCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = ER_KEY
        self.base_url = ER_BASE

    # ------------------------------------------------------------------ #
    #  API
    # ------------------------------------------------------------------ #

    async def fetch_game_detail(self, game_id: int) -> Optional[list]:
        url = f"{self.base_url}/games/{game_id}"
        headers = {"x-api-key": self.api_key}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                user_games = data.get("userGames")
                return user_games if user_games else None

    # ------------------------------------------------------------------ #
    #  헬퍼
    # ------------------------------------------------------------------ #

    def get_character_name(self, char_num: int) -> str:
        return Character_Names.get(char_num, f"캐릭터{char_num}")

    def get_weapon_name(self, mastery_code: int) -> str:
        # data.py의 Weapon_Types: 숙련도 코드 기반, bestWeapon 필드와 동일
        return Weapon_Types.get(mastery_code, f"무기{mastery_code}")

    def format_duration(self, seconds: int) -> str:
        return f"{seconds // 60}분 {seconds % 60}초"

    def format_start_dtm(self, dtm: str) -> str:
        if not dtm:
            return "알 수 없음"
        try:
            dt = datetime.fromisoformat(dtm.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return dtm

    def get_game_type_name(self, matching_mode: int, matching_team_mode: int) -> str:
        team_label = MATCHING_TEAM_MODE.get(matching_team_mode, f"모드{matching_team_mode}")
        mode_label = MATCHING_MODE.get(matching_mode, "기타")
        return f"{team_label} {mode_label}매치"

    # ------------------------------------------------------------------ #
    #  임베드 생성
    # ------------------------------------------------------------------ #

    def _player_line(self, player: dict) -> str:
        """플레이어 1명의 정보를 2줄 문자열로 반환"""
        nickname  = player.get("nickname", "Unknown")
        char_name = self.get_character_name(player.get("characterNum", 0))
        weapon    = self.get_weapon_name(player.get("bestWeapon", 0))
        w_lv      = player.get("bestWeaponLevel", 0)
        level     = player.get("characterLevel", 0)
        kill      = player.get("playerKill", 0)
        assist    = player.get("playerAssistant", 0)
        death     = player.get("playerDeaths", 0)
        damage    = player.get("damageToPlayer", 0)
        monster_k = player.get("monsterKill", 0)

        return (
            f"**{nickname}** `Lv.{level}` · {char_name} ({weapon} {w_lv}레벨)\n"
            f"K/A/D `{kill}/{assist}/{death}` · 딜 `{damage:,}` · 야생 `{monster_k}`"
        )

    def build_team_embeds(self, all_players: list, team_mode: int) -> list[discord.Embed]:
        """
        팀을 순위 오름차순으로 정렬한 뒤,
        TEAMS_PER_EMBED 개씩 하나의 임베드에 묶어 반환
        """
        # ① 팀 번호 기준 그룹핑
        teams: dict[int, list] = {}
        for p in all_players:
            teams.setdefault(p.get("teamNumber", 0), []).append(p)

        # ② 순위(gameRank) 오름차순 정렬
        sorted_teams = sorted(
            teams.values(),
            key=lambda players: players[0].get("gameRank", 999)
        )

        chunk_size = TEAMS_PER_EMBED.get(team_mode, 2)
        embeds: list[discord.Embed] = []

        for i in range(0, len(sorted_teams), chunk_size):
            chunk = sorted_teams[i:i + chunk_size]
            rank_start = chunk[0][0].get("gameRank", "?")
            rank_end   = chunk[-1][0].get("gameRank", "?")

            embed = discord.Embed(
                title=f"📊 {rank_start}위 ~ {rank_end}위",
                color=0xFFD700 if rank_start == 1 else 0x5865F2,
            )

            for players in chunk:
                rank    = players[0].get("gameRank", 0)
                victory = any(p.get("victory", 0) for p in players)
                tk      = players[0].get("teamKill", 0)
                team_kills  = sum(p.get("playerKill", 0) for p in players)
                team_deaths = sum(p.get("playerDeaths", 0) for p in players)

                medal = RANK_MEDAL.get(rank, f"{rank}위")
                win_badge = " 🏆" if victory else ""

                header = (
                    f"{medal}{win_badge}  팀 킬 `{tk}` "
                    f"(K `{team_kills}` / D `{team_deaths}`)"
                )

                player_lines = "\n\n".join(self._player_line(p) for p in players)

                embed.add_field(
                    name=header,
                    value=player_lines,
                    inline=False,
                )

            embeds.append(embed)

        return embeds

    # ------------------------------------------------------------------ #
    #  커맨드
    # ------------------------------------------------------------------ #

    @commands.command(name="매치", aliases=["ㅁㅊ"])
    async def match_detail(self, ctx: commands.Context, game_id: int):
        """특정 게임의 상세 정보 조회"""
        loading_msg = await ctx.reply(f"🔍 게임 정보를 불러오는 중... (`{game_id}`)")

        try:
            game_data = await self.fetch_game_detail(game_id)

            if not game_data:
                return await loading_msg.edit(
                    content="❌ 해당 게임 정보를 찾을 수 없거나 참가자 정보가 없습니다."
                )

            first       = game_data[0]
            team_mode   = first.get("matchingTeamMode", 3)
            game_type   = self.get_game_type_name(first.get("matchingMode", 0), team_mode)
            duration    = self.format_duration(first.get("playTime", 0))
            start_time  = self.format_start_dtm(first.get("startDtm", ""))
            season_id   = first.get("seasonId", "?")
            bot_added   = first.get("botAdded", 0)
            bot_text    = f" *(AI 봇 {bot_added}명 포함)*" if bot_added else ""

            main_embed = discord.Embed(
                title="⚔️ 매치 상세 정보",
                description=(
                    f"**게임 ID:** `{game_id}`\n"
                    f"**모드:** {game_type}  ·  **시즌:** {season_id}\n"
                    f"**시작:** {start_time}  ·  **게임 시간:** {duration}\n"
                    f"**참가자:** {len(game_data)}명{bot_text}"
                ),
                color=0xFF6B6B,
                timestamp=datetime.now(),
            )
            main_embed.set_footer(text="이리와 봇 · 매치 상세 정보")

            team_embeds = self.build_team_embeds(game_data, team_mode)

            # Discord 제한: 한 번에 최대 10개 임베드
            all_embeds = [main_embed] + team_embeds[:9]
            await loading_msg.edit(content=None, embeds=all_embeds)

        except Exception as e:
            await loading_msg.edit(content=f"❌ 오류 발생: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchDetailCog(bot))