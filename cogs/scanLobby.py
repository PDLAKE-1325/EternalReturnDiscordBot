# cogs/lobby_scan.py

import discord
from discord.ext import commands
import aiohttp
import asyncio
import re
from google import genai
from config import AI_KEY, ER_KEY

ER_BASE = "https://open-api.bser.io/v1"

class LobbyScan(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.gemini = genai.Client(api_key=AI_KEY)

    # ----------------------------
    # Gemini OCR
    # ----------------------------
    async def extract_names_from_image(self, image_bytes: bytes) -> list[str]:
        prompt = """
        이터널 리턴 대기창 스크린샷이다.
        플레이어 닉네임만 줄바꿈으로 정리해라.
        다른 설명 절대 하지마.
        """

        response = self.gemini.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[
                {"text": prompt},
                {"inline_data": {
                    "mime_type": "image/png",
                    "data": image_bytes
                }}
            ]
        )

        text = response.text.strip()
        names = [n.strip() for n in text.split("\n") if len(n.strip()) > 1]
        return names

    # ----------------------------
    # ER API 호출
    # ----------------------------
    async def get_user_data(self, session, nickname):
        headers = {"x-api-key": ER_KEY}

        # 유저 번호 조회
        async with session.get(
            f"{ER_BASE}/user/nickname",
            headers=headers,
            params={"query": nickname}
        ) as res:
            if res.status != 200:
                return None
            user_data = await res.json()

        user_num = user_data.get("user", {}).get("userNum")
        if not user_num:
            return None

        # 랭크 정보 조회
        async with session.get(
            f"{ER_BASE}/user/{user_num}/rank",
            headers=headers
        ) as res:
            if res.status != 200:
                return None
            rank_data = await res.json()

        tier = rank_data.get("rank", {}).get("tier", "Unranked")
        return {"nickname": nickname, "tier": tier}

    # ----------------------------
    # 명령어
    # ----------------------------
    @commands.command(name="대기분석")
    async def lobby_scan(self, ctx):
        if not ctx.message.attachments:
            await ctx.send("이미지 첨부 필요")
            return

        attachment = ctx.message.attachments[0]
        image_bytes = await attachment.read()

        await ctx.send("🔍 분석중...")

        # 1️⃣ Gemini OCR
        names = await asyncio.to_thread(
            self.extract_names_from_image,
            image_bytes
        )

        if not names:
            await ctx.send("닉네임 추출 실패")
            return

        # 2️⃣ 병렬 전적 조회
        async with aiohttp.ClientSession() as session:
            tasks = [
                self.get_user_data(session, name)
                for name in names
            ]
            results = await asyncio.gather(*tasks)

        results = [r for r in results if r]

        # 3️⃣ Embed 정리
        embed = discord.Embed(
            title="📊 대기창 분석 결과",
            color=discord.Color.blue()
        )

        for r in results:
            embed.add_field(
                name=r["nickname"],
                value=f"티어: {r['tier']}",
                inline=False
            )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))