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
MATCH_MODE     = 3    # 1=솔로, 2=듀오, 3=스쿼드(랭크)

# 비공개 닉네임 패턴: "실험체1", "실험체12" 등
HIDDEN_NAME_RE = re.compile(r"^실험체\d+$")

RANK_CACHE_TTL = 300  # 랭크 캐시 유지 시간 (초)


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
    "이터니티":    "👑",
    "데미갓":      "💜",
    "미스릴":      "🩵",
    "메테오라이트": "🔮",
    "다이아몬드":  "💎",
    "플레티넘":    "🩶",
    "골드":        "🥇",
    "실버":        "🥈",
    "브론즈":      "🥉",
    "아이언":      "⚫",
    "Unranked":    "❓",
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

    # ── Gemini OCR ──────────────────────────────
    def extract_names_from_image(self, image_bytes: bytes) -> list[str]:
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "플레이어 닉네임만 줄바꿈으로 출력.\n"
            "설명 절대 금지."
        )
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        res = self.gemini.models.generate_content(
            model="models/gemini-3-preview",
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
        # thought_signature 등 non-text part 무시
        text = "".join(
            part.text for part in res.candidates[0].content.parts
            if hasattr(part, "text") and part.text
        ).strip()

        print(f"[OCR 원본 응답]\n{text}\n{'-'*30}")
        return [n.strip() for n in text.split("\n") if len(n.strip()) > 1]

    # ── ER API ──────────────────────────────────
    async def _get(self, session: aiohttp.ClientSession, url: str, **kwargs) -> tuple[int, dict]:
        """GET 요청 + 429시 1초 뒤 1회 재시도"""
        headers = {"x-api-key": ER_KEY}
        for attempt in range(2):
            await self.rl.wait()
            async with session.get(url, headers=headers, **kwargs) as r:
                body = await r.json()
                if r.status == 429:
                    print(f"[429] {url} → 1초 후 재시도 (attempt {attempt+1})")
                    await asyncio.sleep(1.0)
                    continue
                return r.status, body
        return 429, {}

    async def get_user_id(self, session: aiohttp.ClientSession, nickname: str) -> str | None:
        if nickname in self._userid_cache:
            print(f"[캐시 HIT] userId: {nickname!r} → {self._userid_cache[nickname]}")
            return self._userid_cache[nickname]

        status, body = await self._get(
            session,
            f"{ER_BASE}/user/nickname",
            params={"query": nickname}
        )
        print(f"[닉네임 조회] {nickname!r} → status={status}, body={body}")
        if status != 200:
            return None
        user_id = body.get("user", {}).get("userId")
        if user_id:
            self._userid_cache[nickname] = user_id
        return user_id

    async def get_rank(self, session: aiohttp.ClientSession, user_id: str) -> dict | None:
        cached = self._get_rank_cache(user_id)
        if cached is not None:
            print(f"[캐시 HIT] rank: userId={user_id}")
            return cached

        status, body = await self._get(
            session,
            f"{ER_BASE}/rank/uid/{user_id}/{CURRENT_SEASON}/{MATCH_MODE}"
        )
        print(f"[랭크 조회] userId={user_id} → status={status}, body={body}")
        if status != 200:
            return None
        user_rank = body.get("userRank")
        if user_rank:
            self._set_rank_cache(user_id, user_rank)
        return user_rank

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
            await ctx.send("이미지 첨부 필요")
            return

        image_bytes = await ctx.message.attachments[0].read()
        msg = await ctx.send("🔍 이미지 분석중...")
        print(f"\n{'='*40}\n[대기분석 시작] by {ctx.author}\n{'='*40}")

        # ── Gemini OCR ──
        names = await asyncio.to_thread(self.extract_names_from_image, image_bytes)
        if not names:
            await msg.edit(content="❌ 닉네임 인식 실패 (이미지를 확인해주세요)")
            return

        hidden_count = sum(1 for n in names if HIDDEN_NAME_RE.match(n))
        need_api     = len(names) - hidden_count

        names_preview = "\n".join(
            f"• {n}  🔒" if HIDDEN_NAME_RE.match(n) else f"• {n}"
            for n in names
        )
        await msg.edit(content=(
            f"✅ **{len(names)}명** 인식 완료 (비공개 {hidden_count}명)\n"
            f"```\n{names_preview}\n```\n"
            f"⏳ 전적 조회중... (0 / {need_api})"
        ))
        print(f"[인식] 총={len(names)}, 비공개={hidden_count}, API 필요={need_api}")

        # ── ER API 순차 조회 ──
        results  = []
        api_done = 0

        async with aiohttp.ClientSession() as session:
            for name in names:
                if not HIDDEN_NAME_RE.match(name):
                    api_done += 1
                    await msg.edit(content=(
                        f"✅ **{len(names)}명** 인식 완료 (비공개 {hidden_count}명)\n"
                        f"```\n{names_preview}\n```\n"
                        f"⏳ 전적 조회중... ({api_done} / {need_api}) — `{name}`"
                    ))
                results.append(await self.get_user_data(session, name))

        # ── 결과 임베드 ──
        embed = discord.Embed(
            title=f"📊 대기창 분석 결과",
            description=f"시즌 10 | 스쿼드 랭크",
            color=discord.Color.blue()
        )

        ok_count   = 0
        fail_names = []

        for r in results:
            if r["hidden"]:
                embed.add_field(name=r["nickname"], value="🔒 닉네임 비공개", inline=False)
            elif r["tier"] is None:
                fail_names.append(r["nickname"])
            elif r["tier"] == "Unranked":
                embed.add_field(
                    name=r["nickname"],
                    value=tier_display("Unranked"),
                    inline=False
                )
                ok_count += 1
            else:
                embed.add_field(
                    name=r["nickname"],
                    value=f"{tier_display(r['tier'])} | {r['mmr']:,} RP | {r['rank']:,}위",
                    inline=False
                )
                ok_count += 1

        if fail_names:
            embed.add_field(
                name="⚠️ 조회 실패 (오인식 가능성)",
                value="\n".join(f"• {n}" for n in fail_names),
                inline=False
            )

        embed.set_footer(
            text=f"총 {len(names)}명 | 조회 성공 {ok_count}명 | 비공개 {hidden_count}명"
        )
        await msg.edit(content="", embed=embed)
        print(f"[완료] 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))