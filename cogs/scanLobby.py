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
CURRENT_SEASON = 37  # ⚠️ 현재 시즌 ID로 교체 필요


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

    # ---------------- Gemini OCR ----------------
    def extract_names_from_image(self, image_bytes: bytes) -> list[str]:
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "플레이어 닉네임만 줄바꿈으로 출력.\n"
            "설명 절대 금지."
        )

        # ✅ Fix 1: raw bytes → Base64 인코딩 문자열로 변환
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # ✅ Fix 2: contents 구조를 올바른 parts 형식으로 수정
        res = self.gemini.models.generate_content(
            model="models/gemini-3-flash-preview",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=prompt),
                        types.Part(
                            inline_data=types.Blob(
                                mime_type="image/png",
                                data=image_b64
                            )
                        )
                    ]
                )
            ]
        )

        text = res.text.strip()
        names = [n.strip() for n in text.split("\n") if len(n.strip()) > 1]
        return names

    # ---------------- ER API ----------------
    async def get_user_data(self, session, nickname):
        headers = {"x-api-key": ER_KEY}

        # 1️⃣ 닉네임 → userNum
        await self.rl.wait()
        async with session.get(
            f"{ER_BASE}/user/nickname",
            headers=headers,
            params={"query": nickname}
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()

        user_num = data.get("user", {}).get("userNum")
        if not user_num:
            return None

        # 2️⃣ rank 조회
        # ✅ Fix 3: 올바른 엔드포인트 /rank/{userNum}/{seasonId}/{matchingTeamMode}
        #    matchingTeamMode: 1=솔로, 2=듀오, 3=스쿼드
        await self.rl.wait()
        async with session.get(
            f"{ER_BASE}/rank/{user_num}/{CURRENT_SEASON}/3",
            headers=headers
        ) as r:
            if r.status != 200:
                return None
            rank_data = await r.json()

        tier = rank_data.get("userRank", {}).get("tier", "Unranked")
        lp = rank_data.get("userRank", {}).get("mmr", 0)
        return {"nickname": nickname, "tier": tier, "lp": lp}

    # ---------------- Command ----------------
    @commands.command(name="대기분석")
    async def lobby_scan(self, ctx):
        if not ctx.message.attachments:
            await ctx.send("이미지 첨부 필요")
            return

        attachment = ctx.message.attachments[0]
        image_bytes = await attachment.read()

        msg = await ctx.send("🔍 분석중...")

        # Gemini OCR (blocking → thread)
        names = await asyncio.to_thread(
            self.extract_names_from_image,
            image_bytes
        )

        if not names:
            await msg.edit(content="닉 추출 실패")
            return

        await msg.edit(content=f"🔍 {len(names)}명 인식 완료, 전적 조회중...")

        results = []
        async with aiohttp.ClientSession() as session:
            for name in names:
                data = await self.get_user_data(session, name)
                if data:
                    results.append(data)

        if not results:
            await msg.edit(content="유저 정보 조회 실패")
            return

        embed = discord.Embed(
            title="📊 대기창 분석",
            color=discord.Color.blue()
        )

        for r in results:
            embed.add_field(
                name=r["nickname"],
                value=f"티어: {r['tier']} | LP: {r['lp']}",
                inline=False
            )

        embed.set_footer(text=f"총 {len(results)}명 조회 완료")
        await msg.edit(content="", embed=embed)


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))