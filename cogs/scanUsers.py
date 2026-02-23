#cogs/scanUsers.py
import re
import io
import discord
from discord.ext import commands
import aiohttp
import asyncio
import base64
import time
from PIL import Image, ImageEnhance
from google import genai
from google.genai import types
from config import AI_KEY, ER_KEY

from data import CURRENT_SEASON_NUM, CURRENT_SEASON

ER_BASE    = "https://open-api.bser.io/v1"
MATCH_MODE = 3

MAX_RECHECK    = 3     # Gemini 재질의 최대 라운드 (후보 수 강화로 3회로 충분)
RANK_CACHE_TTL = 3600  # 랭크 캐시 유지 시간 (초)
MAX_RETRY_429  = 3     # 429 최대 재시도 횟수

# 비공개 닉네임 패턴: "실험체1", "실험체12" 등
HIDDEN_NAME_RE = re.compile(r"^실험체\d+$")

# ── 하이픈 변형 후보 ──────────────────────────
HYPHEN_VARIANTS = [
    "\u2500",  # ─  BOX DRAWINGS LIGHT HORIZONTAL
    "-",       # -  HYPHEN-MINUS (ASCII)
    "\u4e00",  # 一 CJK 한자 일
    "\u2013",  # –  EN DASH
    "\u2014",  # —  EM DASH
    "\u2212",  # −  MINUS SIGN
    "\uff0d",  # － FULLWIDTH HYPHEN-MINUS
]


def _hyphen_variants(nickname: str) -> list[str]:
    found_hyphen = None
    for ch in HYPHEN_VARIANTS:
        if ch in nickname:
            found_hyphen = ch
            break
    if found_hyphen is None:
        return []

    candidates = []
    for variant in HYPHEN_VARIANTS:
        candidate = nickname.replace(found_hyphen, variant)
        if candidate != nickname and candidate not in candidates:
            candidates.append(candidate)
    return candidates


