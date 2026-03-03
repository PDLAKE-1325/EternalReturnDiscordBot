# cogs/matchDetail.py
import discord
from discord.ext import commands
import aiohttp
from typing import Optional
from datetime import datetime

from config import ER_KEY
from data import Character_Names, Weapon_Types

ER_BASE = "https://open-api.bser.io/v1"


class MatchDetailCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = ER_KEY
        self.base_url = ER_BASE
        self.character_names = Character_Names
        self.weapon_names = Weapon_Types

    async def fetch_game_detail(self, game_id: int) -> Optional[dict]:
        """특정 게임의 상세 정보 조회"""
        headers = {"x-api-key": self.api_key}
        
        url = f"{self.base_url}/games/{game_id}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("userGames") if data.get("userGames") else None

    def get_character_name(self, char_num: int) -> str:
        """캐릭터 번호를 이름으로 변환"""
        return self.character_names.get(char_num, f"캐릭터{char_num}")

    def get_weapon_name(self, weapon_num: int) -> str:
        """무기 번호를 이름으로 변환"""
        return self.weapon_names.get(weapon_num, f"무기{weapon_num}")

    def format_duration(self, seconds: int) -> str:
        """게임 시간을 분:초 형식으로 변환"""
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}분 {secs}초"

    def get_game_type_name(self, matching_mode: int, matching_team_mode: int) -> str:
        """게임 타입을 상세하게 반환"""
        MatchingTeamMode = {1: "론울프", 3: "스쿼드", 4: "코발트 프로토콜"}
        mtm = MatchingTeamMode.get(matching_team_mode, "스쿼드")
        
        MatchingMode = {2: "일반", 3: "랭크", 4: "일반", 6:"", 9: "론울프"}
        mm = MatchingMode.get(matching_mode, "기타")
        
        return f"{mtm} {mm}매치"

    def create_team_embed(self, all_players: list) -> discord.Embed:
        """팀별 플레이어를 임베드로 생성"""
        teams = {}
        
        # 팀별로 플레이어 분류
        for player in all_players:
            team_num = player.get("teamNumber", 0)
            if team_num not in teams:
                teams[team_num] = []
            teams[team_num].append(player)
        
        embed = discord.Embed(
            # title=f"팀 {team_num}",
            title=f"매치 상세 정보",
            color=0x5865F2,
            timestamp=datetime.now()
        )

        for team_num in sorted(teams.keys()):
            players = teams[team_num]
            
            # 팀 전체 통계
            team_kills = sum(p.get("playerKill", 0) for p in players)
            team_rank = players[0].get("gameRank", 0) if players else 0

            players_info = []

            # 각 플레이어 정보
            for player in players:
                char_name = self.get_character_name(player.get("characterNum", 0))
                nickname = player.get("nickname", "Unknown")
                # rank = player.get("gameRank", 0)
                kill = player.get("playerKill", 0)
                assist = player.get("playerAssistant", 0)
                death = player.get("totalDeaths", 0)
                # level = player.get("characterLevel", 0)
                damage = player.get("damageToPlayer", 0)
                best_weapon = player.get("bestWeapon", 0)
                weapon_name = self.get_weapon_name(best_weapon)

                player_info = (
                    f"> **{nickname}** | {char_name}({weapon_name})\n"
                    f"> K/D/A: {kill}/{death}/{assist} | 딜량: {damage:,}"
                )

                players_info.append(player_info)
            
            

            embed.add_field(
                name=f"팀 {team_num:02d}",
                value=f"최종 순위: **{team_rank}등** | TK: **{team_kills}**\n" + "\n".join(players_info),
                inline=False
            )
            
        return embed

    @commands.command(name="매치", aliases=["ㅁㅊ"])
    async def match_detail(self, ctx: commands.Context, game_id: int):
        """특정 게임의 상세 정보 조회"""
        
        loading_msg = await ctx.reply(f"🔍 게임 정보를 불러오는 중... (`{game_id}`)")
        
        try:
            # 게임 상세 정보 조회
            game_data = await self.fetch_game_detail(game_id)
            
            if not game_data:
                return await loading_msg.edit(content="❌ 해당 게임 정보를 찾을 수 없습니다.")
            
            if not game_data:
                return await loading_msg.edit(content="❌ 게임에 참가한 플레이어 정보가 없습니다.")
            
            # 게임 기본 정보
            first_player = game_data[0]
            game_type = self.get_game_type_name(
                first_player.get("matchingMode", 0),
                first_player.get("matchingTeamMode", 0)
            )
            duration = self.format_duration(first_player.get("playTime", 0))
            start_time = first_player.get("startDtm", "")
            
            # 메인 임베드
            main_embed = discord.Embed(
                title=f"⚔️ 매치 상세 정보",
                description=f"**게임 ID:** `{game_id}`\n**게임 모드:** {game_type}",
                color=0xFF6B6B,
                timestamp=datetime.now()
            )
            
            main_embed.add_field(
                name="게임 시간",
                value=f"{duration}",
                inline=True
            )
            
            main_embed.add_field(
                name="시작 시간",
                value=f"`{start_time}`",
                inline=True
            )
            
            main_embed.add_field(
                name="참가자",
                value=f"**{len(game_data)}명**",
                inline=True
            )
            
            main_embed.set_footer(text=f"이리와 봇 · 매치 상세 정보 | 게임 ID: {game_id}")
            
            # 팀별 임베드 생성
            team_embeds = self.create_team_embed(game_data)
            
            # 모든 임베드를 하나로 합치기 (max 10)
            all_embeds = [main_embed] + team_embeds
            
            await loading_msg.edit(content=None, embeds=all_embeds)
            
        except Exception as e:
            await loading_msg.edit(content=f"❌ 오류 발생: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchDetailCog(bot))
