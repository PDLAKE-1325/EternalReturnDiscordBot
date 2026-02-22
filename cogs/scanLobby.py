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
CURRENT_SEASON = 27  # ⚠️ 현재 시즌 ID로 교체 필요
RANK_CACHE_TTL = 300  # 랭크 캐시 유지 시간 (초), 기본 5분

# 비공개 닉네임 패턴: "실험체1", "실험체12" 등
HIDDEN_NAME_RE = re.compile(r"^실험체\d+$")


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


class LobbyScan(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini = genai.Client(api_key=AI_KEY)
        self.rl = RateLimiter(rate_per_sec=1)

        # ── 캐시 ──────────────────────────────────────────
        # { nickname: userNum }  — 닉네임은 잘 안 바뀌므로 영구 캐시
        self._usernum_cache: dict[str, int] = {}

        # { userNum: (result_dict, cached_at) }  — TTL 5분
        self._rank_cache: dict[int, tuple[dict, float]] = {}

    # ---------------- 캐시 헬퍼 ----------------
    def _get_rank_cache(self, user_num: int) -> dict | None:
        entry = self._rank_cache.get(user_num)
        if entry and (time.monotonic() - entry[1]) < RANK_CACHE_TTL:
            return entry[0]
        return None

    def _set_rank_cache(self, user_num: int, data: dict):
        self._rank_cache[user_num] = (data, time.monotonic())

    # ---------------- Gemini OCR ----------------
    def extract_names_from_image(self, image_bytes: bytes) -> list[str]:
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "플레이어 닉네임만 줄바꿈으로 출력.\n"
            "설명 절대 금지."
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

        # thought_signature 등 non-text part 무시
        text = ""
        for part in res.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text += part.text

        text = text.strip()
        print(f"[OCR 원본 응답]\n{text}\n{'-'*30}")

        names = [n.strip() for n in text.split("\n") if len(n.strip()) > 1]
        return names

    # ---------------- ER API ----------------
    async def get_user_num(self, session, nickname: str) -> int | None:
        # 캐시 히트
        if nickname in self._usernum_cache:
            print(f"[캐시 HIT] userNum: {nickname!r} → {self._usernum_cache[nickname]}")
            return self._usernum_cache[nickname]

        headers = {"x-api-key": ER_KEY}
        await self.rl.wait()
        async with session.get(
            f"{ER_BASE}/user/nickname",
            headers=headers,
            params={"query": nickname}
        ) as r:
            body = await r.json()
            print(f"[닉네임 조회] {nickname!r} → status={r.status}, body={body}")
            if r.status != 200:
                return None
            user_num = body.get("user", {}).get("userId")
            if user_num:
                self._usernum_cache[nickname] = user_num
            return user_num

    async def get_rank(self, session, user_num: int, mode: int = 1) -> dict | None:
        # 캐시 히트
        cached = self._get_rank_cache(user_num)
        if cached is not None:
            print(f"[캐시 HIT] rank: userNum={user_num}")
            return cached

        headers = {"x-api-key": ER_KEY}
        await self.rl.wait()
        async with session.get(
            f"{ER_BASE}/rank/{user_num}/{CURRENT_SEASON}/{mode}",
            headers=headers
        ) as r:
            body = await r.json()
            print(f"[랭크 조회] userNum={user_num}, mode={mode} → status={r.status}, body={body}")
            if r.status != 200:
                return None
            user_rank = body.get("userRank")
            if user_rank is not None:
                self._set_rank_cache(user_num, user_rank)
            return user_rank

    async def get_user_data(self, session, nickname: str) -> dict:
        """항상 dict 반환. 실패/비공개도 포함."""

        # ── 비공개 닉네임 처리 ──
        if HIDDEN_NAME_RE.match(nickname):
            print(f"[비공개] {nickname!r} → 이름 숨김 처리")
            return {"nickname": nickname, "tier": None, "lp": None, "hidden": True}

        user_num = await self.get_user_num(session, nickname)
        if not user_num:
            print(f"[FAIL] {nickname!r}: userId 없음")
            return {"nickname": nickname, "tier": None, "lp": None, "hidden": False}

        user_rank = await self.get_rank(session, user_num, mode=1)

        if not user_rank:
            print(f"[언랭] {nickname!r}")
            return {"nickname": nickname, "tier": "Unranked", "lp": "-", "hidden": False}

        tier = user_rank.get("tier", "Unranked")
        lp   = user_rank.get("mmr", 0)
        print(f"[OK] {nickname!r} → tier={tier}, lp={lp}")
        return {"nickname": nickname, "tier": tier, "lp": lp, "hidden": False}

    # ---------------- Command ----------------
    @commands.command(name="대기분석")
    async def lobby_scan(self, ctx):
        if not ctx.message.attachments:
            await ctx.send("이미지 첨부 필요")
            return

        attachment = ctx.message.attachments[0]
        image_bytes = await attachment.read()

        msg = await ctx.send("🔍 이미지 분석중...")
        print(f"\n{'='*40}\n[대기분석 시작] by {ctx.author}\n{'='*40}")

        # ── Gemini OCR ──
        names = await asyncio.to_thread(self.extract_names_from_image, image_bytes)

        if not names:
            await msg.edit(content="❌ 닉네임 인식 실패 (이미지를 확인해주세요)")
            return

        # 비공개/일반 분류 → 실제 API 필요한 인원 수 미리 계산
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
        results = []
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
                data = await self.get_user_data(session, name)
                results.append(data)

        # ── 결과 임베드 ──
        embed = discord.Embed(title="📊 대기창 분석 결과", color=discord.Color.blue())

        ok_count   = 0
        fail_names = []

        for r in results:
            if r["hidden"]:
                embed.add_field(name=r["nickname"], value="🔒 닉네임 비공개", inline=False)
            elif r["tier"] is None:
                fail_names.append(r["nickname"])
            else:
                ok_count += 1
                embed.add_field(
                    name=r["nickname"],
                    value=f"티어: **{r['tier']}** | LP: {r['lp']}",
                    inline=False
                )

        if fail_names:
            embed.add_field(
                name="⚠️ 조회 실패 (오인식 가능성)",
                value="\n".join(f"• {n}" for n in fail_names),
                inline=False
            )

        embed.set_footer(
            text=f"총 {len(names)}명 | 조회 성공 {ok_count}명 | 비공개 {hidden_count}명 | 시즌 {CURRENT_SEASON}"
        )
        await msg.edit(content="", embed=embed)
        print(f"[완료] 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))