# ────────────────────────────────────────────
# 이미지 전처리 (Fotor 기본 조정 기준)
# ─────────────────────────────────────────────
# Fotor 슬라이더 → Pillow ImageEnhance 배율 매핑:
#   factor = 1.0 + (fotor_value / 100)
#   밝기  -30  → 0.70
#   대비 +100  → 2.00
#   채도 -100  → 0.00  (완전 흑백)
#   선명도+150 → 2.50
# ────────────────────────────────────────────
def _preprocess_image(image_bytes: bytes) -> bytes:
    """
    OCR 정확도 향상을 위해 이미지를 전처리한다.
    Fotor 기준 밝기 -30, 대비 +100, 채도 -100, 선명도 +150 적용.
    반환값: 전처리된 PNG bytes
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    # 1. 밝기 (Brightness): factor 0.70
    img = ImageEnhance.Brightness(img).enhance(0.70)

    # 2. 대비 (Contrast): factor 2.00
    img = ImageEnhance.Contrast(img).enhance(2.00)

    # 3. 채도 (Color/Saturation): factor 0.00 → 완전 흑백
    img = ImageEnhance.Color(img).enhance(0.00)

    # 4. 선명도 (Sharpness): factor 2.50
    img = ImageEnhance.Sharpness(img).enhance(2.50)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ────────────────────────────────────────────
# 티어 계산
# ────────────────────────────────────────────
def _season_num(season_id: int) -> int:
    return (season_id - 19) // 2

def _calc_tier(mmr: int, rank: int, season_num: int) -> str:
    def eternity(mmr_cut, rank_cut_e, rank_cut_d):
        if rank and rank <= rank_cut_e: return "이터니티"
        if rank and rank <= rank_cut_d: return "데미갓"
        return "미스릴"

    if season_num < 3:
        if mmr >= 6200: return eternity(6200, 200, 700)
        if mmr >= 6000: return "미스릴"
        if mmr >= 5000: return "다이아몬드"
        if mmr >= 4000: return "플레티넘"
        if mmr >= 3000: return "골드"
        if mmr >= 2000: return "실버"
        if mmr >= 1000: return "브론즈"
        return "아이언"
    elif season_num < 4:
        if mmr >= 6400: return eternity(6400, 200, 700)
        if mmr >= 6200: return "미스릴"
        if mmr >= 4800: return "다이아몬드"
        if mmr >= 3600: return "플레티넘"
        if mmr >= 2600: return "골드"
        if mmr >= 1600: return "실버"
        if mmr >= 800:  return "브론즈"
        return "아이언"
    elif season_num < 5:
        if mmr >= 7000: return eternity(7000, 200, 700)
        if mmr >= 6800: return "미스릴"
        if mmr >= 5200: return "다이아몬드"
        if mmr >= 3800: return "플레티넘"
        if mmr >= 2600: return "골드"
        if mmr >= 1600: return "실버"
        if mmr >= 800:  return "브론즈"
        return "아이언"
    elif season_num < 6:
        if mmr >= 7500: return eternity(7500, 200, 700)
        if mmr >= 6800: return "미스릴"
        if mmr >= 6400: return "메테오라이트"
        if mmr >= 5000: return "다이아몬드"
        if mmr >= 3600: return "플레티넘"
        if mmr >= 2400: return "골드"
        if mmr >= 1400: return "실버"
        if mmr >= 600:  return "브론즈"
        return "아이언"
    elif season_num < 7:
        if mmr >= 7700: return eternity(7700, 300, 1000)
        if mmr >= 7000: return "미스릴"
        if mmr >= 6400: return "메테오라이트"
        if mmr >= 5000: return "다이아몬드"
        if mmr >= 3600: return "플레티넘"
        if mmr >= 2400: return "골드"
        if mmr >= 1400: return "실버"
        if mmr >= 600:  return "브론즈"
        return "아이언"
    elif season_num < 9:
        if mmr >= 7800: return eternity(7800, 300, 1000)
        if mmr >= 7100: return "미스릴"
        if mmr >= 6400: return "메테오라이트"
        if mmr >= 5000: return "다이아몬드"
        if mmr >= 3600: return "플레티넘"
        if mmr >= 2400: return "골드"
        if mmr >= 1400: return "실버"
        if mmr >= 600:  return "브론즈"
        return "아이언"
    elif season_num < 10:
        if mmr >= 7900: return eternity(7900, 300, 1000)
        if mmr >= 7200: return "미스릴"
        if mmr >= 6400: return "메테오라이트"
        if mmr >= 5000: return "다이아몬드"
        if mmr >= 3600: return "플레티넘"
        if mmr >= 2400: return "골드"
        if mmr >= 1400: return "실버"
        if mmr >= 600:  return "브론즈"
        return "아이언"
    else:
        if mmr >= 8100: return eternity(8100, 300, 1000)
        if mmr >= 7400: return "미스릴"
        if mmr >= 6400: return "메테오라이트"
        if mmr >= 5000: return "다이아몬드"
        if mmr >= 3600: return "플레티넘"
        if mmr >= 2400: return "골드"
        if mmr >= 1400: return "실버"
        if mmr >= 600:  return "브론즈"
        return "아이언"


TIER_EMOJI = {
    "이터니티":    "<:Immortal:1475215908665299035>",
    "데미갓":      "<:Titan:1475215920313139261>",
    "미스릴":      "<:Mithril:1475215913778413609>",
    "메테오라이트": "<:Meteorite:1475215912083652760>",
    "다이아몬드":  "<:Diamond:1475215904789762169>",
    "플레티넘":    "<:Platinum:1475215916332482893>",
    "골드":        "<:Gold:1475215906635518012>",
    "실버":        "<:Silver:1475215918509326438>",
    "브론즈":      "<:Bronze:1475215903468556364>",
    "아이언":      "<:Iron:1475215910313656422>",
    "Unranked":    "<:Unrank:1475215921797664868>",
}


def tier_display(tier: str) -> str:
    emoji = TIER_EMOJI.get(tier, "")
    return f"{emoji} {tier}"


# ────────────────────────────────────────────
# OCR 응답 파서
# ────────────────────────────────────────────
def _parse_teams(text: str) -> list[list[str]]:
    """
    Gemini가 반환한 팀 구분 텍스트를 파싱.
    - 팀 번호가 불연속이면 빈 슬롯은 건너뛰되 경고 출력.
    - 팀 번호가 연속 최댓값보다 2 이상 튀면 이전 팀에 병합 (환각 방지).
    """
    TEAM_HEADER_RE = re.compile(r"^팀\s*(\d+)$")
    teams_numbered: dict[int, list[str]] = {}  # { 팀번호: [닉네임...] }
    current_num: int | None = None
    current: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = TEAM_HEADER_RE.match(line)
        if m:
            if current_num is not None and current:
                teams_numbered[current_num] = current
            current_num = int(m.group(1))
            current = []
        else:
            if len(line) > 1:
                if current_num is not None:
                    current.append(line)

    if current_num is not None and current:
        teams_numbered[current_num] = current

    if not teams_numbered:
        names = [l.strip() for l in text.splitlines() if len(l.strip()) > 1]
        return [names] if names else []

    # ── 환각 팀 번호 탐지 ──
    sorted_nums = sorted(teams_numbered.keys())
    if sorted_nums:
        expected_max = sorted_nums[0] + len(sorted_nums) - 1
        actual_max   = sorted_nums[-1]
        if actual_max > expected_max + 1:
            print(f"[파서 경고] 팀 번호 불연속: {sorted_nums} → 최댓값 {actual_max}를 {sorted_nums[-2]}에 병합")
            last_valid = sorted_nums[-2]
            teams_numbered[last_valid].extend(teams_numbered.pop(actual_max))
            sorted_nums = sorted(teams_numbered.keys())

    return [teams_numbered[n] for n in sorted_nums]


# ────────────────────────────────────────────
# RateLimiter
# ────────────────────────────────────────────
class RateLimiter:
    """초당 1회 보장"""
    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / rate_per_sec
        self.lock = asyncio.Lock()
        self.last_called = 0.0

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            wait_time = self.interval - (now - self.last_called)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self.last_called = time.monotonic()


# ────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────
class LobbyScan(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini = genai.Client(api_key=AI_KEY)
        self.rl = RateLimiter(rate_per_sec=1)

        self._userid_cache: dict[str, str]              = {}
        self._rank_cache:   dict[str, tuple[dict, float]] = {}

    # ── 캐시 헬퍼 ──────────────────────────────
    def _get_rank_cache(self, user_id: str) -> dict | None:
        entry = self._rank_cache.get(user_id)
        if entry and (time.monotonic() - entry[1]) < RANK_CACHE_TTL:
            return entry[0]
        return None

    def _set_rank_cache(self, user_id: str, data: dict):
        self._rank_cache[user_id] = (data, time.monotonic())

    # ── Gemini OCR (팀 구분) ──────────────────
    def extract_teams_from_image(self, image_bytes: bytes) -> list[list[str]]:
        """
        이미지에서 팀별 닉네임 목록을 추출한다.
        전처리(밝기/대비/채도/선명도) 후 Gemini에 전달.
        반환값: [[팀1닉1, 팀1닉2, ...], [팀2닉1, ...], ...]
        팀 구분이 불가능하면 전체를 하나의 팀으로 묶어 반환.
        """
        # ── 전처리 적용 ──
        processed_bytes = _preprocess_image(image_bytes)

        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "화면에 표시된 팀 번호(01, 02, 03 ...)를 기준으로 팀을 구분하고, "
            "각 팀의 플레이어 닉네임을 출력하라.\n\n"
            "출력 형식 (팀 수·인원 수는 이미지에 보이는 그대로):\n"
            "팀1\n"
            "닉네임A\n"
            "닉네임B\n"
            "\n"
            "팀2\n"
            "닉네임C\n"
            "닉네임D\n\n"
            "규칙:\n"
            "- '팀N' 헤더 다음 줄부터 해당 팀 닉네임을 한 줄에 하나씩 나열.\n"
            "- 팀 사이에 반드시 빈 줄 하나.\n"
            "- 화면에 보이지 않는 팀을 임의로 추가하지 말 것.\n"
            "- 닉네임 외 설명·번호·기호 절대 금지.\n"
            "- 팀 구분이 불가능하면 '팀1' 하나로 전부 묶어 출력."
        )
        image_b64 = base64.b64encode(processed_bytes).decode("utf-8")
        res = self.gemini.models.generate_content(
            model="models/gemini-3-flash-preview",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=prompt),
                        types.Part(inline_data=types.Blob(
                            mime_type="image/png",
                            data=image_b64
                        ))
                    ]
                )
            ]
        )
        text = "".join(
            part.text for part in res.candidates[0].content.parts
            if hasattr(part, "text") and part.text
        ).strip()

        # print(f"[OCR 원본 응답]\n{text}\n{'-'*30}")
        return _parse_teams(text)

    def recheck_failed_nicknames(
        self, image_bytes: bytes, failed_names: list[str]
    ) -> dict[str, list[str]]:
        """
        조회 실패한 닉네임 목록을 원본 이미지와 함께 Gemini에 재질의.
        전처리된 이미지를 사용한다.

        반환값: { 원래_닉네임: [후보1, 후보2, ...] }
        Gemini가 변경 없다고 판단하면 원래 닉네임만 포함한 리스트 반환.
        후보를 최대 4개까지 반환할 수 있음.
        """
        # ── 전처리 적용 ──
        processed_bytes = _preprocess_image(image_bytes)

        names_str = "\n".join(f"- {n}" for n in failed_names)
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "아래 닉네임들은 OCR 인식 결과인데 게임 API 조회에 실패했다. "
            "이미지를 다시 보고 각 닉네임이 실제로 어떻게 적혀 있는지 정확히 읽어라.\n\n"
            f"실패 목록:\n{names_str}\n\n"
            "출력 형식:\n"
            "원래닉네임|+]수정된닉네임\n"
            "원래닉네임2|+]후보A|+]후보B|+]후보C   ← 불확실하면 후보 최대 4개를 '|+]'로 구분\n\n"
            "규칙:\n"
            "- 반드시 '|+]' 구분자 사용, 한 줄에 하나씩.\n"
            "- 변경 없으면 원래 닉네임 그대로 출력.\n"
            "- 확신이 없을 때는 가능성 있는 후보를 모두 나열하라 (최대 4개).\n"
            "- 하이픈 모양 문자(─, -, 一, –, —, −, － 등)가 포함된 닉네임은 "
            "각 하이픈 변형을 후보로 추가하라.\n"
            "- OCR 혼동이 잦은 문자 쌍을 적극 고려하라:\n"
            "  · 숫자/라틴: 0↔O, 1↔l↔I, rn↔m\n"
            "  · 한글 모음: ㅏ↔ㅑ, ㅓ↔ㅕ, ㅗ↔ㅛ, ㅜ↔ㅠ, ㅐ↔ㅔ, ㅡ↔ㅗ↔ㅜ, ㅣ↔ㅏ↔ㅓ\n"
            "  · 한글 초성: ㅈ↔ㅊ, ㄱ↔ㅋ, ㅂ↔ㅍ↔ㄹ↔ㅁ, ㅅ↔ㅆ, ㄷ↔ㄹ↔ㅌ\n"
            "- 설명·번호·기호 절대 금지."
        )
        image_b64 = base64.b64encode(processed_bytes).decode("utf-8")
        res = self.gemini.models.generate_content(
            model="models/gemini-3-flash-preview",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=prompt),
                        types.Part(inline_data=types.Blob(
                            mime_type="image/png",
                            data=image_b64
                        ))
                    ]
                )
            ]
        )
        text = "".join(
            part.text for part in res.candidates[0].content.parts
            if hasattr(part, "text") and part.text
        ).strip()

        corrections: dict[str, list[str]] = {}
        for line in text.splitlines():
            line = line.strip()
            if "|+]" not in line:
                continue
            parts = line.split("|+]")
            original   = parts[0].strip()
            candidates = [p.strip() for p in parts[1:] if p.strip()]
            if original and candidates:
                corrections[original] = candidates

        # 목록에 없는 닉네임은 그대로
        for n in failed_names:
            corrections.setdefault(n, [n])

        return corrections

    # ── ER API ──────────────────────────────────
    async def get_user_id(self, session: aiohttp.ClientSession, nickname: str) -> str | None:
        if nickname in self._userid_cache:
            return self._userid_cache[nickname]

        headers = {"x-api-key": ER_KEY}
        for _ in range(MAX_RETRY_429):
            await self.rl.wait()
            async with session.get(
                f"{ER_BASE}/user/nickname",
                headers=headers,
                params={"query": nickname}
            ) as r:
                body = await r.json()
                if r.status == 429:
                    await asyncio.sleep(1)
                    continue
                if r.status != 200:
                    return None
                user_id = body.get("user", {}).get("userId")
                if user_id:
                    self._userid_cache[nickname] = user_id
                return user_id
        return None

    async def get_rank(self, session: aiohttp.ClientSession, user_id: str) -> dict | None:
        cached = self._get_rank_cache(user_id)
        if cached is not None:
            return cached

        headers = {"x-api-key": ER_KEY}
        for _ in range(MAX_RETRY_429):
            await self.rl.wait()
            async with session.get(
                f"{ER_BASE}/rank/uid/{user_id}/{CURRENT_SEASON_NUM}/{MATCH_MODE}",
                headers=headers
            ) as r:
                body = await r.json()
                if r.status == 429:
                    await asyncio.sleep(1)
                    continue
                if r.status != 200:
                    return None
                user_rank = body.get("userRank")
                if user_rank:
                    self._set_rank_cache(user_id, user_rank)
                return user_rank
        return None

    async def get_user_data(self, session: aiohttp.ClientSession, nickname: str) -> dict:
        """항상 dict 반환. 비공개/언랭/실패 모두 포함."""
        if HIDDEN_NAME_RE.match(nickname):
            return {"nickname": nickname, "tier": None, "mmr": None, "rank": None, "hidden": True}

        user_id = await self.get_user_id(session, nickname)
        if not user_id:
            return {"nickname": nickname, "tier": None, "mmr": None, "rank": None, "hidden": False}

        rank_data = await self.get_rank(session, user_id)
        if not rank_data or not rank_data.get("rank"):
            return {"nickname": nickname, "tier": "Unranked", "mmr": 0, "rank": None, "hidden": False}

        mmr  = rank_data.get("mmr", 0)
        rank = rank_data.get("rank", 0)
        snum = _season_num(CURRENT_SEASON_NUM)
        tier = _calc_tier(mmr, rank, snum)

        return {"nickname": nickname, "tier": tier, "mmr": mmr, "rank": rank, "hidden": False}

    # ── Command ─────────────────────────────────
    @commands.command(name="대기분석", aliases=["ㄷㄱㅂㅅ"])
    async def lobby_scan(self, ctx):
        if not ctx.message.attachments:
            await ctx.reply("이미지 첨부 필요")
            return

        image_bytes = await ctx.message.attachments[0].read()
        msg = await ctx.reply("🔍 이미지 분석중...")
        print(f"\n{'='*40}\n[대기분석 시작] by {ctx.author}\n{'='*40}")

        # ── Gemini OCR (팀 구분) ──
        teams: list[list[str]] = await asyncio.to_thread(
            self.extract_teams_from_image, image_bytes
        )
        all_names = [name for team in teams for name in team]

        if not all_names:
            await msg.edit(content="❌ 닉네임 인식 실패 (이미지를 확인해주세요)")
            return

        hidden_count = sum(1 for n in all_names if HIDDEN_NAME_RE.match(n))
        need_api     = len(all_names) - hidden_count

        preview_lines = []
        for i, team in enumerate(teams, 1):
            preview_lines.append(f"── 팀 {i} ──")
            for n in team:
                lock = "  🔒" if HIDDEN_NAME_RE.match(n) else ""
                preview_lines.append(f"• {n}{lock}")
        names_preview = "\n".join(preview_lines)

        await msg.edit(content=(
            f"✅ **{len(all_names)}명** 인식 완료 (팀 {len(teams)}개 | 비공개 {hidden_count}명)\n"
            f"```\n{names_preview}\n```\n"
            f"⧖ 전적 조회중... (0 / {need_api})"
        ))
        print(f"[인식] 총={len(all_names)}, 팀={len(teams)}, 비공개={hidden_count}, API 필요={need_api}")

        # ── ER API 순차 조회 ──
        team_results: list[list[dict]] = []
        api_done = 0

        async with aiohttp.ClientSession() as session:
            for team in teams:
                tr = []
                for name in team:
                    if not HIDDEN_NAME_RE.match(name):
                        api_done += 1
                        await msg.edit(content=(
                            f"✅ **{len(all_names)}명** 인식 완료 (팀 {len(teams)}개 | 비공개 {hidden_count}명)\n"
                            f"```\n{names_preview}\n```\n"
                            f"⧖ 전적 조회중... ({api_done} / {need_api}) — `{name}`"
                        ))
                    tr.append(await self.get_user_data(session, name))
                team_results.append(tr)

        # ── 임베드 빌더 ──
        def build_embed(results: list[list[dict]]) -> tuple[discord.Embed, int, list[str]]:
            embed = discord.Embed(
                title="📊 대기창 분석 결과",
                description=f"시즌 {CURRENT_SEASON} 랭크 정보",
                color=discord.Color.blue()
            )
            _ok   = 0
            _fail = []
            for team_idx, team_data in enumerate(results, 1):
                team_lines = []
                for r in team_data:
                    if r["hidden"]:
                        team_lines.append("> 닉네임 비공개")
                    elif r["tier"] is None:
                        _fail.append(r["nickname"])
                        team_lines.append(f"> ~~{r['nickname']}~~ | 조회 실패")
                    elif r["tier"] == "Unranked":
                        team_lines.append(f"> {r['nickname']} | {tier_display('Unranked')}")
                        _ok += 1
                    else:
                        if r["tier"] == "이터니티":
                            team_lines.append(
                                f"> {r['nickname']} | {tier_display(r['tier'])} #{r['rank']:,}"
                            )
                        else:
                            team_lines.append(
                                f"> {r['nickname']} | {tier_display(r['tier'])}"
                            )
                        _ok += 1
                embed.add_field(
                    name=f"**팀 {team_idx:02d}**",
                    value="\n".join(team_lines) if team_lines else "—",
                    inline=True
                )
                if team_idx % 2 == 0:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
            if _fail:
                embed.add_field(
                    name="𒄬 조회 실패 — 재시도 중...",
                    value="\n".join(f"• {n}" for n in _fail),
                    inline=False
                )
            embed.set_footer(
                text=(
                    f"총 {len(all_names)}명 | 팀 {len(teams)}개 "
                    f"| 조회 성공 {_ok}명 | 비공개 {hidden_count}명"
                )
            )
            return embed, _ok, _fail

        embed, ok_count, fail_names = build_embed(team_results)
        await msg.edit(content="", embed=embed)
        print(f"[1차 완료] 팀={len(teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")

        # ── 0단계: 하이픈 변형 시도 (Gemini 없음) ──
        hyphen_targets = [
            (ti, pi, r)
            for ti, team_data in enumerate(team_results)
            for pi, r in enumerate(team_data)
            if not r["hidden"] and r["tier"] is None and _hyphen_variants(r["nickname"])
        ]
        if hyphen_targets:
            print(f"[하이픈 변형 시도] {len(hyphen_targets)}명 대상")
            any_hyphen_updated = False
            async with aiohttp.ClientSession() as session:
                for ti, pi, r in hyphen_targets:
                    old_name   = r["nickname"]
                    candidates = _hyphen_variants(old_name)
                    print(f"[하이픈 변형 시도] {old_name!r} → {candidates}")
                    resolved   = None
                    for candidate in candidates:
                        new_data = await self.get_user_data(session, candidate)
                        if new_data["tier"] is not None:
                            new_data["nickname"] = candidate
                            resolved = new_data
                            print(f"[하이픈 변형 성공] {old_name!r} → {candidate!r}, tier={new_data['tier']}")
                            break
                    if resolved:
                        team_results[ti][pi] = resolved
                        any_hyphen_updated   = True
                    else:
                        print(f"[하이픈 변형 전부 실패] {old_name!r}")

            if any_hyphen_updated:
                embed, ok_count, fail_names = build_embed(team_results)
                await msg.edit(embed=embed)

        # ── Gemini 재질의 라운드 ──
        # tried_candidates: { 원래닉네임: {이미 시도한 후보들} }
        tried_candidates: dict[str, set[str]] = {}

        for recheck_round in range(1, MAX_RECHECK + 1):
            failed_entries = [
                (ti, pi, r)
                for ti, team_data in enumerate(team_results)
                for pi, r in enumerate(team_data)
                if not r["hidden"] and r["tier"] is None
            ]
            if not failed_entries:
                break

            failed_names_list = [e[2]["nickname"] for e in failed_entries]
            print(f"[Gemini 재시도 {recheck_round}] 실패 닉네임: {failed_names_list}")

            corrections: dict[str, list[str]] = await asyncio.to_thread(
                self.recheck_failed_nicknames, image_bytes, failed_names_list
            )
            print(f"[Gemini 재시도 {recheck_round}] 수정안: {corrections}")

            any_updated       = False
            any_new_candidate = False

            async with aiohttp.ClientSession() as session:
                for ti, pi, r in failed_entries:
                    old_name     = r["nickname"]
                    gemini_names = corrections.get(old_name, [old_name])
                    tried        = tried_candidates.setdefault(old_name, set())

                    # 새로운 후보만 필터링 (원래 닉네임 및 이미 시도한 것 제외)
                    new_candidates = [
                        gn for gn in gemini_names
                        if gn != old_name and gn not in tried
                    ]

                    if not new_candidates:
                        print(f"[Gemini 재시도 {recheck_round}] {old_name!r}: 새 후보 없음, 스킵")
                        continue

                    any_new_candidate = True
                    tried.update(new_candidates)
                    print(f"[Gemini 재시도 {recheck_round}] {old_name!r} 후보: {new_candidates}")

                    resolved = None
                    for candidate in new_candidates:
                        new_data = await self.get_user_data(session, candidate)
                        if new_data["tier"] is not None:
                            new_data["nickname"] = candidate
                            resolved = new_data
                            print(f"[Gemini 재시도 성공] {old_name!r} → {candidate!r}, tier={new_data['tier']}")
                            break

                    if resolved:
                        team_results[ti][pi] = resolved
                        any_updated          = True
                    else:
                        print(f"[Gemini 재시도 {recheck_round}] {old_name!r}: 모든 후보 실패")

            if any_updated:
                embed, ok_count, fail_names = build_embed(team_results)
                await msg.edit(embed=embed)

            if not any_new_candidate:
                print(f"[조기 종료] 라운드 {recheck_round}: 모든 실패 닉네임에 새 후보 없음")
                break

        # ── 최종 임베드 (실패 필드 문구 정리) ──
        final_embed, ok_count, fail_names = build_embed(team_results)
        for i, field in enumerate(final_embed.fields):
            if field.name.startswith("𒄬 조회 실패 — 재시도"):
                final_embed.set_field_at(
                    i,
                    name="𒄬 최종 조회 실패",
                    value=field.value,
                    inline=False
                )
        await msg.edit(embed=final_embed)
        print(f"[최종] 팀={len(teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))