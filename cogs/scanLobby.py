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

ER_BASE = "https://open-api.bser.io/v1"
CURRENT_SEASON = 37   # 시즌 10
MATCH_MODE     = 3    # 3=스쿼드(랭크)

# 비공개 닉네임 패턴: "실험체1", "실험체12" 등
HIDDEN_NAME_RE = re.compile(r"^실험체\d+$")

RANK_CACHE_TTL = 3600  # 랭크 캐시 유지 시간 (초)
MAX_RETRY_429  = 3     # 429 최대 재시도 횟수


# ────────────────────────────────────────────
# 티어 계산 (user_rank.py 동일 로직)
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
    else:  # season 10+
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
    "이터니티": 1475211106841661510,
    "데미갓": 1475211026180997414,
    "미스릴": 1475210930643013674,
    "메테오라이트": 1475210893376880680,
    "다이아몬드":  1475210845943496886,
    "플레티넘":    1475210794273603614,
    "골드":        1475210757623775274,
    "실버":        1475210565831098690,
    "브론즈":      1475210549792080046,
    "아이언":      1475210532611948698,
    "Unranked":    1475210494607491215,
}

def tier_display(bot, tier: str) -> str:
    emoji_id = TIER_EMOJI.get(tier)
    if not emoji_id:
        return tier

    emoji = bot.get_emoji(emoji_id)

    if emoji:
        return f"{emoji} {tier}"
    else:
        return tier  # 못 불러오면 텍스트만


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

    # ── Gemini OCR (팀 구분) ──────────────────
    def extract_teams_from_image(self, image_bytes: bytes) -> list[list[str]]:
        """
        이미지에서 팀별 닉네임 목록을 추출한다.
        반환값: [[팀1닉1, 팀1닉2, ...], [팀2닉1, ...], ...]
        팀 구분이 불가능하면 전체를 하나의 팀으로 묶어 반환.
        """
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "화면에 보이는 팀을 구분하여 각 팀의 플레이어 닉네임을 출력하라.\n"
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
            print(f"[캐시 HIT] userId: {nickname!r} → {self._userid_cache[nickname]}")
            return self._userid_cache[nickname]

        headers = {"x-api-key": ER_KEY}
        for attempt in range(1, MAX_RETRY_429 + 1):
            await self.rl.wait()
            async with session.get(
                f"{ER_BASE}/user/nickname",
                headers=headers,
                params={"query": nickname}
            ) as r:
                body = await r.json()
                print(f"[닉네임 조회] {nickname!r} → status={r.status}, body={body}")
                if r.status == 429:
                    print(f"  └─ 429 Too Many Requests, {attempt}/{MAX_RETRY_429} 재시도 대기 1초...")
                    await asyncio.sleep(1)
                    continue
                if r.status != 200:
                    return None
                user_id = body.get("user", {}).get("userId")
                if user_id:
                    self._userid_cache[nickname] = user_id
                return user_id

        print(f"[FAIL] {nickname!r}: 429 재시도 초과")
        return None

    async def get_rank(self, session: aiohttp.ClientSession, user_id: str) -> dict | None:
        cached = self._get_rank_cache(user_id)
        if cached is not None:
            print(f"[캐시 HIT] rank: userId={user_id}")
            return cached

        headers = {"x-api-key": ER_KEY}
        for attempt in range(1, MAX_RETRY_429 + 1):
            await self.rl.wait()
            async with session.get(
                f"{ER_BASE}/rank/uid/{user_id}/{CURRENT_SEASON}/{MATCH_MODE}",
                headers=headers
            ) as r:
                body = await r.json()
                print(f"[랭크 조회] userId={user_id} → status={r.status}, body={body}")
                if r.status == 429:
                    print(f"  └─ 429 Too Many Requests, {attempt}/{MAX_RETRY_429} 재시도 대기 1초...")
                    await asyncio.sleep(1)
                    continue
                if r.status != 200:
                    return None
                user_rank = body.get("userRank")
                if user_rank:
                    self._set_rank_cache(user_id, user_rank)
                return user_rank

        print(f"[FAIL] rank userId={user_id}: 429 재시도 초과")
        return None

    async def get_user_data(self, session: aiohttp.ClientSession, nickname: str) -> dict:
        """항상 dict 반환. 비공개/언랭/실패 모두 포함."""
        if HIDDEN_NAME_RE.match(nickname):
            print(f"[비공개] {nickname!r}")
            return {"nickname": nickname, "tier": None, "mmr": None, "rank": None, "hidden": True}

        user_id = await self.get_user_id(session, nickname)
        if not user_id:
            print(f"[FAIL] {nickname!r}: userId 없음")
            return {"nickname": nickname, "tier": None, "mmr": None, "rank": None, "hidden": False}

        rank_data = await self.get_rank(session, user_id)
        if not rank_data or not rank_data.get("rank"):
            print(f"[언랭] {nickname!r}")
            return {"nickname": nickname, "tier": "Unranked", "mmr": 0, "rank": None, "hidden": False}

        mmr  = rank_data.get("mmr", 0)
        rank = rank_data.get("rank", 0)
        snum = _season_num(CURRENT_SEASON)
        tier = _calc_tier(mmr, rank, snum)

        print(f"[OK] {nickname!r} → tier={tier}, mmr={mmr}, rank={rank}, season_num={snum}")
        return {"nickname": nickname, "tier": tier, "mmr": mmr, "rank": rank, "hidden": False}

    # ── Command ─────────────────────────────────
    @commands.command(name="대기분석")
    async def lobby_scan(self, ctx):
        if not ctx.message.attachments:
            await ctx.send("이미지 첨부 필요 <:08:1475208526694449338>")
            return

        image_bytes = await ctx.message.attachments[0].read()
        msg = await ctx.send("🔍 이미지 분석중...")
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
            f"⏳ 전적 조회중... (0 / {need_api})"
        ))
        print(f"[인식] 총={len(all_names)}, 팀={len(teams)}, 비공개={hidden_count}, API 필요={need_api}")

        # ── ER API 순차 조회 ──
        # teams 구조를 유지하며 result도 팀별로 수집
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
                            f"⏳ 전적 조회중... ({api_done} / {need_api}) — `{name}`"
                        ))
                    tr.append(await self.get_user_data(session, name))
                team_results.append(tr)

        # ── 결과 임베드 (팀별) ──
        embed = discord.Embed(
            title="📊 대기창 분석 결과",
            description="시즌 10 | 스쿼드 랭크",
            color=discord.Color.blue()
        )

        ok_count   = 0
        fail_names = []

        for team_idx, team_data in enumerate(team_results, 1):
            team_lines = []
            for r in team_data:
                if r["hidden"]:
                    team_lines.append("🔒 닉네임 비공개")
                elif r["tier"] is None:
                    fail_names.append(r["nickname"])
                    team_lines.append(f"~~{r['nickname']}~~ ⚠️ 조회 실패")
                elif r["tier"] == "Unranked":
                    team_lines.append(f"**{r['nickname']}** — {tier_display(self.bot, 'Unranked')}")
                    ok_count += 1
                else:
                    team_lines.append(
                        f"**{r['nickname']}** — {tier_display(self.bot, r['tier'])} | {r['mmr']:,} RP | {r['rank']:,}위"
                    )
                    ok_count += 1

            embed.add_field(
                name=f"**팀 {team_idx}**",
                value="\n".join(team_lines) if team_lines else "—",
                inline=False
            )

        if fail_names:
            embed.add_field(
                name="⚠️ 조회 실패 목록 (오인식 가능성)",
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
    """
    Gemini가 반환한 팀 구분 텍스트를 파싱하여 list[list[str]] 로 변환.

    기대 형식:
        팀1
        닉네임A
        닉네임B

        팀2
        닉네임C
        ...

    '팀N' 헤더가 없으면 전체를 하나의 팀으로 묶는다.
    """
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
            if len(line) > 1:
                current.append(line)

    if current:
        teams.append(current)

    # 팀 헤더가 전혀 없으면 전체를 하나의 팀으로
    if not teams:
        names = [l.strip() for l in text.splitlines() if len(l.strip()) > 1]
        if names:
            teams = [names]

    return teams


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))