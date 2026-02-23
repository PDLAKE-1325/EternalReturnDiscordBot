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

# 비공개 닉네임 패턴: "실험체1", "실험체12" 등
HIDDEN_NAME_RE = re.compile(r"^실험체\d+$")

RANK_CACHE_TTL = 3600  # 랭크 캐시 유지 시간 (초)
MAX_RETRY      = 3     # 최대 재시도 횟수 (429 / 5xx 공통)


# ────────────────────────────────────────────
# 닉네임 정규화
# ────────────────────────────────────────────
CHAR_NORMALIZE = str.maketrans({
    # 하이픈 계열
    '\u2013': '-',   # en dash –
    '\u2014': '-',   # em dash —
    '\u2010': '-',   # hyphen ‐
    '\u2011': '-',   # non-breaking hyphen
    '\u00AD': '-',   # soft hyphen
    '\u30FC': '-',   # 가타카나 장음 ー
    '\uFF0D': '-',   # 전각 하이픈 －
    '\u2212': '-',   # minus sign −
    # 따옴표 계열
    '\u2018': "'",   # left single quote '
    '\u2019': "'",   # right single quote '
    '\uFF07': "'",   # 전각 apostrophe ＇
    '\u02BC': "'",   # modifier letter apostrophe ʼ
    # 공백 계열
    '\u3000': ' ',   # 전각 공백
    '\u00A0': ' ',   # non-breaking space
    '\u200B': '',    # zero-width space (제거)
    '\uFEFF': '',    # BOM (제거)
    # 점 계열
    '\uFF0E': '.',   # 전각 마침표 ．
    '\u3002': '.',   # 중국어 마침표 。
    # 밑줄 계열
    '\uFF3F': '_',   # 전각 밑줄 ＿
})

def normalize_nickname(name: str) -> str:
    """유니코드 유사 문자를 ASCII로 정규화하고 앞뒤 공백 제거."""
    return name.translate(CHAR_NORMALIZE).strip()


