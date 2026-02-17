"""
이터널 리턴 캐릭터 검색 Cog
-----------------------------
명령어:
  !캐릭터 <이름>         - 스킬 정보 + 추천 무기 임베드 출력
  !티어 [무기종류]       - 캐릭터 티어/픽률 통계 출력
  !추천 <이름>           - 캐릭터별 추천 아이템/무기 출력

필요 환경변수 (.env):
  ER_KEY=<이터널 리턴 개발자 포털에서 발급받은 API 키>
  DISCORD_TOKEN=<디스코드 봇 토큰>

의존성:
  pip install discord.py python-dotenv aiohttp
"""

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from config import ER_KEY
ER_BASE_URL = "https://open-api.bser.io"
API_VERSION = "v1"

# ── 무기 타입 한글 매핑 ─────────────────────────────────────
WEAPON_TYPE_KR = {
    "Glove": "글러브",
    "Tonfa": "톤파",
    "Bat": "배트",
    "Whip": "채찍",
    "HighAngleFire": "곡사포",
    "Arcane": "아케인",
    "Hammer": "해머",
    "CrossBow": "석궁",
    "Pistol": "권총",
    "AssaultRifle": "돌격소총",
    "SniperRifle": "저격소총",
    "Spear": "창",
    "DualSword": "쌍검",
    "Sword": "검",
    "TwoHandedSword": "대검",
    "Rapier": "레이피어",
    "Axe": "도끼",
    "HealingStaff": "치유 지팡이",
    "DefensiveStaff": "방어 지팡이",
    "Bow": "활",
    "Throw": "투척",
    "Shuriken": "수리검",
    "Nunchaku": "쌍절곤",
}

# ── 스킬 슬롯 한글 매핑 ────────────────────────────────────
SKILL_SLOT_KR = {
    "Q": "Q",
    "W": "W",
    "E": "E",
    "R": "R (궁극기)",
    "Passive": "패시브",
}

# ── 티어 색상 ──────────────────────────────────────────────
TIER_COLORS = {
    "S+": 0xFF0000,
    "S": 0xFF4500,
    "A": 0xFFA500,
    "B": 0xFFD700,
    "C": 0x00BFFF,
    "D": 0x808080,
}


