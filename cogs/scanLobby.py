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

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        res = self.gemini.models.generate_content(
            model="models/gemini-2.0-flash",
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

        # ✅ Fix: thought_signature 등 non-text part 무시, text만 합산
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
            return body.get("user", {}).get("userNum")

    async def get_rank(self, session, user_num: int, mode: int = 3) -> dict | None:
        """matchingTeamMode: 1=솔로, 2=듀오, 3=스쿼드"""
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
            return body.get("userRank")

    async def get_user_data(self, session, nickname: str) -> dict | None:
        user_num = await self.get_user_num(session, nickname)
        if not user_num:
            print(f"[SKIP] {nickname!r}: userNum 없음")
            return None

        user_rank = await self.get_rank(session, user_num, mode=1)

        # 랭크 없어도 닉네임은 표시 (언랭 처리)
        if not user_rank:
            print(f"[언랭] {nickname!r}: 랭크 데이터 없음 → Unranked 처리")
            return {"nickname": nickname, "tier": "Unranked", "lp": "-"}

        tier = user_rank.get("tier", "Unranked")
        lp   = user_rank.get("mmr", 0)
        print(f"[OK] {nickname!r} → tier={tier}, lp={lp}")
        return {"nickname": nickname, "tier": tier, "lp": lp}

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

        names_preview = "\n".join(f"• {n}" for n in names)
        await msg.edit(content=(
            f"✅ **{len(names)}명** 인식 완료\n"
            f"```\n{names_preview}\n```\n"
            f"⏳ 전적 조회중... (0 / {len(names)})"
        ))
        print(f"[인식된 닉네임 {len(names)}명] {names}")

        # ── ER API 순차 조회 ──
        results = []
        failed  = []

        async with aiohttp.ClientSession() as session:
            for i, name in enumerate(names, 1):
                await msg.edit(content=(
                    f"✅ **{len(names)}명** 인식 완료\n"
                    f"```\n{names_preview}\n```\n"
                    f"⏳ 전적 조회중... ({i} / {len(names)}) — `{name}`"
                ))
                data = await self.get_user_data(session, name)
                if data:
                    results.append(data)
                else:
                    failed.append(name)

        print(f"[완료] 성공={len(results)}, 실패={len(failed)}, 실패목록={failed}")

        # ── 결과 임베드 ──
        embed = discord.Embed(
            title="📊 대기창 분석 결과",
            color=discord.Color.blue()
        )

        for r in results:
            embed.add_field(
                name=r["nickname"],
                value=f"티어: **{r['tier']}** | LP: {r['lp']}",
                inline=False
            )

        if failed:
            embed.add_field(
                name="⚠️ 조회 실패 (닉네임 오인식 가능성)",
                value="\n".join(f"• {n}" for n in failed),
                inline=False
            )

        embed.set_footer(text=f"총 {len(names)}명 중 {len(results)}명 조회 성공 | 시즌 {CURRENT_SEASON}")
        await msg.edit(content="", embed=embed)


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))