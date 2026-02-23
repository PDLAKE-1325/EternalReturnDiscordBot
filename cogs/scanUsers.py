#cogs/scanUsers.py
import re
import discord
from discord.ext import commands
import aiohttp
import asyncio
import base64
import time
from google import genai
from google.genai import types
from config import AI_KEY, ER_KEY

from data import CURRENT_SEASON_NUM, CURRENT_SEASON

ER_BASE    = "https://open-api.bser.io/v1"
MATCH_MODE = 3

MAX_RECHECK       = 4   # Gemini 재질의 최대 라운드
RANK_CACHE_TTL    = 3600
MAX_RETRY_429     = 3
MAX_VARIANTS_PER_NAME = 15  # 닉네임당 변형 후보 최대 시도 수 (1rps 환경에서 15초 상한)

HIDDEN_NAME_RE = re.compile(r"^실험체\d+$")

# ── 하이픈 변형 후보 ──────────────────────────
HYPHEN_VARIANTS = [
    "\u2500",  # ─
    "-",       # -
    "\u4e00",  # 一
    "\u2013",  # –
    "\u2014",  # —
    "\u2212",  # −
    "\uff0d",  # －
]

# ── 한글 자모 분리/결합 ───────────────────────
def _decompose(ch: str) -> tuple[int, int, int] | None:
    code = ord(ch) - 0xAC00
    if not (0 <= code <= 11171):
        return None
    return code // 28 // 21, (code // 28) % 21, code % 28

def _compose(cho: int, jung: int, jong: int) -> str:
    return chr(0xAC00 + cho * 21 * 28 + jung * 28 + jong)

# 초성 인덱스: ㄱ=0 ㄲ=1 ㄴ=2 ㄷ=3 ㄸ=4 ㄹ=5 ㅁ=6 ㅂ=7 ㅃ=8 ㅅ=9 ㅆ=10
#              ㅇ=11 ㅈ=12 ㅉ=13 ㅊ=14 ㅋ=15 ㅌ=16 ㅍ=17 ㅎ=18
CHO_CONFUSE = [
    (0, 15),   # ㄱ↔ㅋ
    (3,  5),   # ㄷ↔ㄹ
    (3, 16),   # ㄷ↔ㅌ
    (5,  6),   # ㄹ↔ㅁ
    (5, 11),   # ㄹ↔ㅇ  ← '룡→몽' 핵심
    (6,  7),   # ㅁ↔ㅂ
    (7, 17),   # ㅂ↔ㅍ
    (9, 10),   # ㅅ↔ㅆ
    (11, 18),  # ㅇ↔ㅎ
    (12, 14),  # ㅈ↔ㅊ
    (2,  3),   # ㄴ↔ㄷ
]

# 중성 인덱스: ㅏ=0 ㅐ=1 ㅑ=2 ㅓ=4 ㅔ=5 ㅕ=6 ㅗ=8 ㅛ=12 ㅜ=13 ㅠ=17 ㅡ=18 ㅣ=20
JUNG_CONFUSE = [
    (0,  2),   # ㅏ↔ㅑ
    (1,  5),   # ㅐ↔ㅔ
    (4,  6),   # ㅓ↔ㅕ
    (8, 12),   # ㅗ↔ㅛ
    (8, 18),   # ㅗ↔ㅡ
    (13, 17),  # ㅜ↔ㅠ
    (13, 18),  # ㅜ↔ㅡ
]

# 종성 인덱스: 0=없음 1=ㄱ 4=ㄴ 8=ㄷ 11=ㄹ 16=ㅁ 17=ㅂ 19=ㅅ 21=ㅇ
JONG_CONFUSE = [
    (0,  21),  # 없음↔ㅇ받침  (통→똥)
    (11, 21),  # ㄹ↔ㅇ 받침   (룡→몽)
    (16, 21),  # ㅁ↔ㅇ 받침
    (0,  17),  # 없음↔ㅂ받침
    (0,  19),  # 없음↔ㅅ받침
    (4,   0),  # ㄴ받침↔없음
]

def _jamo_variants_char(ch: str) -> list[str]:
    dec = _decompose(ch)
    if dec is None:
        return []
    cho, jung, jong = dec
    results = set()
    for a, b in CHO_CONFUSE:
        if cho == a: results.add(_compose(b, jung, jong))
        if cho == b: results.add(_compose(a, jung, jong))
    for a, b in JUNG_CONFUSE:
        if jung == a: results.add(_compose(cho, b, jong))
        if jung == b: results.add(_compose(cho, a, jong))
    for a, b in JONG_CONFUSE:
        if jong == a: results.add(_compose(cho, jung, b))
        if jong == b: results.add(_compose(cho, jung, a))
    results.discard(ch)
    return list(results)