# ────────────────────────────────────────────
# 조건부 이미지 전처리
# ────────────────────────────────────────────
def _preprocess_lobby_image(image_bytes: bytes) -> bytes:
    """
    저화질(720p 이하) 또는 JPEG 압축 이미지일 때만 대비/샤프닝 적용.
    - 크롭 없음: 레이아웃이 다양하므로 고정 크롭은 위험
    - 업스케일 없음: Gemini 인식률 오히려 저하
    - 고화질 PNG는 원본 그대로 반환
    """
    from PIL import Image, ImageEnhance, ImageFilter
    import io as _io

    is_jpeg    = image_bytes[:2] == b'\xff\xd8'
    img        = Image.open(_io.BytesIO(image_bytes))
    is_low_res = (img.width * img.height) <= (1280 * 720)

    if not (is_jpeg or is_low_res):
        return image_bytes  # 고화질 PNG → 원본 그대로

    img = img.convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.4)
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)

    out = _io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    reason = "JPEG" if is_jpeg else f"저해상도 {img.width}x{img.height}"
    print(f"[전처리] {reason} 감지 → 대비/샤프닝 적용")
    return out.getvalue()


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

        # 캐시: { nickname: userId }  영구
        self._userid_cache: dict[str, str] = {}
        # 캐시: { userId: (rank_data, cached_at) }  TTL
        self._rank_cache: dict[str, tuple[dict, float]] = {}

    # ── 캐시 헬퍼 ──────────────────────────────
    def _get_rank_cache(self, user_id: str) -> dict | None:
        entry = self._rank_cache.get(user_id)
        if entry and (time.monotonic() - entry[1]) < RANK_CACHE_TTL:
            return entry[0]
        return None

    def _set_rank_cache(self, user_id: str, data: dict):
        self._rank_cache[user_id] = (data, time.monotonic())

    # ── Gemini OCR ───────────────────────────
    def extract_teams_from_image(self, image_bytes: bytes) -> list[list[str]]:
        """이미지에서 팀별 닉네임 목록을 추출한다."""
        image_bytes = _preprocess_lobby_image(image_bytes)
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "화면에 보이는 팀을 구분하여 각 팀의 플레이어 닉네임을 정확히 출력하라.\n"
            "\n"
            "출력 형식(예시, 팀 수·인원 수는 실제에 맞게):\n"
            "팀1\n"
            "닉네임A\n"
            "닉네임B\n"
            "닉네임C\n"
            "\n"
            "팀2\n"
            "닉네임D\n"
            "닉네임E\n"
            "닉네임F\n"
            "\n"
            "규칙:\n"
            "- '팀N' 헤더 다음 줄부터 해당 팀 닉네임을 한 줄에 하나씩 나열.\n"
            "- 팀 사이에 반드시 빈 줄 하나.\n"
            "- 닉네임에 포함된 특수문자(-_.'!?)를 절대 변경하거나 생략하지 말 것.\n"
            "  예) 'Jung-in' → 'Jung-in' 그대로, 'Player.exe' → 'Player.exe' 그대로.\n"
            "- 한글·영문·숫자·특수문자 모두 화면에 보이는 그대로 출력.\n"
            "- 닉네임 외 설명·번호·기호 절대 금지.\n"
            "- 팀 구분이 불가능하면 '팀1' 하나로 전부 묶어 출력."
        )
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
        text = "".join(
            part.text for part in res.candidates[0].content.parts
            if hasattr(part, "text") and part.text
        ).strip()

        print(f"[OCR 원본 응답]\n{text}\n{'-'*30}")
        return _parse_teams(text)

    # ── ER API ──────────────────────────────────
    async def get_user_id(self, session: aiohttp.ClientSession, nickname: str) -> str | None:
        if nickname in self._userid_cache:
            return self._userid_cache[nickname]

        headers = {"x-api-key": ER_KEY}
        for attempt in range(1, MAX_RETRY + 1):
            await self.rl.wait()
            async with session.get(
                f"{ER_BASE}/user/nickname",
                headers=headers,
                params={"query": nickname}
            ) as r:
                body = await r.json()
                if r.status in (429, 500, 502, 503):
                    await asyncio.sleep(attempt)
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
        for attempt in range(1, MAX_RETRY + 1):
            await self.rl.wait()
            async with session.get(
                f"{ER_BASE}/rank/uid/{user_id}/{CURRENT_SEASON_NUM}/{MATCH_MODE}",
                headers=headers
            ) as r:
                body = await r.json()
                if r.status in (429, 500, 502, 503):
                    await asyncio.sleep(attempt)
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

        # 1차: 원본 닉네임
        user_id = await self.get_user_id(session, nickname)

        # 2차: 정규화 폴백 (유니코드 특수문자 치환 후 다를 때만)
        if not user_id:
            normalized = normalize_nickname(nickname)
            if normalized != nickname:
                print(f"[폴백] {nickname!r} → {normalized!r}")
                user_id = await self.get_user_id(session, normalized)
                if user_id:
                    self._userid_cache[nickname] = user_id

        if not user_id:
            print(f"[FAIL] {nickname!r}: userId 없음")
            return {"nickname": nickname, "tier": None, "mmr": None, "rank": None, "hidden": False}

        rank_data = await self.get_rank(session, user_id)
        if not rank_data or not rank_data.get("rank"):
            return {"nickname": nickname, "tier": "Unranked", "mmr": 0, "rank": None, "hidden": False}

        mmr  = rank_data.get("mmr", 0)
        rank = rank_data.get("rank", 0)
        snum = _season_num(CURRENT_SEASON_NUM)
        tier = _calc_tier(mmr, rank, snum)

        print(f"[OK] {nickname!r} → tier={tier}, mmr={mmr}, rank={rank}")
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

        # ── Gemini OCR ──
        teams: list[list[str]] = await asyncio.to_thread(
            self.extract_teams_from_image, image_bytes
        )
        all_names = [name for team in teams for name in team]

        if not all_names:
            await msg.edit(content="❌ 닉네임 인식 실패 (이미지를 확인해주세요)")
            return

        hidden_count = sum(1 for n in all_names if HIDDEN_NAME_RE.match(n))
        need_api     = len(all_names) - hidden_count

        # 진행상황 미리보기 (팀별)
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

        # ── 결과 임베드 (팀별) ──
        embed = discord.Embed(
            title="📊 대기창 분석 결과",
            description=f"시즌 {CURRENT_SEASON} 랭크 정보",
            color=discord.Color.blue()
        )

        ok_count   = 0
        fail_names = []

        for team_idx, team_data in enumerate(team_results, 1):
            team_lines = []
            for r in team_data:
                if r["hidden"]:
                    team_lines.append("> 닉네임 비공개")
                elif r["tier"] is None:
                    fail_names.append(r["nickname"])
                    team_lines.append(f"> ~~{r['nickname']}~~ — 조회 실패")
                elif r["tier"] == "Unranked":
                    team_lines.append(f"> {r['nickname']} — {tier_display('Unranked')}")
                    ok_count += 1
                else:
                    if r['tier'] == "이터니티":
                        team_lines.append(
                            f"> {r['nickname']} — {tier_display(r['tier'])} #{r['rank']:,}"
                        )
                    else:
                        team_lines.append(
                            f"> {r['nickname']} — {tier_display(r['tier'])}"
                        )
                    ok_count += 1

            embed.add_field(
                name=f"**팀 {team_idx:02d}**",
                value="\n".join(team_lines) if team_lines else "—",
                inline=False
            )

        if fail_names:
            embed.add_field(
                name="𒄬 조회 실패 목록",
                value="\n".join(f"• {n}" for n in fail_names),
                inline=False
            )

        embed.set_footer(
            text=f"총 {len(all_names)}명 | 팀 {len(teams)}개 | 조회 성공 {ok_count}명 | 비공개 {hidden_count}명"
        )
        await msg.edit(content="", embed=embed)
        print(f"[완료] 팀={len(teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")


# ── OCR 응답 파서 ────────────────────────────
def _parse_teams(text: str) -> list[list[str]]:
    TEAM_HEADER_RE = re.compile(r"^팀\s*\d+$")
    teams: list[list[str]] = []
    current: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if TEAM_HEADER_RE.match(line):
            if current:
                teams.append(current)
            current = []
        else:
            cleaned = normalize_nickname(line)
            if len(cleaned) > 1:
                current.append(cleaned)

    if current:
        teams.append(current)

    if not teams:
        names = [normalize_nickname(l) for l in text.splitlines() if len(normalize_nickname(l)) > 1]
        if names:
            teams = [names]

    return teams


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))