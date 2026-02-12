# cogs/user_rank.py
import discord
from discord.ext import commands
from config import ER_KEY
import aiohttp
from typing import Optional, Dict, List
from datetime import datetime
import asyncio
import os, re

from db import SessionLocal
from models import User

# 시즌 ID -> 한글 이름 매핑
SEASON_NAMES = {
    1: "EA 시즌 1",
    2: "EA 프리시즌 2",
    3: "EA 시즌 2",
    4: "EA 프리시즌 3",
    5: "EA 시즌 3",
    6: "EA 프리시즌 4",
    7: "EA 시즌 4",
    8: "EA 프리시즌 5",
    9: "EA 시즌 5",
    10: "EA 프리시즌 6",
    11: "EA 시즌 6",
    12: "EA 프리시즌 7",
    13: "EA 시즌 7",
    14: "EA 프리시즌 8",
    15: "EA 시즌 8",
    16: "EA 프리시즌 9",
    17: "EA 시즌 9",
    18: "프리시즌 1",
    19: "시즌 1",
    20: "프리시즌 2",
    21: "시즌 2",
    22: "프리시즌 3",
    23: "시즌 3",
    24: "프리시즌 4",
    25: "시즌 4",
    26: "프리시즌 5",
    27: "시즌 5",
    28: "프리시즌 6",
    29: "시즌 6",
    30: "프리시즌 7",
    31: "시즌 7",
    32: "프리시즌 8",
    33: "시즌 8",
    34: "프리시즌 9",
    35: "시즌 9",
    36: "프리시즌 10",
    37: "시즌 10",
}

def get_season_korean_name(season_id: int) -> str:
    """시즌 ID로 한글 이름 가져오기 (37 이후 자동 생성)"""
    if season_id in SEASON_NAMES:
        return SEASON_NAMES[season_id]
    
    # 37 이후 자동 계산
    # 패턴: 프리시즌, 시즌 반복 (38=프리시즌 11, 39=시즌 11)
    if season_id > 37:
        offset = season_id - 37
        season_num = 10 + (offset + 1) // 2
        if offset % 2 == 1:  # 홀수: 프리시즌
            return f"프리시즌 {season_num}"
        else:  # 짝수: 시즌
            return f"시즌 {season_num}"
    
    return f"시즌 {season_id}"