def _jamo_variants_1depth(nickname: str) -> list[str]:
    """1글자만 변형 (1-depth)."""
    candidates: set[str] = set()
    chars = list(nickname)
    for i, ch in enumerate(chars):
        for vc in _jamo_variants_char(ch):
            cand = "".join(chars[:i] + [vc] + chars[i+1:])
            if cand != nickname:
                candidates.add(cand)
    return list(candidates)

def _jamo_variants_2depth(nickname: str, base_list: list[str]) -> list[str]:
    """
    1-depth 결과에서 추가로 1글자 변형 (2-depth).
    base_list: _jamo_variants_1depth 결과를 재사용해서 중복 계산 방지.
    """
    candidates: set[str] = set()
    for partial in base_list:
        pchars = list(partial)
        for i, ch in enumerate(pchars):
            for vc in _jamo_variants_char(ch):
                cand = "".join(pchars[:i] + [vc] + pchars[i+1:])
                if cand != nickname:
                    candidates.add(cand)
    return list(candidates)

def _hyphen_variants(nickname: str) -> list[str]:
    found = next((ch for ch in HYPHEN_VARIANTS if ch in nickname), None)
    if not found:
        return []
    return [
        nickname.replace(found, v)
        for v in HYPHEN_VARIANTS
        if nickname.replace(found, v) != nickname
    ]

def _latin_ocr_variants(nickname: str) -> list[str]:
    LATIN = [
        ("0","O"),("O","0"),
        ("1","l"),("l","1"),("1","I"),("I","1"),
        ("rn","m"),("m","rn"),
    ]
    result = []
    for wrong, right in LATIN:
        if wrong in nickname:
            c = nickname.replace(wrong, right, 1)
            if c != nickname and c not in result:
                result.append(c)
    return result

def _all_variants_1depth(nickname: str) -> list[str]:
    """하이픈 + 라틴 OCR + 자모 1-depth 변형."""
    jamo1 = _jamo_variants_1depth(nickname)
    seen, result = set(), []
    for c in _hyphen_variants(nickname) + _latin_ocr_variants(nickname) + jamo1:
        if c not in seen:
            seen.add(c); result.append(c)
    return result

def _all_variants_2depth(nickname: str) -> list[str]:
    """
    1-depth 결과에서 추가 자모 변형만 (2-depth).
    하이픈/라틴은 이미 1-depth에서 처리됨.
    """
    jamo1 = _jamo_variants_1depth(nickname)
    jamo2 = _jamo_variants_2depth(nickname, jamo1)
    seen = set(jamo1) | set(_hyphen_variants(nickname)) | set(_latin_ocr_variants(nickname))
    result = []
    for c in jamo2:
        if c not in seen:
            seen.add(c); result.append(c)
    return result


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
    return f"{TIER_EMOJI.get(tier, '')} {tier}"


# ────────────────────────────────────────────
# OCR 응답 파서
# ────────────────────────────────────────────
def _parse_teams(text: str) -> list[list[str]]:
    TEAM_HEADER_RE = re.compile(r"^팀\s*(\d+)$")
    teams_numbered: dict[int, list[str]] = {}
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
        elif len(line) > 1 and current_num is not None:
            current.append(line)

    if current_num is not None and current:
        teams_numbered[current_num] = current

    if not teams_numbered:
        names = [l.strip() for l in text.splitlines() if len(l.strip()) > 1]
        return [names] if names else []

    sorted_nums = sorted(teams_numbered.keys())
    expected_max = sorted_nums[0] + len(sorted_nums) - 1
    actual_max   = sorted_nums[-1]
    if actual_max > expected_max + 1:
        print(f"[파서 경고] 팀 번호 불연속: {sorted_nums} → {actual_max}를 {sorted_nums[-2]}에 병합")
        last_valid = sorted_nums[-2]
        teams_numbered[last_valid].extend(teams_numbered.pop(actual_max))
        sorted_nums = sorted(teams_numbered.keys())

    return [teams_numbered[n] for n in sorted_nums]