# ═══════════════════════════════════════════════════════════
#  API 헬퍼
# ═══════════════════════════════════════════════════════════
class ERApiClient:
    """이터널 리턴 Open API 비동기 클라이언트"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"x-api-key": api_key}
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.headers)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get(self, endpoint: str, params: dict | None = None) -> dict | None:
        session = await self._get_session()
        url = f"{ER_BASE_URL}/{API_VERSION}/{endpoint}"
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except aiohttp.ClientError:
            return None

    # ── 메타데이터 ────────────────────────────────────────
    async def get_characters(self) -> list[dict]:
        """모든 캐릭터 메타데이터 조회"""
        data = await self.get("data/Character")
        return data.get("data", []) if data else []

    async def get_character_skills(self, character_code: int) -> list[dict]:
        """특정 캐릭터 스킬 목록 조회"""
        data = await self.get("data/CharacterSkill", {"characterCode": character_code})
        return data.get("data", []) if data else []

    async def get_skill_descriptions(self) -> list[dict]:
        """스킬 설명 텍스트 전체 조회"""
        data = await self.get("data/SkillInfo")
        return data.get("data", []) if data else []

    async def get_character_weapons(self, character_code: int) -> list[str]:
        """캐릭터 사용 가능 무기 타입 조회"""
        characters = await self.get_characters()
        for char in characters:
            if char.get("code") == character_code:
                return [char.get("characterMastery", "")]
        return []

    # ── 통계 ─────────────────────────────────────────────
    async def get_character_stats(
        self, season_id: int = 0, mode: int = 3
    ) -> list[dict]:
        """
        캐릭터 통계 (픽률·승률) 조회
        mode: 2=솔로, 3=스쿼드, 4=듀오
        """
        data = await self.get(f"statistics/character", {"seasonId": season_id, "mode": mode})
        return data.get("data", {}).get("characterStats", []) if data else []

    async def get_character_weapon_stats(
        self, character_code: int, season_id: int = 0, mode: int = 3
    ) -> list[dict]:
        """특정 캐릭터의 무기별 통계 (픽률·승률·추천 빌드 포함)"""
        data = await self.get(
            f"statistics/character/{character_code}",
            {"seasonId": season_id, "mode": mode},
        )
        return data.get("data", {}).get("characterWeaponStat", []) if data else []


# ═══════════════════════════════════════════════════════════
#  Cog
# ═══════════════════════════════════════════════════════════
class ERCharacterCog(commands.Cog, name="이터널 리턴 캐릭터"):
    """이터널 리턴 캐릭터 정보를 검색하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api = ERApiClient(ER_KEY)
        self._character_cache: dict[str, dict] | None = None  # 이름→캐릭터 객체

    async def cog_unload(self):
        await self.api.close()

    # ── 내부 유틸 ─────────────────────────────────────────
    async def _load_characters(self) -> dict[str, dict]:
        """캐릭터 목록을 캐시에 로드 (한글 이름 기준)"""
        if self._character_cache is not None:
            return self._character_cache
        chars = await self.api.get_characters()
        self._character_cache = {}
        for c in chars:
            # API 이름(영문)과 한글 이름을 모두 키로 등록
            eng = c.get("name", "").lower()
            kor = c.get("localizedName", c.get("name", "")).strip()
            self._character_cache[eng] = c
            self._character_cache[kor] = c
        return self._character_cache

    async def _find_character(self, name: str) -> dict | None:
        """이름으로 캐릭터 검색 (대소문자·공백 무시)"""
        cache = await self._load_characters()
        key = name.strip().lower()
        # 정확 일치
        if key in cache:
            return cache[key]
        # 부분 일치
        for k, v in cache.items():
            if key in k.lower():
                return v
        return None

    @staticmethod
    def _pick_rate_bar(rate: float, width: int = 10) -> str:
        filled = round(rate / 10 * width)
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _tier_from_rank(rank: int, total: int) -> str:
        pct = rank / max(total, 1) * 100
        if pct <= 2:
            return "S+"
        elif pct <= 8:
            return "S"
        elif pct <= 25:
            return "A"
        elif pct <= 50:
            return "B"
        elif pct <= 75:
            return "C"
        return "D"

    # ── 명령어: !캐릭터 ──────────────────────────────────
    @commands.command(name="캐릭터", aliases=["character", "char"])
    async def character_info(self, ctx: commands.Context, *, name: str):
        """
        !캐릭터 <이름>
        스킬 정보와 사용 가능한 무기 타입을 보여줍니다.
        """
        async with ctx.typing():
            char = await self._find_character(name)
            if char is None:
                await ctx.send(f"❌ **{name}** 캐릭터를 찾을 수 없어요. 이름을 다시 확인해주세요.")
                return

            code = char.get("code")
            char_name = char.get("localizedName") or char.get("name", "?")
            mastery = char.get("characterMastery", "")  # 주무기 타입
            mastery_kr = WEAPON_TYPE_KR.get(mastery, mastery)

            # 스킬 조회
            skills = await self.api.get_character_skills(code)

            embed = discord.Embed(
                title=f"🔍 {char_name}",
                description=f"**주 무기 타입:** {mastery_kr}",
                color=0x7289DA,
            )

            # 스킬 슬롯별 정보 추가
            slot_order = ["Passive", "Q", "W", "E", "R"]
            skill_by_slot: dict[str, list[dict]] = {s: [] for s in slot_order}
            for sk in skills:
                slot = sk.get("skillSlot", "")
                if slot in skill_by_slot:
                    skill_by_slot[slot].append(sk)

            for slot in slot_order:
                sk_list = skill_by_slot[slot]
                if not sk_list:
                    continue
                sk = sk_list[0]  # 기본 형태만 표시
                slot_display = SKILL_SLOT_KR.get(slot, slot)
                sk_name = sk.get("name", "?")
                sk_desc = sk.get("description", "설명 없음")
                # 긴 설명은 앞 120자만
                if len(sk_desc) > 120:
                    sk_desc = sk_desc[:120].rstrip() + "…"
                embed.add_field(
                    name=f"[{slot_display}] {sk_name}",
                    value=sk_desc or "설명 없음",
                    inline=False,
                )

            embed.set_footer(text="!추천 <이름> 으로 추천 빌드를 확인하세요 | 이터널 리턴 Open API")
            if char.get("characterImagePath"):
                embed.set_thumbnail(url=char["characterImagePath"])

            await ctx.send(embed=embed)

    # ── 명령어: !티어 ─────────────────────────────────────
    @commands.command(name="티어", aliases=["tier", "stats"])
    async def tier_list(self, ctx: commands.Context, *, weapon_filter: str = ""):
        """
        !티어 [무기종류]
        캐릭터 티어/픽률 통계를 보여줍니다. 무기 종류로 필터링 가능합니다.
        예: !티어 검
        """
        async with ctx.typing():
            stats = await self.api.get_character_stats()
            if not stats:
                await ctx.send("⚠️ 통계 데이터를 불러오지 못했어요. 잠시 후 다시 시도해주세요.")
                return

            cache = await self._load_characters()
            total = len(stats)

            # 픽률 기준 정렬 (내림차순)
            sorted_stats = sorted(stats, key=lambda x: x.get("pickRate", 0), reverse=True)

            lines = []
            rank = 0
            for stat in sorted_stats:
                char_code = stat.get("characterCode")
                # 캐릭터 이름 찾기
                char_name = str(char_code)
                char_obj = next((v for v in cache.values() if v.get("code") == char_code), None)
                if char_obj:
                    char_name = char_obj.get("localizedName") or char_obj.get("name", str(char_code))
                    mastery = char_obj.get("characterMastery", "")
                    mastery_kr = WEAPON_TYPE_KR.get(mastery, mastery)
                else:
                    mastery_kr = "?"

                # 무기 필터 적용
                if weapon_filter and weapon_filter not in mastery_kr:
                    continue

                rank += 1
                tier = self._tier_from_rank(rank, total)
                pick = stat.get("pickRate", 0.0)
                win = stat.get("winRate", 0.0)
                bar = self._pick_rate_bar(pick)

                lines.append(
                    f"`{tier:2s}` **{char_name}** ({mastery_kr})\n"
                    f"　픽률 {bar} {pick:.1f}%　승률 {win:.1f}%"
                )

                if rank >= 20:  # 최대 20위까지만 표시
                    break

            if not lines:
                await ctx.send(f"❌ **{weapon_filter}** 무기를 사용하는 캐릭터 통계가 없어요.")
                return

            title = f"📊 캐릭터 티어 (픽률 순위 TOP {len(lines)})"
            if weapon_filter:
                title += f" — {weapon_filter} 필터"

            # 25개 필드 제한이 있으므로 텍스트 임베드로 처리
            # 한 번에 10개씩 페이지 나누기
            chunk_size = 10
            pages = [lines[i : i + chunk_size] for i in range(0, len(lines), chunk_size)]
            for idx, page in enumerate(pages):
                embed = discord.Embed(
                    title=title if idx == 0 else f"{title} (계속)",
                    description="\n\n".join(page),
                    color=0xFFA500,
                )
                embed.set_footer(text=f"페이지 {idx+1}/{len(pages)} | 이터널 리턴 Open API")
                await ctx.send(embed=embed)

    # ── 명령어: !추천 ─────────────────────────────────────
    @commands.command(name="추천", aliases=["recommend", "build"])
    async def recommend_build(self, ctx: commands.Context, *, name: str):
        """
        !추천 <이름>
        캐릭터의 무기별 추천 아이템/빌드를 보여줍니다.
        """
        async with ctx.typing():
            char = await self._find_character(name)
            if char is None:
                await ctx.send(f"❌ **{name}** 캐릭터를 찾을 수 없어요.")
                return

            code = char.get("code")
            char_name = char.get("localizedName") or char.get("name", "?")

            weapon_stats = await self.api.get_character_weapon_stats(code)
            if not weapon_stats:
                await ctx.send(
                    f"⚠️ **{char_name}**의 빌드 통계를 불러오지 못했어요. "
                    "시즌 초이거나 데이터가 아직 없을 수 있어요."
                )
                return

            # 픽률 상위 무기 3가지만
            sorted_ws = sorted(weapon_stats, key=lambda x: x.get("pickRate", 0), reverse=True)[:3]

            embed = discord.Embed(
                title=f"⚔️ {char_name} — 추천 빌드",
                color=0x2ECC71,
            )

            for ws in sorted_ws:
                weapon_type = ws.get("weaponType", "?")
                weapon_kr = WEAPON_TYPE_KR.get(weapon_type, weapon_type)
                pick = ws.get("pickRate", 0.0)
                win = ws.get("winRate", 0.0)

                # 추천 아이템 코드 목록 → 이름 변환 (API에서 itemName 포함 여부에 따라 다름)
                top_items: list[str] = []
                for item_entry in ws.get("topItems", [])[:6]:
                    item_name = item_entry.get("itemName") or item_entry.get("name") or str(item_entry.get("itemCode", "?"))
                    top_items.append(item_name)

                items_text = " → ".join(top_items) if top_items else "데이터 없음"

                embed.add_field(
                    name=f"🔫 {weapon_kr}  (픽률 {pick:.1f}% / 승률 {win:.1f}%)",
                    value=f"**추천 아이템:** {items_text}",
                    inline=False,
                )

            embed.set_footer(text="이터널 리턴 Open API | 현재 시즌 기준")
            await ctx.send(embed=embed)

    # ── 에러 핸들러 ───────────────────────────────────────
    @character_info.error
    @tier_list.error
    @recommend_build.error
    async def command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                "❗ 캐릭터 이름을 입력해주세요.\n"
                "예시: `!캐릭터 아야`, `!티어 검`, `!추천 아야`"
            )
        else:
            await ctx.send(f"⚠️ 오류가 발생했어요: `{error}`")


# ═══════════════════════════════════════════════════════════
#  Setup (discord.py v2 방식)
# ═══════════════════════════════════════════════════════════
async def setup(bot: commands.Bot):
    await bot.add_cog(ERCharacterCog(bot))