class SeasonSelectView(discord.ui.View):
    """시즌 선택 드롭다운"""
    def __init__(self, cog, ctx, user_id: str, nickname: str, user_api_id: str, available_seasons: List[Dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.user_id = user_id
        self.nickname = nickname
        self.user_api_id = user_api_id
        self.available_seasons = available_seasons
        self.selected_season = available_seasons[0] if available_seasons else None
        self.message = None  # ✅ 메시지 참조 저장
        self.is_loading = False  # ✅ 로딩 상태

        # 드롭다운 생성
        self.create_select_menu()
    
    def create_select_menu(self):
        """사용 가능한 시즌으로 드롭다운 생성"""
        # ✅ 기존 아이템 제거
        self.clear_items()
        
        options = []
        current_season_id = self.selected_season["seasonID"] if self.selected_season else None
        
        for season in self.available_seasons[:25]:
            season_id = season["seasonID"]
            season_name = get_season_korean_name(season_id)
            is_current = season.get("isCurrent", 0) == 1
            
            options.append(
                discord.SelectOption(
                    label=season_name,
                    value=str(season_id),
                    description=f"{season['seasonStart'][:10]} ~ {season['seasonEnd'][:10]}",
                    emoji="🟢" if is_current else "🔴",
                    default=(season_id == current_season_id)
                )
            )
        
        if options:
            select = discord.ui.Select(
                placeholder="🏆 시즌 선택",
                options=options
            )
            select.callback = self.season_callback
            self.add_item(select)
    
    async def season_callback(self, interaction: discord.Interaction):
        """시즌 선택 콜백"""
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message(
                "❌ 명령어를 사용한 사람만 선택할 수 있습니다.", 
                ephemeral=True
            )
        
        try:
            await interaction.response.defer()
            
            selected_id = int(interaction.data['values'][0])
            self.selected_season = next(
                (s for s in self.available_seasons if s["seasonID"] == selected_id), 
                None
            )
            
            if not self.selected_season:
                await interaction.followup.send(
                    "⚠️ 시즌 정보를 찾을 수 없습니다.", 
                    ephemeral=True
                )
                return
            
            embed, img_path = await self.cog.create_rank_embed(  # ✅ 튜플 언패킹
                self.user_api_id,
                self.nickname,
                self.selected_season
            )
            
            if not embed:
                embed = discord.Embed(
                    title="⚠️ 오류",
                    description="랭크 정보를 불러올 수 없습니다.",
                    color=0xff9900
                )
            
            self.create_select_menu()
            
            # ✅ 이미지 파일이 있으면 함께 전송
            file_obj = None
            if img_path and os.path.exists(img_path):
                file_obj = discord.File(img_path, filename=os.path.basename(img_path))
            
            if self.message:
                if file_obj:
                    # 이미지가 있으면 메시지를 새로 보내야 함
                    await self.message.delete()
                    self.message = await interaction.channel.send(file=file_obj, embed=embed, view=self)
                else:
                    await self.message.edit(embed=embed, view=self)
            else:
                if file_obj:
                    await interaction.followup.send(file=file_obj, embed=embed, view=self)
                else:
                    await interaction.edit_original_response(embed=embed, view=self)
                    
        except Exception as e:
            # print(f"❌ 콜백 오류: {e}")
            import traceback
            traceback.print_exc()
            
            try:
                await interaction.followup.send(
                    "⚠️ 오류가 발생했습니다.",
                    ephemeral=True
                )
            except:
                pass


class UserRankCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = ER_KEY 
        self.base_url = "https://open-api.bser.io/v1"
        self.base_url_v2 = "https://open-api.bser.io/v2"
        self.seasons_cache = None  # 시즌 정보 캐시

        #티어 이미지 폴더
        self.tier_image_folder = "images/tier"
    
    def get_tier_image_path(self, tier_num: int) -> List[str]:
        folder = os.path.abspath(self.tier_image_folder)
        if not os.path.isdir(folder):
            return None

        files = os.listdir(folder)


        for fname in files:
            if fname.lower().startswith(f"{tier_num:02d}."):
                result = os.path.join(folder, fname)
                return result

        return None


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
    def season_1to3_tier(self, mmr: int, rank: int) -> str:
        if mmr >= 6200:
            if rank and rank <= 200:
                return "이터니티"
            if rank and rank <= 700:
                return "데미갓"
            return "미스릴"
        if mmr >= 6000:
            return "미스릴"
        if mmr >= 5000:
            return "다이아몬드"
        if mmr >= 4000:
            return "플레티넘"
        if mmr >= 3000:
            return "골드"
        if mmr >= 2000:
            return "실버"
        if mmr >= 1000:
            return "브론즈"
        return "아이언"
    def season_3to4_tier(self, mmr: int, rank: int) -> str:
        if mmr >= 6400:
            if rank and rank <= 200:
                return "이터니티"
            if rank and rank <= 700:
                return "데미갓"
            return "미스릴"
        if mmr >= 6200:
            return "미스릴"
        if mmr >= 4800:
            return "다이아몬드"
        if mmr >= 3600:
            return "플레티넘"
        if mmr >= 2600:
            return "골드"
        if mmr >= 1600:
            return "실버"
        if mmr >= 800:
            return "브론즈"
        return "아이언"
    def season_4to5_tier(self, mmr: int, rank: int) -> str:
        if mmr >= 7000:
            if rank and rank <= 200:
                return "이터니티"
            if rank and rank <= 700:
                return "데미갓"
            return "미스릴"
        if mmr >= 6800:
            return "미스릴"
        if mmr >= 5200:
            return "다이아몬드"
        if mmr >= 3800:
            return "플레티넘"
        if mmr >= 2600:
            return "골드"
        if mmr >= 1600:
            return "실버"
        if mmr >= 800:
            return "브론즈"
        return "아이언"
    def season_5to6_tier(self, mmr: int, rank: int) -> str:
        if mmr >= 7500:
            if rank and rank <= 200:
                return "이터니티"
            if rank and rank <= 700:
                return "데미갓"
            return "미스릴"
        if mmr >= 6800:
            return "미스릴"
        if mmr >= 6400:
            return "메테오라이트"
        if mmr >= 5000:
            return "다이아몬드"
        if mmr >= 3600:
            return "플레티넘"
        if mmr >= 2400:
            return "골드"
        if mmr >= 1400:
            return "실버"
        if mmr >= 600:
            return "브론즈"
        return "아이언"
    def season_6to7_tier(self, mmr: int, rank: int) -> str:
        if mmr >= 7700:
            if rank and rank <= 300:
                return "이터니티"
            if rank and rank <= 1000:
                return "데미갓"
            return "미스릴"
        if mmr >= 7000:
            return "미스릴"
        if mmr >= 6400:
            return "메테오라이트"
        if mmr >= 5000:
            return "다이아몬드"
        if mmr >= 3600:
            return "플레티넘"
        if mmr >= 2400:
            return "골드"
        if mmr >= 1400:
            return "실버"
        if mmr >= 600:
            return "브론즈"
        return "아이언"
    def season_7to9_tier(self, mmr: int, rank: int) -> str:
        if mmr >= 7800:
            if rank and rank <= 300:
                return "이터니티"
            if rank and rank <= 1000:
                return "데미갓"
            return "미스릴"
        if mmr >= 7100:
            return "미스릴"
        if mmr >= 6400:
            return "메테오라이트"
        if mmr >= 5000:
            return "다이아몬드"
        if mmr >= 3600:
            return "플레티넘"
        if mmr >= 2400:
            return "골드"
        if mmr >= 1400:
            return "실버"
        if mmr >= 600:
            return "브론즈"
        return "아이언"
    def season_9to10_tier(self, mmr: int, rank: int) -> str:
        if mmr >= 7900:
            if rank and rank <= 300:
                return "이터니티"
            if rank and rank <= 1000:
                return "데미갓"
            return "미스릴"
        if mmr >= 7200:
            return "미스릴"
        if mmr >= 6400:
            return "메테오라이트"
        if mmr >= 5000:
            return "다이아몬드"
        if mmr >= 3600:
            return "플레티넘"
        if mmr >= 2400:
            return "골드"
        if mmr >= 1400:
            return "실버"
        if mmr >= 600:
            return "브론즈"
        return "아이언"
    def season_10tier(self, mmr: int, rank: int) -> str:
        if mmr >= 8100:
            if rank and rank <= 300:
                return "이터니티"
            if rank and rank <= 1000:
                return "데미갓"
            return "미스릴"
        if mmr >= 7400:
            return "미스릴"
        if mmr >= 6400:
            return "메테오라이트"
        if mmr >= 5000:
            return "다이아몬드"
        if mmr >= 3600:
            return "플레티넘"
        if mmr >= 2400:
            return "골드"
        if mmr >= 1400:
            return "실버"
        if mmr >= 600:
            return "브론즈"
        return "아이언"
    
    def get_tier_str(self, mmr: int, rank: int, season_num: int) -> str:
        if season_num <3:
            return self.season_1to3_tier(mmr, rank)
        elif season_num <4:
            return self.season_3to4_tier(mmr, rank)
        elif season_num <5:
            return self.season_4to5_tier(mmr, rank)
        elif season_num <6:
            return self.season_5to6_tier(mmr, rank)
        elif season_num <7:
            return self.season_6to7_tier(mmr, rank)
        elif season_num <9:
            return self.season_7to9_tier(mmr, rank)
        elif season_num <10:
            return self.season_9to10_tier(mmr, rank)
        else:
            return self.season_10tier(mmr, rank)

    def resolve_tier(self, rank_data: Dict, season_id: int) -> tuple:
        mmr = rank_data.get("mmr")
        rank = rank_data.get("rank")
        rank_percent = rank_data.get("rankPercent")
        if season_id:
            season_num = (season_id - 19)//2

        # 랭크 안 돌렸으면
        if not rank or rank <= 0:
            return "Unranked", 0x808080
        
        tier = self.get_tier_str(mmr, rank, season_num)
        # rank_percent_str = (
        #     f"상위 {rank_percent:.2f}%"
        #     if isinstance(rank_percent, (int, float))
        #     else None
        # )

        if tier == "이터니티":
            # 핫핑크 + 신성함 (최상위)
            return tier, 0xFF4D8D, 10
        elif tier == "데미갓":
            # 연보라 다이아 느낌
            return tier, 0xB38BFF, 9
        elif tier == "미스릴":
            # 밝은 실버 + 청색 기운
            return tier, 0xBFD7EA, 8
        elif tier == "메테오라이트":
            # 보라빛 금속 (중요 티어 느낌)
            return tier, 0x8E5EFF, 7
        elif tier == "다이아몬드":
            # 맑은 하늘색
            return tier, 0x5BCBFF, 6
        elif tier == "플레티넘":
            # 청록 계열 (차분)
            return tier, 0x2DE2E6, 5
        elif tier == "골드":
            # 진짜 금색 (노랑 과하지 않게)
            return tier, 0xF4C430, 4
        elif tier == "실버":
            # 연한 회은색
            return tier, 0xC7CCD6, 3
        elif tier == "브론즈":
            # 구리색
            return tier, 0xC47A4A, 2
        elif tier == "아이언":
            # 어두운 철색
            return tier, 0x6B6F76, 1
        else:
            return "Unranked", 0x808080, 0



#         0~399 아이언
# 400~799 브론즈
# 800~1199 실버
# 1200~1599 골드
# 1600~1999 플래티넘
# 2000~2399 다이아몬드
# 2400~ 데미갓
# 2600~ 이터니티(200위까지)
# MMR 3000점이라도 201위면 데미갓입니다

    
    async def fetch_seasons(self) -> Optional[List[Dict]]:
        """시즌 정보 조회 (캐싱)"""
        if self.seasons_cache:
            return self.seasons_cache
        
        headers = {"x-api-key": self.api_key}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url_v2}/data/Season",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                
                if data.get("code") == 200 and data.get("data"):
                    self.seasons_cache = data["data"]
                    return self.seasons_cache
                return None
    
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
    
    async def fetch_user_rank(self, user_id: str, season_id: int, team_mode: int = 3, retry: int = 1) -> Optional[Dict]:
        """유저 랭크 정보 조회 (Rate limit 재시도)"""
        headers = {"x-api-key": self.api_key}
        url = f"{self.base_url}/rank/uid/{user_id}/{season_id}/{team_mode}"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    status = resp.status
                    
                    if status == 429:  # Rate limit
                        # print(f"⚠️ Rate limit! 시즌 {season_id}")
                        
                        # ✅ 재시도 (최대 1회)
                        if retry > 0:
                            # print(f"   → 2초 후 재시도...")
                            await asyncio.sleep(2.0)
                            return await self.fetch_user_rank(user_id, season_id, team_mode, retry - 1)
                        return None
                    
                    if status != 200:
                        text = await resp.text()
                        # print(f"⚠️ 시즌 {season_id} HTTP {status}: {text[:100]}")
                        return None
                    
                    data = await resp.json()
                    
                    if data.get("code") != 200:
                        # print(f"⚠️ 시즌 {season_id} API code={data.get('code')}")
                        return None
                    
                    if data.get("userRank"):
                        return data["userRank"]
                    
                    return None
                
        except Exception as e:
            # print(f"❌ 시즌 {season_id} 예외: {e}")
            return None
    
    async def get_available_seasons(self, user_id: str, max_seasons: int = None) -> List[Dict]:
        """유저가 플레이한 시즌 목록 조회 (전체 조회)"""
        all_seasons = await self.fetch_seasons()
        if not all_seasons:
            return []

        available = []
        sorted_seasons = sorted(all_seasons, key=lambda x: x["seasonID"], reverse=True)
        
        # ✅ max_seasons가 None이면 전체 조회
        seasons_to_check = sorted_seasons if max_seasons is None else sorted_seasons[:max_seasons]

        # print(f"=== {user_id} 시즌 조회 시작 (총 {len(seasons_to_check)}개) ===")
        
        for i, season in enumerate(seasons_to_check):
            season_id = season["seasonID"]
            season_name = get_season_korean_name(season_id)

            # ✅ EA시즌 스킵, 프리시즌 스킵
            if( season_id <= 17 or season['seasonName'].startswith('Pre')):
                continue
            
            if i > 0:
                await asyncio.sleep(1.2)
            
            try:
                rank_data = await self.fetch_user_rank(user_id, season_id)
                
                if rank_data:
                    mmr = rank_data.get('mmr', 0)
                    rank = rank_data.get('rank', 0)
                    games = rank_data.get('totalGames', 0)
                    
                    # print(f"✅ {season_name} (ID:{season_id}): MMR={mmr}, Rank={rank}, Games={games}")
                    
                    if rank > 0:
                        season_copy = dict(season)
                        season_copy["_rankData"] = rank_data
                        available.append(season_copy)
                    else:
                        pass
                        # print(f"   → rank={rank}이라 제외됨")
                else:
                    pass
                    # print(f"❌ {season_name} (ID:{season_id}): API 응답 없음")
                    
            except Exception as e:
                pass
                # print(f"⚠️ {season_name} (ID:{season_id}): 예외 - {e}")
        
        # print(f"=== 총 {len(available)}개 시즌 발견 ===")
        return available


    async def get_available_seasons_progressive(self, user_id: str, view: 'SeasonSelectView', initial_count: int = 5):
        """시즌을 점진적으로 조회하면서 View 업데이트"""
        all_seasons = await self.fetch_seasons()
        if not all_seasons:
            return

        sorted_seasons = sorted(all_seasons, key=lambda x: x["seasonID"], reverse=True)
        
        # ✅ 이미 조회한 시즌 ID 목록
        existing_season_ids = {s["seasonID"] for s in view.available_seasons}
        
        # print(f"=== {user_id} 전체 시즌 백그라운드 조회 시작 (이미 {len(existing_season_ids)}개 있음) ===")
        
        # ✅ 모든 시즌 조회 (중복 제외)
        for i, season in enumerate(sorted_seasons):
            season_id = season["seasonID"]
            
            # ✅ 이미 조회한 시즌은 스킵, EA시즌 스킵, 프리시즌 스킵
            if season_id in existing_season_ids or season_id <= 17 or season['seasonName'].startswith('Pre'):
                continue
            
            season_name = get_season_korean_name(season_id)
            
            await asyncio.sleep(1.2)
            
            try:
                rank_data = await self.fetch_user_rank(user_id, season_id)
                
                if rank_data:
                    mmr = rank_data.get('mmr', 0)
                    rank = rank_data.get('rank', 0)
                    
                    # print(f"✅ [백그라운드] {season_name} (ID:{season_id}): MMR={mmr}, Rank={rank}")
                    
                    if rank > 0:
                        season_copy = dict(season)
                        season_copy["_rankData"] = rank_data
                        view.available_seasons.append(season_copy)
                        existing_season_ids.add(season_id)  # ✅ 추가한 시즌 기록
                        
                        # ✅ 드롭다운 업데이트
                        view.create_select_menu()
                        
                        # ✅ 메시지 업데이트
                        if view.message:
                            try:
                                await view.message.edit(view=view)
                                # print(f"   → 드롭다운 업데이트 완료 (총 {len(view.available_seasons)}개)")
                            except Exception as e:
                                pass
                                # print(f"   → 드롭다운 업데이트 실패: {e}")
                    else:
                        pass
                        # print(f"   → rank={rank}이라 제외됨")
                                
            except Exception as e:
                pass
                # print(f"⚠️ [백그라운드] {season_name} (ID:{season_id}): 예외 - {e}")
        # print(f"=== 백그라운드 조회 완료: 총 {len(view.available_seasons)}개 시즌 ===")

    
    async def create_rank_embed(self, user_id: str, nickname: str, season_info: Dict) -> tuple[Optional[discord.Embed], Optional[str]]:
        season_id = season_info["seasonID"]

        rank_data = season_info.get("_rankData")
        
        if not rank_data:
            # print(f"❌ create_rank_embed: 시즌 {season_id} 캐시 없음")
            return None, None

        mmr = rank_data.get("mmr", 0)
        rank = rank_data.get("rank", 0)
        nickname = rank_data.get("nickname", nickname)

        tier_name, tier_color, tier_order = self.resolve_tier(rank_data, season_id)

        season_korean = get_season_korean_name(season_id)

        img_path = self.get_tier_image_path(tier_order)

        season_start = season_info["seasonStart"][:10]
        season_end = season_info["seasonEnd"][:10]
        is_current = season_info.get("isCurrent", 0) == 1

        embed = discord.Embed(
            title=f"👑 {nickname} 랭크 게임 정보", #🏆
            description=f"**{season_korean}** {'(현재 시즌)' if is_current else ''}",
            color=tier_color,
            timestamp=datetime.now()
        )

        # ✅ 티어 이미지가 있으면 썸네일로 설정
        if img_path and os.path.exists(img_path):
            filename = os.path.basename(img_path)
            embed.set_thumbnail(url=f"attachment://{filename}")
        
        tier_text = f"**{tier_name}**"

        embed.add_field(
            name=f"❖ 티어",
            value=tier_text,
            inline=True
        )
        embed.add_field(name="🃁 MMR", value=f"**{mmr:,}** RP", inline=True)
        embed.add_field(name="⌥ 랭킹", value=f"**{rank:,}** 위", inline=True) #🏅
        embed.add_field(name="📆 시즌 기간", value=f"{season_start} ~ {season_end}", inline=False)
        embed.set_footer(text="이리와 봇 - 랭크전")

        return embed, img_path  # ✅ 이미지 경로도 함께 반환

    
    @commands.command(name="랭크", aliases=["ㄹㅋ", "fz", "랭킹", "랭겜"])
    async def show_rank(self, ctx: commands.Context, *, nickname: str = None):
        """이터널 리턴 랭킹 조회"""
        user_id = str(ctx.author.id)
        
        if not nickname:
            nickname = self.get_active_nickname(user_id)
            
            if not nickname:
                embed = discord.Embed(
                    title="🏆 랭킹 조회",
                    description="닉네임을 입력하거나 먼저 등록해주세요!",
                    color=0x0fb9b1
                )
                embed.add_field(
                    name="사용법",
                    value="`!랭크 [닉네임]` 또는\n`!닉네임등록 [닉네임]` 후 `!랭크`",
                    inline=False
                )
                await ctx.send(embed=embed)
                return
        
        loading_msg = await ctx.send(f"🔍 **{nickname}** 님의 랭킹을 조회 중...")
        
        try:
            user_api_id = await self.fetch_user_id(nickname)
            
            if not user_api_id:
                embed = discord.Embed(
                    title="❌ 검색 실패",
                    description=f"**{nickname}** 님의 정보를 찾을 수 없습니다.",
                    color=0xff0000
                )
                await loading_msg.edit(content=None, embed=embed)
                return
            
            available_seasons = await self.get_available_seasons(user_api_id, max_seasons=5)
            
            if not available_seasons:
                embed = discord.Embed(
                    title="❌ 랭크 데이터 없음",
                    description=f"**{nickname}** 님의 랭크 기록을 찾을 수 없습니다.",
                    color=0xff0000
                )
                await loading_msg.edit(content=None, embed=embed)
                return
            
            current_season = available_seasons[0]
            embed, img_path = await self.create_rank_embed(user_api_id, nickname, current_season)  # ✅ 튜플 언패킹
            
            if not embed:
                embed = discord.Embed(
                    title="⚠️ 오류",
                    description="랭크 정보를 불러올 수 없습니다.",
                    color=0xff9900
                )
                await loading_msg.edit(content=None, embed=embed)
                return
            
            # ✅ 이미지 파일이 있으면 함께 전송
            file_obj = None
            if img_path and os.path.exists(img_path):
                file_obj = discord.File(img_path, filename=os.path.basename(img_path))
            
            view = SeasonSelectView(self, ctx, user_id, nickname, user_api_id, available_seasons)
            
            if file_obj:
                await loading_msg.delete()
                msg = await ctx.send(file=file_obj, embed=embed, view=view)
            else:
                await loading_msg.edit(content=None, embed=embed, view=view)
                msg = loading_msg
            
            view.message = msg
            
            asyncio.create_task(self.get_available_seasons_progressive(user_api_id, view, initial_count=4))
            
        except Exception as e:
            error_embed = discord.Embed(
                title="⚠️ 오류 발생",
                description=f"```{str(e)}```",
                color=0xff9900
            )
            await loading_msg.edit(content=None, embed=error_embed)
            
async def setup(bot: commands.Bot):
    await bot.add_cog(UserRankCog(bot))