# ────────────────────────────────────────────
# RateLimiter — 초당 1회
# ────────────────────────────────────────────
class RateLimiter:
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
        self._userid_cache: dict[str, str]               = {}
        self._rank_cache:   dict[str, tuple[dict, float]] = {}

    def _get_rank_cache(self, user_id: str) -> dict | None:
        entry = self._rank_cache.get(user_id)
        if entry and (time.monotonic() - entry[1]) < RANK_CACHE_TTL:
            return entry[0]
        return None

    def _set_rank_cache(self, user_id: str, data: dict):
        self._rank_cache[user_id] = (data, time.monotonic())

    # ── Gemini OCR ──────────────────────────────
    def extract_teams_from_image(self, image_bytes: bytes) -> list[list[str]]:
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "화면에 표시된 팀 번호(01, 02, 03 ...)를 기준으로 팀을 구분하고, "
            "각 팀의 플레이어 닉네임을 출력하라.\n\n"
            "출력 형식 (팀 수·인원 수는 이미지에 보이는 그대로):\n"
            "팀1\n닉네임A\n닉네임B\n\n팀2\n닉네임C\n닉네임D\n\n"
            "규칙:\n"
            "- '팀N' 헤더 다음 줄부터 닉네임 한 줄에 하나.\n"
            "- 팀 사이 반드시 빈 줄 하나.\n"
            "- 화면에 없는 팀 절대 추가 금지.\n"
            "- 닉네임 외 설명·번호·기호 절대 금지.\n"
            "- 팀 구분 불가능하면 '팀1' 하나로 전부 묶어 출력."
        )
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        res = self.gemini.models.generate_content(
            model="models/gemini-3-flash-preview",
            contents=[types.Content(role="user", parts=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/png", data=image_b64))
            ])]
        )
        text = "".join(
            p.text for p in res.candidates[0].content.parts
            if hasattr(p, "text") and p.text
        ).strip()
        return _parse_teams(text)

    def recheck_failed_nicknames(
        self, image_bytes: bytes, failed_names: list[str]
    ) -> dict[str, list[str]]:
        names_str = "\n".join(f"- {n}" for n in failed_names)
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "아래 닉네임들은 OCR 인식 결과인데 게임 API 조회에 실패했다.\n"
            "이미지를 다시 보고 각 닉네임이 실제로 어떻게 적혀 있는지 정확히 읽어라.\n\n"
            f"실패 목록:\n{names_str}\n\n"
            "출력 형식 (불확실하면 후보 여러 개 '|' 로 구분):\n"
            "원래닉네임|수정된닉네임\n"
            "원래닉네임2|후보A|후보B\n\n"
            "주의사항:\n"
            "- 반드시 '|' 구분자, 한 줄에 하나.\n"
            "- 변경 없으면 원래 닉네임 그대로.\n"
            "- 하이픈류 문자(─ - 一 – —)는 원본 그대로.\n"
            "- 한글 초성 혼동 잦음: ㄹ↔ㄷ, ㄹ↔ㅁ, ㅂ↔ㅁ, ㅈ↔ㅊ, ㄱ↔ㅋ, ㄷ↔ㄹ, ㅅ↔ㅆ.\n"
            "- 한글 받침 혼동 잦음: 받침없음↔ㅇ받침(통↔똥), ㄹ↔ㅇ받침(룡↔몽).\n"
            "- 한글 모음 혼동 잦음: ㅏ↔ㅑ, ㅓ↔ㅕ, ㅗ↔ㅛ, ㅜ↔ㅠ, ㅡ↔ㅗ, ㅡ↔ㅜ, ㅐ↔ㅔ.\n"
            "- 설명·번호·기호 절대 금지."
        )
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        res = self.gemini.models.generate_content(
            model="models/gemini-3-flash-preview",
            contents=[types.Content(role="user", parts=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/png", data=image_b64))
            ])]
        )
        text = "".join(
            p.text for p in res.candidates[0].content.parts
            if hasattr(p, "text") and p.text
        ).strip()

        corrections: dict[str, list[str]] = {}
        for line in text.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.split("|")
            original   = parts[0].strip()
            candidates = [p.strip() for p in parts[1:] if p.strip()]
            if original and candidates:
                corrections[original] = candidates
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
                f"{ER_BASE}/user/nickname", headers=headers, params={"query": nickname}
            ) as r:
                body = await r.json()
                if r.status == 429:
                    await asyncio.sleep(1); continue
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
                    await asyncio.sleep(1); continue
                if r.status != 200:
                    return None
                user_rank = body.get("userRank")
                if user_rank:
                    self._set_rank_cache(user_id, user_rank)
                return user_rank
        return None

    async def get_user_data(self, session: aiohttp.ClientSession, nickname: str) -> dict:
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
        tier = _calc_tier(mmr, rank, _season_num(CURRENT_SEASON_NUM))
        return {"nickname": nickname, "tier": tier, "mmr": mmr, "rank": rank, "hidden": False}

    async def _try_candidates_serial(
        self,
        session: aiohttp.ClientSession,
        old_name: str,
        candidates: list[str],
        tried: set[str],
        limit: int = MAX_VARIANTS_PER_NAME,
    ) -> dict | None:
        """
        후보를 순서대로 1rps 직렬 조회.
        성공하면 즉시 반환 (나머지 후보는 건너뜀).
        tried에 이미 있는 후보는 스킵. limit 초과 시 중단.
        """
        count = 0
        for candidate in candidates:
            if candidate in tried:
                continue
            if count >= limit:
                print(f"[한도 초과] {old_name!r}: {limit}개 시도 후 중단")
                break
            tried.add(candidate)
            count += 1
            data = await self.get_user_data(session, candidate)
            if data["tier"] is not None:
                print(f"[후보 성공] {old_name!r} → {candidate!r}, tier={data['tier']}")
                return data
        return None

    # ── Command ─────────────────────────────────
    @commands.command(name="대기분석", aliases=["ㄷㄱㅂㅅ"])
    async def lobby_scan(self, ctx):
        if not ctx.message.attachments:
            await ctx.send("이미지 첨부 필요")
            return

        image_bytes = await ctx.message.attachments[0].read()
        msg = await ctx.send("🔍 이미지 분석중...")
        print(f"\n{'='*40}\n[대기분석 시작] by {ctx.author}\n{'='*40}")

        # ── OCR ──
        teams: list[list[str]] = await asyncio.to_thread(
            self.extract_teams_from_image, image_bytes
        )
        all_names = [n for team in teams for n in team]
        if not all_names:
            await msg.edit(content="❌ 닉네임 인식 실패 (이미지를 확인해주세요)")
            return

        hidden_count = sum(1 for n in all_names if HIDDEN_NAME_RE.match(n))
        need_api     = len(all_names) - hidden_count

        preview_lines = []
        for i, team in enumerate(teams, 1):
            preview_lines.append(f"── 팀 {i} ──")
            for n in team:
                preview_lines.append(f"• {n}{'  🔒' if HIDDEN_NAME_RE.match(n) else ''}")
        names_preview = "\n".join(preview_lines)

        await msg.edit(content=(
            f"✅ **{len(all_names)}명** 인식 완료 (팀 {len(teams)}개 | 비공개 {hidden_count}명)\n"
            f"```\n{names_preview}\n```\n"
            f"⧖ 전적 조회중... (0 / {need_api})"
        ))
        print(f"[인식] 총={len(all_names)}, 팀={len(teams)}, 비공개={hidden_count}, API 필요={need_api}")

        # ── 1차 API 조회 (순차) ──
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
        def build_embed(results: list[list[dict]], retrying: bool = False) -> tuple[discord.Embed, int, list[str]]:
            embed = discord.Embed(
                title="📊 대기창 분석 결과",
                description=f"시즌 {CURRENT_SEASON} 랭크 정보",
                color=discord.Color.blue()
            )
            _ok, _fail = 0, []
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
                        suffix = f" #{r['rank']:,}" if r["tier"] == "이터니티" else ""
                        team_lines.append(f"> {r['nickname']} | {tier_display(r['tier'])}{suffix}")
                        _ok += 1
                embed.add_field(
                    name=f"**팀 {team_idx:02d}**",
                    value="\n".join(team_lines) or "—",
                    inline=True
                )
                if team_idx % 2 == 0:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
            if _fail:
                embed.add_field(
                    name="𒄬 조회 실패 — 재시도 중..." if retrying else "𒄬 최종 조회 실패",
                    value="\n".join(f"• {n}" for n in _fail),
                    inline=False
                )
            embed.set_footer(text=(
                f"총 {len(all_names)}명 | 팀 {len(teams)}개 "
                f"| 조회 성공 {_ok}명 | 비공개 {hidden_count}명"
            ))
            return embed, _ok, _fail

        has_fail = any(not r["hidden"] and r["tier"] is None for team in team_results for r in team)
        embed, ok_count, fail_names = build_embed(team_results, retrying=has_fail)
        await msg.edit(content="", embed=embed)
        print(f"[1차 완료] 팀={len(teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")

        if not fail_names:
            return

        # tried: 닉네임별 이미 시도한 후보 기록
        tried_candidates: dict[str, set[str]] = {
            r["nickname"]: {r["nickname"]}
            for team in team_results for r in team
            if not r["hidden"] and r["tier"] is None
        }

        # ────────────────────────────────────────
        # 0단계: 자모/하이픈 변형 시도
        # 1-depth 먼저, 실패 시 2-depth (단, Gemini 재시도 전까지만)
        # ────────────────────────────────────────
        async with aiohttp.ClientSession() as session:
            any_updated = False
            for ti, team_data in enumerate(team_results):
                for pi, r in enumerate(team_data):
                    if r["hidden"] or r["tier"] is not None:
                        continue
                    old = r["nickname"]
                    tried = tried_candidates[old]

                    # 1-depth
                    cands_1 = _all_variants_1depth(old)
                    print(f"[변형 1-depth] {old!r}: {len(cands_1)}개")
                    resolved = await self._try_candidates_serial(session, old, cands_1, tried)

                    # 2-depth (1-depth 실패 시, 상한 절반으로 줄여서 시도)
                    if not resolved:
                        cands_2 = _all_variants_2depth(old)
                        print(f"[변형 2-depth] {old!r}: {len(cands_2)}개 (상한 {MAX_VARIANTS_PER_NAME//2})")
                        resolved = await self._try_candidates_serial(
                            session, old, cands_2, tried, limit=MAX_VARIANTS_PER_NAME // 2
                        )

                    if resolved:
                        team_results[ti][pi] = resolved
                        any_updated = True
                    else:
                        print(f"[변형 전부 실패] {old!r}")

        if any_updated:
            embed, ok_count, fail_names = build_embed(team_results, retrying=True)
            await msg.edit(embed=embed)
            if not fail_names:
                return

        # ────────────────────────────────────────
        # Gemini 재질의 라운드
        # Gemini 수정안 기반 후보 + 그 1-depth 변형만 시도 (2-depth는 비용 대비 효과 낮음)
        # ────────────────────────────────────────
        for recheck_round in range(1, MAX_RECHECK + 1):
            failed_entries = [
                (ti, pi, r)
                for ti, td in enumerate(team_results)
                for pi, r in enumerate(td)
                if not r["hidden"] and r["tier"] is None
            ]
            if not failed_entries:
                break

            failed_names_list = [e[2]["nickname"] for e in failed_entries]
            print(f"[Gemini 재시도 {recheck_round}] 실패: {failed_names_list}")

            corrections = await asyncio.to_thread(
                self.recheck_failed_nicknames, image_bytes, failed_names_list
            )
            print(f"[Gemini 재시도 {recheck_round}] 수정안: {corrections}")

            any_new_candidate = False
            any_updated = False

            async with aiohttp.ClientSession() as session:
                for ti, pi, r in failed_entries:
                    old_name     = r["nickname"]
                    gemini_names = corrections.get(old_name, [old_name])
                    tried        = tried_candidates.setdefault(old_name, {old_name})

                    # Gemini 수정안 + 각 수정안의 1-depth 변형
                    new_cands: list[str] = []
                    for gn in gemini_names:
                        if gn != old_name and gn not in tried and gn not in new_cands:
                            new_cands.append(gn)
                        for v in _all_variants_1depth(gn):
                            if v not in tried and v not in new_cands:
                                new_cands.append(v)

                    if not new_cands:
                        print(f"[Gemini {recheck_round}] {old_name!r}: 새 후보 없음")
                        continue

                    any_new_candidate = True
                    print(f"[Gemini {recheck_round}] {old_name!r} 후보 {len(new_cands)}개")
                    resolved = await self._try_candidates_serial(session, old_name, new_cands, tried)
                    if resolved:
                        team_results[ti][pi] = resolved
                        any_updated = True

            if any_updated:
                embed, ok_count, fail_names = build_embed(team_results, retrying=True)
                await msg.edit(embed=embed)
                if not fail_names:
                    break

            if not any_new_candidate:
                print(f"[조기 종료] 라운드 {recheck_round}: 새 후보 없음")
                break

        # ── 최종 ──
        final_embed, ok_count, fail_names = build_embed(team_results, retrying=False)
        await msg.edit(embed=final_embed)
        print(f"[최종] 팀={len(teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))