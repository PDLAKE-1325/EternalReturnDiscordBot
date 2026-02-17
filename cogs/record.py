# cogs/record_with_auto_nickname.py
import discord
from discord import File
from discord.ext import commands
from config import ER_KEY
from data import Character_Names, Weapon_Types, Character_Names_EN
import aiohttp
from datetime import datetime
from typing import Optional, List
import re
import os

from db import SessionLocal
from models import User

class RecordCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = ER_KEY 
        self.base_url = "https://open-api.bser.io/v1"
        
        # 캐릭터 이름 매핑 (characterNum -> 한글 이름)
        self.character_names = Character_Names
        
        # 무기 타입 매핑
        self.weapon_names = Weapon_Types

        #실험체 미니 이미지 폴더
        self.char_image_folder = "images/character/Mini_Files"
    
    def get_character_image_path(self, char_num: int) -> List[str]:
        char_name = Character_Names_EN.get(char_num)
        if not char_name:
            return []

        folder = os.path.abspath(self.char_image_folder)
        if not os.path.isdir(folder):
            return []

        files = os.listdir(folder)

        def norm(s: str) -> str:
            return "".join(ch.lower() for ch in s if ch.isalnum())

        def extract_index(fname: str) -> int:
            """
            Eleven_Mini_00.png -> 0
            Eleven_Mini_12.png -> 12
            숫자 없으면 뒤로 보냄
            """
            m = re.search(r'_(\d+)(?:\.[^.]+)?$', fname)
            return int(m.group(1)) if m else 10**9

        target = norm(char_name)
        results: List[str] = []

        for fname in files:
            if not fname.lower().endswith(".png"):
                continue
            if target in norm(fname):
                results.append(os.path.join(folder, fname))

        # 🔹 마지막 숫자 기준 정렬
        results.sort(key=lambda path: extract_index(os.path.basename(path)))

        return results
    
    
    def get_game_type_name(self, matching_mode: int, matching_team_mode: int) -> str:
        """게임 타입을 상세하게 반환"""

        
        MatchingTeamMode = {1: "론울프", 3: "스쿼드", 4: "코발트 프로토콜", 2: "2",5: "5",6: "6",7: "7",8: "8",9: "9"}
        mtm = MatchingTeamMode.get(matching_team_mode, "스쿼드")

        MatchingMode = {1:"1", 2: "일반", 3: "랭크", 4: "일반", 5:"5", 6:"", 7:"7", 8:"8", 9: "9"} # 6: 코발트 4인큐일떄 뜸
        mm = MatchingMode.get(matching_mode, "기타")
        
        return f"{mtm} {mm}매치" 
    
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
        """닉네임으로 유저 ID 조회"""
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
                
                if not data.get("user"):
                    return None
                    
                return data["user"]["userId"]
    
    async def fetch_user_games(self, user_id: str, next_param: int = None) -> Optional[dict]:
        """유저 최근 게임 기록 조회"""
        headers = {"x-api-key": self.api_key}
        
        url = f"{self.base_url}/user/games/uid/{user_id}"
        params = {}
        if next_param:
            params["next"] = next_param
            
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    
    def format_duration(self, seconds: int) -> str:
        """게임 시간을 분:초 형식으로 변환"""
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}분 {secs}초"
    
    def get_character_name(self, char_num: int) -> str:
        """캐릭터 번호를 이름으로 변환"""
        return self.character_names.get(char_num, f"캐릭터{char_num}")
    
    def get_weapon_name(self, weapon_num: int) -> str:
        """무기 번호를 이름으로 변환"""
        return self.weapon_names.get(weapon_num, f"무기{weapon_num}")

    @commands.command(name="전적", aliases=["ㅈㅈ"])
    async def check_record(self, ctx: commands.Context, *, nickname: str = None):
        """이터널 리턴 최근 전적 검색"""
        user_id = str(ctx.author.id)
        
        # 닉네임이 제공되지 않았으면 DB에서 가져오기
        if not nickname:
            nickname = self.get_active_nickname(user_id)
            
            if not nickname:
                embed = discord.Embed(
                    title="⚔️ 이터널 리턴 전적 검색",
                    description="닉네임을 입력하거나 먼저 등록해주세요!",
                    color=0x0fb9b1,
                    timestamp=datetime.now()
                )
                embed.add_field(
                    name="💡 사용법",
                    value=(
                        "**직접 검색:**\n"
                        "`ㅇ전적 [닉네임]` 또는 `ㅇㅈㅈ [닉네임]`\n\n"
                        "**닉네임 등록 후 자동 검색:**\n"
                        "`ㅇ등록 [닉네임]` 으로 등록하면\n"
                        "`ㅇ전적` 만 입력해도 자동으로 검색됩니다!"
                    ),
                    inline=False
                )
                embed.set_footer(text="이리와 봇 - 이터널 리턴 전적 검색")
                await ctx.reply(embed=embed)
                return
        
        # 로딩 메시지
        loading_msg = await ctx.reply(f"🔍 **{nickname}** 님의 전적을 검색 중...")
        
        try:
            # 유저 ID 조회
            user_api_id = await self.fetch_user_id(nickname)
            
            if not user_api_id:
                embed = discord.Embed(
                    title="❌ 검색 실패",
                    description=f"**{nickname}** 님의 정보를 찾을 수 없습니다.\n닉네임을 확인해주세요.",
                    color=0xff0000,
                    timestamp=datetime.now()
                )
                await loading_msg.edit(content=None, embed=embed)
                return
            
            # 최근 게임 기록 조회
            games_data = await self.fetch_user_games(user_api_id)
            
            if not games_data or not games_data.get("userGames"):
                embed = discord.Embed(
                    title="❌ 데이터 없음",
                    description=f"**{nickname}** 님의 게임 기록을 찾을 수 없습니다.",
                    color=0xff0000,
                    timestamp=datetime.now()
                )
                await loading_msg.edit(content=None, embed=embed)
                return
            
            games = games_data["userGames"][:5]  # 최근 n게임
            
            # 통계 계산
            total_games = len(games)
            wins = sum(1 for g in games if g["gameRank"] == 1)
            top3 = sum(1 for g in games if g["gameRank"] <= 3)
            total_kills = sum(g["playerKill"] for g in games)
            total_deaths = sum(g.get("playerDeaths", 0) for g in games)
            avg_rank = sum(g["gameRank"] for g in games) / total_games
            
            # 평균 KDA 계산
            avg_kills = total_kills / total_games
            avg_deaths = total_deaths / total_games if total_deaths > 0 else 1
            kda = total_kills / avg_deaths if avg_deaths > 0 else total_kills
            
            # 가장 많이 플레이한 캐릭터 (썸네일용)
            char_counts = {}
            skin_counts = {}

            for game in games:
                char_num = game["characterNum"]
                char_counts[char_num] = char_counts.get(char_num, 0) + 1
            
            most_played_char = max(char_counts.items(), key=lambda x: x[1])[0] if char_counts else games[0]["characterNum"]
            
            for game in games:
                if game["characterNum"] != most_played_char:
                    continue
                skin_num = game["skinCode"] % 100
                skin_counts[skin_num] = skin_counts.get(skin_num, 0) + 1

            most_played_skin = max(skin_counts.items(), key=lambda x: x[1])[0] if skin_counts else 0   

            # 임베드 생성
            embed = discord.Embed(
                title=f"⚔️ {nickname} 님의 최근 전적",
                description=f"최근 {total_games}게임 통계",
                color=0x0fb9b1,
                timestamp=datetime.now()
            )
            


            # 전체 통계
            embed.add_field(
                name="𒄬 종합 통계",
                value=(
                    f"**승리:** {wins}회 ({wins/total_games*100:.1f}%)\n"
                    f"**Top 3:** {top3}회 ({top3/total_games*100:.1f}%)\n"
                    f"**평균 순위:** {avg_rank:.1f}위\n"
                    f"**평균 킬:** {avg_kills:.1f}\n"
                    f"**KDA:** {kda:.2f}"
                ),
                inline=False
            )

            # 가장 많이 플레이한 캐릭터
            if char_counts:
                most_played = max(char_counts.items(), key=lambda x: x[1])
                char_name = self.get_character_name(most_played[0])
                play_count = most_played[1]
                
                embed.add_field(
                    name="❖ 주 캐릭터",
                    value=f"{char_name} ({play_count}회)",
                    inline=True
                )
            
            # 평균 게임 시간
            avg_duration = sum(g["duration"] for g in games) / total_games
            embed.add_field(
                name="⏱ 평균 게임 시간",
                value=self.format_duration(int(avg_duration)),
                inline=True
            )
            
            # 최근 게임 기록
            recent_games = []
            games_played = 0
            for i, game in enumerate(games[:5], 1):
                rank = game["gameRank"]
                char_name = self.get_character_name(game["characterNum"])
                weapon_name = self.get_weapon_name(game["bestWeapon"])
                
                tk = game["teamKill"]
                kills = game["playerKill"]
                assists = game["playerAssistant"]
                
                # 승리 여부 표시
                if game["matchingTeamMode"] == 4:
                    result = "승리" if rank == 1 else f"패배"
                else:
                    result = "승리" if rank == 1 else f"{rank}위"
                
                # 게임 타입 상세 표시
                game_type = self.get_game_type_name(game["matchingMode"], game["matchingTeamMode"])
                
                game_info = (
                    f"**{result}**[{game_type}]\n"
                    f"> **{char_name}**-{weapon_name}\n"
                    f"> TK/K/D/A : {tk}/{kills}/{game.get('playerDeaths', 0)}/{assists}"
                )
                recent_games.append(game_info)
                games_played = i;
            
            embed.add_field(
                name=f"𒉻 최근 {games_played}게임 기록",
                value="\n".join(recent_games),
                inline=False
            )
            
            embed.set_footer(text="이리와 봇 - 전적")
            
            # 가장 많이 플레이한 캐릭터 이미지 설정
            # img_path = self.get_character_image_path(most_played_char)[most_played_skin]
            img_paths = self.get_character_image_path(most_played_char)
            img_path = img_paths[most_played_skin] if most_played_skin < len(img_paths) else (img_paths[0] if img_paths else None)
            print(f"most_played_char:{most_played_char}, most_played_skin : {most_played_skin}")

            if img_path:
                file = File(img_path, filename=os.path.basename(img_path))
                embed.set_thumbnail(url=f"attachment://{file.filename}")
                await loading_msg.delete()
                await ctx.reply(embed=embed, file=file)
                return
            await loading_msg.edit(content=None, embed=embed)
            
        except Exception as e:
            embed = discord.Embed(
                title="⚠️ 오류 발생",
                description=f"전적 검색 중 오류가 발생했습니다.\n```{str(e)}```",
                color=0xff9900,
                timestamp=datetime.now()
            )
            await loading_msg.edit(content=None, embed=embed)

    @commands.command(name="최근게임", aliases=["ㅊㄱㄱ"])
    async def recent_game(self, ctx: commands.Context, *, nickname: str = None):
        """가장 최근 게임 상세 정보"""
        user_id = str(ctx.author.id)
        
        # 닉네임이 제공되지 않았으면 DB에서 가져오기
        if not nickname:
            nickname = self.get_active_nickname(user_id)
            
            if not nickname:
                embed = discord.Embed(
                    title="🎮 최근 게임 조회",
                    description="닉네임을 입력하거나 먼저 등록해주세요!",
                    color=0x0fb9b1
                )
                embed.add_field(
                    name="사용법",
                    value="`ㅇ최근게임 [닉네임]` 또는\n`ㅇ등록 [닉네임]` 후 `ㅇ최근게임`",
                    inline=False
                )
                await ctx.reply(embed=embed)
                return
        
        loading_msg = await ctx.reply(f"🔍 **{nickname}** 님의 최근 게임을 조회 중...")
        
        try:
            user_api_id = await self.fetch_user_id(nickname)
            if not user_api_id:
                embed = discord.Embed(
                    title="❌ 검색 실패",
                    description=f"**{nickname}** 님을 찾을 수 없습니다.",
                    color=0xff0000
                )
                await loading_msg.edit(content=None, embed=embed)
                return
            
            games_data = await self.fetch_user_games(user_api_id)
            if not games_data or not games_data.get("userGames"):
                embed = discord.Embed(
                    title="❌ 데이터 없음",
                    description="게임 기록이 없습니다.",
                    color=0xff0000
                )
                await loading_msg.edit(content=None, embed=embed)
                return
            
            game = games_data["userGames"][0]  # 가장 최근 게임
            
            # 게임 결과
            rank = game["gameRank"]
            result = "승리" if rank == 1 else f"{rank}위"
            
            # 게임 타입 상세 표시
            game_type = self.get_game_type_name(game["matchingMode"], game["matchingTeamMode"])
            
            # 캐릭터 & 무기
            char_name = self.get_character_name(game["characterNum"])
            weapon_name = self.get_weapon_name(game["bestWeapon"])
            
            embed = discord.Embed(
                title=f"🎮 {nickname}님의 최근 게임",
                description=f"**{result}** | {game_type}",
                color=0x00ff00 if rank == 1 else 0x0fb9b1,
                timestamp=datetime.now()
            )
            
            
            # 기본 정보
            embed.add_field(
                name="⚔️ 플레이 정보",
                value=(
                    f"**캐릭터:** {char_name} Lv.{game['characterLevel']}\n"
                    f"**무기:** {weapon_name} Lv.{game['bestWeaponLevel']}\n"                    
                ),
                inline=True
            )
            
            # KDA
            kills = game["playerKill"]
            deaths = game.get("playerDeaths", 0)
            assists = game["playerAssistant"]
            
            embed.add_field(
                name="𒄬 전투 기록",
                value=(
                    f"**TK/K/D/A:** {game['teamKill']}/{kills}/{deaths}/{assists}\n"
                    f"**KD:** {kills / deaths if deaths > 0 else kills:.2f}\n"
                    f"**킬 관여율:** {((kills + assists) / max(1, game['teamKill']))*100:.1f}%"
                ),
                inline=True
            )
            
            # 딜량
            embed.add_field(
                name="𒉻 피해량",
                value=(
                    f"**준 피해:** {game['damageToPlayer']:,}\n"
                    f"**받은 피해:** {game['damageFromPlayer']:,}\n"
                    f"**몬스터 피해:** {game['damageToMonster']:,}"
                ),
                inline=True
            )
            
            # 게임 진행
            embed.add_field(
                name="⏱ 게임 정보",
                value=(
                    f"**플레이 시간:** {self.format_duration(game['playTime'])}\n"
                    f"**생존 시간:** {self.format_duration(game['survivableTime'])}\n"
                    f"**몬스터 처치:** {game['monsterKill']}"
                ),
                inline=True
            )
            
            # MMR 정보 - 안전하게 처리
            mmr_before = game.get('mmrBefore')
            mmr_after = game.get('mmrAfter')
            mmr_gain = game.get('mmrGain')
            mmr_avg = game.get('mmrAvg')
            
            # MMR 정보가 있는 경우에만 표시
            if mmr_before is not None and mmr_after is not None and mmr_gain is not None:
                mmr_symbol = "📈" if mmr_gain > 0 else "📉" if mmr_gain < 0 else "➡️"
                
                mmr_value = (
                    f"**MMR:** {mmr_before} → {mmr_after}\n"
                    f"**변동:** {mmr_symbol} {mmr_gain:+d}\n"
                )
                
                if mmr_avg is not None:
                    mmr_value += f"**평균 MMR:** {mmr_avg}"
                
                embed.add_field(
                    name="🃁 랭크 게임",
                    value=mmr_value,
                    inline=True
                )
            else:
                # 랭크 정보가 없는 경우 (일반 게임 등)
                embed.add_field(
                    name="🃁 일반 게임",
                    value="\n(랭크 정보 없음)",
                    inline=True
                )
            
            # 아이템 제작
            embed.add_field(
                name="🛠️ 제작",
                value=(
                    f"🟢 **일반:** {game['craftUncommon']}\n"
                    f"🔵 **희귀:** {game['craftRare']}\n"
                    f"🟣 **서사:** {game['craftEpic']}\n"
                    f"🟡 **전설:** {game['craftLegend']}\n"
                    f"🔴 **혈템:** {game.get('craftMythic', 0)}"
                ),
                inline=True
            )
            
            # 바로가기 링크
            embed.add_field(
                name="Dak.gg 리플레이",
                value=f"https://dak.gg/er/replay/{game['gameId']}",
                inline=False
            )

            embed.set_footer(text=f"이리와 봇 - 최근 게임 | 게임 ID: {game['gameId']}")
            
            # 플레이한 캐릭터 이미지 설정
            # img_path = self.get_character_image_path(game["characterNum"])[game["skinCode"]%100]
            img_paths = self.get_character_image_path(game["characterNum"])
            skin_idx = game["skinCode"] % 100
            img_path = img_paths[skin_idx] if skin_idx < len(img_paths) else (img_paths[0] if img_paths else None)
            print(f"skincode: {game["skinCode"]}")
            if img_path:
                file = File(img_path, filename=os.path.basename(img_path))
                embed.set_thumbnail(url=f"attachment://{file.filename}")
                await loading_msg.delete()
                await ctx.reply(embed=embed, file=file)
                return
            await loading_msg.edit(content=None, embed=embed)
            
        except Exception as e:
            embed = discord.Embed(
                title="⚠️ 오류 발생",
                description=f"```{str(e)}```",
                color=0xff9900
            )
            await loading_msg.edit(content=None, embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(RecordCog(bot))