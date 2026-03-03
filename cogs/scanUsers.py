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

MAX_RECHECK    = 3     # Gemini 재질의 최대 라운드
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

# ── 좌표 파싱 정규식 ─────────────────────────
# "닉네임A [123, 456, 150, 580]" 형태 파싱
COORD_LINE_RE = re.compile(
    r"^(.*?)\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]\s*$"
)


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
# 이미지 전처리 (밝기/대비/채도/선명도)
# ────────────────────────────────────────────
def _preprocess_image(image_bytes: bytes) -> bytes:
    """OCR 정확도 향상을 위한 전처리. PNG bytes 반환."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(0.70)
    img = ImageEnhance.Contrast(img).enhance(1.50)
    img = ImageEnhance.Color(img).enhance(0.00)
    img = ImageEnhance.Sharpness(img).enhance(2.00)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ────────────────────────────────────────────
# 동적 크롭
# ────────────────────────────────────────────
def _crop_nickname_region(
    image_bytes: bytes,
    box: list[int],
    padding_norm: int = 30,
    min_output_width: int = 500,
) -> bytes:
    """
    0~1000 정규화 좌표 [ymin, xmin, ymax, xmax] 로 닉네임 영역을 크롭한다.

    Args:
        padding_norm: 바운딩박스 주변 패딩 (0~1000 단위)
        min_output_width: 결과 이미지 최소 너비 (픽셀). 작으면 업스케일.
    Returns:
        전처리된 크롭 PNG bytes
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size

    ymin_n, xmin_n, ymax_n, xmax_n = box

    # 패딩 적용 및 범위 클램프
    ymin_n = max(0, ymin_n - padding_norm)
    xmin_n = max(0, xmin_n - padding_norm)
    ymax_n = min(1000, ymax_n + padding_norm)
    xmax_n = min(1000, xmax_n + padding_norm)

    # 픽셀 변환
    x0 = int(xmin_n / 1000 * w)
    y0 = int(ymin_n / 1000 * h)
    x1 = int(xmax_n / 1000 * w)
    y1 = int(ymax_n / 1000 * h)

    # 영역이 너무 작으면 크롭 포기 → None 반환 신호
    if (x1 - x0) < 5 or (y1 - y0) < 5:
        raise ValueError(f"크롭 영역이 너무 작음: box={box}, px=({x0},{y0},{x1},{y1})")

    cropped = img.crop((x0, y0, x1, y1))

    # 업스케일 (너비가 min_output_width 미만이면 확대)
    if cropped.width < min_output_width:
        scale = min_output_width / cropped.width
        new_w = int(cropped.width  * scale)
        new_h = int(cropped.height * scale)
        cropped = cropped.resize((new_w, new_h), Image.LANCZOS)

    # 전처리 적용
    cropped = ImageEnhance.Brightness(cropped).enhance(0.70)
    cropped = ImageEnhance.Contrast(cropped).enhance(1.50)
    cropped = ImageEnhance.Color(cropped).enhance(0.00)
    cropped = ImageEnhance.Sharpness(cropped).enhance(2.00)

    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
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
# OCR 응답 파서 (좌표 포함 버전)
# ────────────────────────────────────────────
def _parse_teams(text: str) -> list[list[dict]]:
    """
    Gemini가 반환한 팀 구분 텍스트를 파싱.
    각 플레이어는 {"name": str, "box": [ymin,xmin,ymax,xmax] | None} 형태.

    좌표가 없는 줄도 name만 추출해서 box=None으로 저장.
    """
    TEAM_HEADER_RE = re.compile(r"^팀\s*(\d+)$")
    teams_numbered: dict[int, list[dict]] = {}
    current_num: int | None = None
    current: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 팀 헤더 체크
        m_header = TEAM_HEADER_RE.match(line)
        if m_header:
            if current_num is not None and current:
                teams_numbered[current_num] = current
            current_num = int(m_header.group(1))
            current = []
            continue

        # 좌표 포함 줄 파싱
        m_coord = COORD_LINE_RE.match(line)
        if m_coord:
            name = m_coord.group(1).strip()
            box  = [int(m_coord.group(i)) for i in range(2, 6)]
            if len(name) > 1 and current_num is not None:
                current.append({"name": name, "box": box})
        else:
            # 좌표 없는 줄 → name만 저장
            if len(line) > 1 and current_num is not None:
                current.append({"name": line, "box": None})

    if current_num is not None and current:
        teams_numbered[current_num] = current

    if not teams_numbered:
        # 헤더 구분 없이 닉네임만 쭉 나열된 경우
        entries = []
        for l in text.splitlines():
            l = l.strip()
            if len(l) <= 1:
                continue
            m_coord = COORD_LINE_RE.match(l)
            if m_coord:
                name = m_coord.group(1).strip()
                box  = [int(m_coord.group(i)) for i in range(2, 6)]
                if len(name) > 1:
                    entries.append({"name": name, "box": box})
            elif len(l) > 1:
                entries.append({"name": l, "box": None})
        return [entries] if entries else []

    # ── 환각 팀 번호 탐지 ──
    sorted_nums = sorted(teams_numbered.keys())
    if sorted_nums:
        expected_max = sorted_nums[0] + len(sorted_nums) - 1
        actual_max   = sorted_nums[-1]
        if actual_max > expected_max + 1:
            print(f"[파서 경고] 팀 번호 불연속: {sorted_nums} → {actual_max}를 {sorted_nums[-2]}에 병합")
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

        self._userid_cache: dict[str, str]               = {}
        self._rank_cache:   dict[str, tuple[dict, float]] = {}

    # ── 캐시 헬퍼 ──────────────────────────────
    def _get_rank_cache(self, user_id: str) -> dict | None:
        entry = self._rank_cache.get(user_id)
        if entry and (time.monotonic() - entry[1]) < RANK_CACHE_TTL:
            return entry[0]
        return None

    def _set_rank_cache(self, user_id: str, data: dict):
        self._rank_cache[user_id] = (data, time.monotonic())

    # ── Gemini 호출 헬퍼 ────────────────────────
    def _gemini_call(self, prompt: str, image_bytes: bytes) -> str:
        """전처리된 이미지 bytes를 받아 Gemini에 전달하고 텍스트 응답 반환."""
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
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
        return "".join(
            part.text for part in res.candidates[0].content.parts
            if hasattr(part, "text") and part.text
        ).strip()

    # ── Gemini OCR (팀 구분 + 좌표 추출) ──────────
    def extract_teams_from_image(self, image_bytes: bytes) -> list[list[dict]]:
        """
        이미지에서 팀별 플레이어 목록을 추출한다.
        각 플레이어: {"name": str, "box": [ymin, xmin, ymax, xmax] | None}
        좌표는 이미지 전체를 0~1000으로 정규화한 정수.
        """
        processed_bytes = _preprocess_image(image_bytes)
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "화면에 표시된 팀 번호(01, 02, 03 ...)를 기준으로 팀을 구분하고, "
            "각 팀의 플레이어 닉네임과 해당 닉네임 텍스트가 위치한 영역의 좌표를 출력하라.\n\n"
            "출력 형식 (좌표는 반드시 닉네임 뒤에 [ymin, xmin, ymax, xmax] 형태로 붙일 것):\n"
            "팀1\n"
            "닉네임A [123, 456, 150, 580]\n"
            "닉네임B [210, 456, 235, 580]\n"
            "\n"
            "팀2\n"
            "닉네임C [123, 600, 150, 720]\n\n"
            "규칙:\n"
            "- 좌표는 이미지 전체 크기를 0~1000으로 정규화한 정수 값으로 표시하라.\n"
            "- 각 좌표는 해당 닉네임 글자가 온전히 포함되도록 정확하게 잡아라.\n"
            "- '팀N' 헤더 다음 줄부터 해당 팀 정보를 한 줄에 하나씩 나열.\n"
            "- 팀 사이에 반드시 빈 줄 하나.\n"
            "- 화면에 보이지 않는 팀을 임의로 추가하지 말 것.\n"
            "- 닉네임과 좌표 외 설명·번호·기호 절대 금지.\n"
            "- 팀 구분이 불가능하면 '팀1' 하나로 전부 묶어 출력.\n\n"
            "닉네임 인식 주의사항:\n"
            "- 하이픈 계열 문자(─, -, 一, –, —, −, － 등)는 이미지에 보이는 그대로 출력하라. 임의로 다른 하이픈으로 바꾸지 말 것.\n"
            "- 대소문자를 정확히 구분하라.\n"
            "- 한국어의 이중 모음과 받침 구분에 주의하라.\n"
            "- OCR 혼동이 잦은 문자 쌍을 주의하라:\n"
            "  · 한글 모음: ㅏ↔ㅑ, ㅓ↔ㅕ, ㅗ↔ㅛ, ㅜ↔ㅠ, ㅐ↔ㅔ, ㅡ↔ㅗ↔ㅜ, ㅣ↔ㅏ↔ㅓ\n"
            "  · 한글 초성: ㅈ↔ㅊ, ㄱ↔ㅋ, ㅂ↔ㅍ↔ㄹ↔ㅁ, ㅅ↔ㅆ, ㄷ↔ㄹ↔ㅌ\n"
        )
        text = self._gemini_call(prompt, processed_bytes)
        # print(f"[OCR 원본 응답]\n{text}\n{'-'*30}")
        return _parse_teams(text)

    # ── 동적 크롭 재질의 (단일 닉네임) ────────────
    def recheck_with_crop(
        self, image_bytes: bytes, name: str, box: list[int]
    ) -> list[str]:
        """
        닉네임 영역을 크롭해서 Gemini에 집중 재질의한다.
        가능성 있는 닉네임 후보 리스트를 반환한다 (최대 4개).
        실패 시 [name] (원래 이름) 반환.
        """
        try:
            crop_bytes = _crop_nickname_region(image_bytes, box)
        except ValueError as e:
            print(f"[크롭 실패] {name!r}: {e}")
            return [name]

        prompt = (
            "이터널 리턴 대기창에서 특정 플레이어의 닉네임 영역만 잘라낸 이미지다.\n"
            f"이 이미지에서 닉네임을 정확히 읽어라. 현재 OCR 결과는 '{name}'이지만 틀릴 수 있다.\n\n"
            "출력 형식:\n"
            "후보1|+]후보2|+]후보3   ← 불확실하면 최대 4개 후보를 '|+]'로 구분\n\n"
            "규칙:\n"
            "- 가장 확실한 후보를 맨 앞에 놓아라.\n"
            "- 확신이 있으면 후보 1개만 출력해도 됨.\n"
            "- 하이픈 계열 문자는 이미지에 보이는 그대로 출력하라.\n"
            "- 대소문자 정확히 구분하라.\n"
            "- OCR 혼동이 잦은 문자 쌍을 적극 고려하라:\n"
            "  · 숫자/라틴: 0↔O, 1↔l↔I, rn↔m\n"
            "  · 한글 모음: ㅏ↔ㅑ, ㅓ↔ㅕ, ㅗ↔ㅛ, ㅜ↔ㅠ, ㅐ↔ㅔ, ㅡ↔ㅗ↔ㅜ, ㅣ↔ㅏ↔ㅓ\n"
            "  · 한글 초성: ㅈ↔ㅊ, ㄱ↔ㅋ, ㅂ↔ㅍ↔ㄹ↔ㅁ, ㅅ↔ㅆ, ㄷ↔ㄹ↔ㅌ\n"
            "- 하이픈이 포함된 닉네임은 다양한 하이픈 변형을 후보로 추가하라.\n"
            "- 닉네임 외 설명·기호 절대 금지."
        )
        text = self._gemini_call(prompt, crop_bytes)
        candidates = [c.strip() for c in text.split("|+]") if c.strip()]
        return candidates if candidates else [name]

    # ── 전체 이미지 재질의 (여러 닉네임, 폴백용) ──
    def recheck_failed_nicknames(
        self, image_bytes: bytes, failed_names: list[str]
    ) -> dict[str, list[str]]:
        """
        조회 실패한 닉네임 목록을 원본 이미지와 함께 Gemini에 재질의.
        반환값: { 원래_닉네임: [후보1, 후보2, ...] }
        """
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
        text = self._gemini_call(prompt, processed_bytes)

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

        for n in failed_names:
            corrections.setdefault(n, [n])
        return corrections

    # ── ER API ──────────────────────────────────
    async def get_user_id(self, session, nickname):
        if nickname in self._userid_cache:
            return self._userid_cache[nickname]

        headers = {"x-api-key": ER_KEY}
        for attempt in range(MAX_RETRY_429):
            await self.rl.wait()
            async with session.get(
                f"{ER_BASE}/user/nickname",
                headers=headers,
                params={"query": nickname}
            ) as r:
                if r.status == 429:
                    wait = 2 ** attempt  # 지수 백오프: 1초 → 2초 → 4초
                    print(f"[429] {nickname!r} 재시도 {attempt+1}/{MAX_RETRY_429}, {wait}초 대기")
                    await asyncio.sleep(wait)
                    continue
                
                body = await r.json()
                
                if r.status != 200:
                    print(f"[API 실패] {nickname!r}: status={r.status}, body={body}")
                    return None
                
                user_id = body.get("user", {}).get("userId")
                
                # ← 핵심 수정: userId=0도 유효한 값으로 처리
                if user_id is not None:
                    self._userid_cache[nickname] = user_id
                    return user_id
                
                print(f"[userId 없음] {nickname!r}: body={body}")
                return None
        
        print(f"[429 한도 초과] {nickname!r}: {MAX_RETRY_429}회 재시도 모두 실패")
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
        """항상 dict 반환. tier/mmr/rank/hidden 포함. box는 호출자가 별도 관리."""
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
            await ctx.send("이미지 첨부 필요")
            return

        image_bytes = await ctx.message.attachments[0].read()
        msg = await ctx.send("🔍 이미지 분석중...")
        print(f"\n{'='*40}\n[대기분석 시작] by {ctx.author}\n{'='*40}")

        # ── Gemini OCR (팀 구분 + 좌표 추출) ──
        # teams: list[list[{"name": str, "box": list|None}]]
        ocr_teams: list[list[dict]] = await asyncio.to_thread(
            self.extract_teams_from_image, image_bytes
        )

        all_ocr = [entry for team in ocr_teams for entry in team]
        if not all_ocr:
            await msg.edit(content="❌ 닉네임 인식 실패 (이미지를 확인해주세요)")
            return

        hidden_count = sum(1 for e in all_ocr if HIDDEN_NAME_RE.match(e["name"]))
        need_api     = len(all_ocr) - hidden_count
        box_count    = sum(1 for e in all_ocr if e["box"] is not None)

        preview_lines = []
        for i, team in enumerate(ocr_teams, 1):
            preview_lines.append(f"── 팀 {i} ──")
            for e in team:
                lock    = "  🔒" if HIDDEN_NAME_RE.match(e["name"]) else ""
                has_box = "" #❖" if e["box"] else ""
                preview_lines.append(f"• {e['name']}{lock}{has_box}")
        names_preview = "\n".join(preview_lines)

        await msg.edit(content=(
            f"✅ **{len(all_ocr)}명** 인식 완료 "
            f"(팀 {len(ocr_teams)}개 | 비공개 {hidden_count}명 | 좌표 {box_count}명)\n"
            f"```\n{names_preview}\n```\n"
            f"⧖ 전적 조회중... (0 / {need_api})"
        ))
        print(
            f"[인식] 총={len(all_ocr)}, 팀={len(ocr_teams)}, "
            f"비공개={hidden_count}, 좌표={box_count}, API 필요={need_api}"
        )

        # ── ER API 순차 조회 ──
        # team_results: list[list[dict]]
        # 각 dict: get_user_data 결과 + "box" 키 추가
        team_results: list[list[dict]] = []
        api_done = 0

        async with aiohttp.ClientSession() as session:
            for ocr_team in ocr_teams:
                tr = []
                for entry in ocr_team:
                    name = entry["name"]
                    box  = entry["box"]
                    if not HIDDEN_NAME_RE.match(name):
                        api_done += 1
                        await msg.edit(content=(
                            f"✅ **{len(all_ocr)}명** 인식 완료 "
                            f"(팀 {len(ocr_teams)}개 | 비공개 {hidden_count}명)\n"
                            f"```\n{names_preview}\n```\n"
                            f"⧖ 전적 조회중... ({api_done} / {need_api}) — `{name}`"
                        ))
                    data = await self.get_user_data(session, name)
                    data["box"] = box  # 좌표 보존
                    tr.append(data)
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
                        team_lines.append(f"> ~~**`{r['nickname']}`** ~~ · 조회 실패")
                    elif r["tier"] == "Unranked":
                        team_lines.append(f"> **`{r['nickname']}`**  · {tier_display('Unranked')}")
                        _ok += 1
                    else:
                        if r["tier"] == "이터니티": #
                            team_lines.append(
                                f"> **`{r['nickname']}`**  · {tier_display(r['tier'])} #{r['rank']:,}"
                            )
                        else:
                            team_lines.append(
                                f"> **`{r['nickname']}`**  · {tier_display(r['tier'])}"
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
                    f"총 {len(all_ocr)}명 | 팀 {len(ocr_teams)}개 "
                    f"| 조회 성공 {_ok}명 | 비공개 {hidden_count}명"
                )
            )
            return embed, _ok, _fail

        embed, ok_count, fail_names = build_embed(team_results)
        await msg.edit(content="", embed=embed)
        print(f"[1차 완료] 팀={len(ocr_teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")

        # ════════════════════════════════════════════
        # 0단계: 하이픈 변형 시도 (Gemini 없음)
        # ════════════════════════════════════════════
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
                    print(f"[하이픈 변형] {old_name!r} → {candidates}")
                    resolved   = None
                    for candidate in candidates:
                        new_data = await self.get_user_data(session, candidate)
                        if new_data["tier"] is not None:
                            new_data["nickname"] = candidate
                            new_data["box"]      = r["box"]
                            resolved = new_data
                            print(f"[하이픈 성공] {old_name!r} → {candidate!r}, tier={new_data['tier']}")
                            break
                    if resolved:
                        team_results[ti][pi] = resolved
                        any_hyphen_updated   = True
                    else:
                        print(f"[하이픈 전부 실패] {old_name!r}")

            if any_hyphen_updated:
                embed, ok_count, fail_names = build_embed(team_results)
                await msg.edit(embed=embed)

        # ════════════════════════════════════════════
        # 1단계: 동적 크롭 재질의 (box 있는 실패 닉네임)
        # ════════════════════════════════════════════
        crop_targets = [
            (ti, pi, r)
            for ti, team_data in enumerate(team_results)
            for pi, r in enumerate(team_data)
            if not r["hidden"] and r["tier"] is None and r.get("box") is not None
        ]

        if crop_targets:
            print(f"[동적 크롭 재질의] {len(crop_targets)}명 대상")
            tried_crop: dict[str, set[str]] = {}
            any_crop_updated = False

            async with aiohttp.ClientSession() as session:
                for ti, pi, r in crop_targets:
                    old_name = r["nickname"]
                    box      = r["box"]
                    tried    = tried_crop.setdefault(old_name, set())

                    # Gemini 크롭 재질의 → 후보 목록
                    crop_candidates: list[str] = await asyncio.to_thread(
                        self.recheck_with_crop, image_bytes, old_name, box
                    )
                    print(f"[크롭 재질의] {old_name!r} → 후보: {crop_candidates}")

                    # 새 후보만 필터
                    new_candidates = [
                        c for c in crop_candidates
                        if c not in tried and c != old_name
                    ]
                    tried.update(crop_candidates)

                    # 하이픈 변형도 자동으로 추가
                    for c in list(new_candidates):
                        for hv in _hyphen_variants(c):
                            if hv not in tried:
                                new_candidates.append(hv)
                                tried.add(hv)

                    if not new_candidates:
                        print(f"[크롭 재질의] {old_name!r}: 새 후보 없음")
                        continue

                    resolved = None
                    for candidate in new_candidates:
                        new_data = await self.get_user_data(session, candidate)
                        if new_data["tier"] is not None:
                            new_data["nickname"] = candidate
                            new_data["box"]      = box
                            resolved = new_data
                            print(f"[크롭 성공] {old_name!r} → {candidate!r}, tier={new_data['tier']}")
                            break

                    if resolved:
                        team_results[ti][pi] = resolved
                        any_crop_updated     = True
                    else:
                        print(f"[크롭 재질의] {old_name!r}: 모든 후보 실패")

            if any_crop_updated:
                embed, ok_count, fail_names = build_embed(team_results)
                await msg.edit(embed=embed)

        # ════════════════════════════════════════════
        # 2단계: 전체 이미지 Gemini 재질의 (폴백)
        #   box가 없거나 크롭 후에도 남은 실패자 대상
        # ════════════════════════════════════════════
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
            print(f"[전체이미지 재시도 {recheck_round}] 실패 닉네임: {failed_names_list}")

            corrections: dict[str, list[str]] = await asyncio.to_thread(
                self.recheck_failed_nicknames, image_bytes, failed_names_list
            )
            print(f"[전체이미지 재시도 {recheck_round}] 수정안: {corrections}")

            any_updated       = False
            any_new_candidate = False

            async with aiohttp.ClientSession() as session:
                for ti, pi, r in failed_entries:
                    old_name     = r["nickname"]
                    box          = r.get("box")
                    gemini_names = corrections.get(old_name, [old_name])
                    tried        = tried_candidates.setdefault(old_name, set())

                    new_candidates = [
                        gn for gn in gemini_names
                        if gn != old_name and gn not in tried
                    ]
                    if not new_candidates:
                        print(f"[전체이미지 {recheck_round}] {old_name!r}: 새 후보 없음, 스킵")
                        continue

                    any_new_candidate = True
                    tried.update(new_candidates)
                    print(f"[전체이미지 {recheck_round}] {old_name!r} 후보: {new_candidates}")

                    resolved = None
                    for candidate in new_candidates:
                        new_data = await self.get_user_data(session, candidate)
                        if new_data["tier"] is not None:
                            new_data["nickname"] = candidate
                            new_data["box"]      = box
                            resolved = new_data
                            print(f"[전체이미지 성공] {old_name!r} → {candidate!r}, tier={new_data['tier']}")
                            break

                    if resolved:
                        team_results[ti][pi] = resolved
                        any_updated          = True
                    else:
                        print(f"[전체이미지 {recheck_round}] {old_name!r}: 모든 후보 실패")

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
        print(f"[최종] 팀={len(ocr_teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))