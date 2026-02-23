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

ER_BASE = "https://open-api.bser.io/v1"
MATCH_MODE     = 3 
MAX_RECHECK   = 4

# 비공개 닉네임 패턴: "실험체1", "실험체12" 등
HIDDEN_NAME_RE = re.compile(r"^실험체\d+$")

RANK_CACHE_TTL = 3600  # 랭크 캐시 유지 시간 (초)
MAX_RETRY_429  = 3     # 429 최대 재시도 횟수

# ── 하이픈 변형 후보 ──────────────────────────
# OCR이 자주 혼동하는 하이픈류 문자 목록
HYPHEN_VARIANTS = [
    "\u2500",  # ─  (BOX DRAWINGS LIGHT HORIZONTAL)
    "-",       # -  (HYPHEN-MINUS, ASCII)
    "\u4e00",  # 一 (CJK 한자 일)
    "\u2013",  # –  (EN DASH)
    "\u2014",  # —  (EM DASH)
    "\u2212",  # −  (MINUS SIGN)
    "\uff0d",  #－ (FULLWIDTH HYPHEN-MINUS)
]

# OCR 혼동 문자 쌍 (단방향 — 인식값 → 실제 가능성)
OCR_CONFUSABLES = [
    ("0", "O"), ("O", "0"),
    ("1", "l"), ("l", "1"), ("1", "I"), ("I", "1"),
    ("rn", "m"), ("m", "rn"),
]

def _hyphen_variants(nickname: str) -> list[str]:
    """
    닉네임에 하이픈류 문자가 포함돼 있으면
    모든 HYPHEN_VARIANTS 로 교체한 후보 목록을 반환.
    원본과 동일한 후보는 제외.
    """
    # 닉네임 내에 하이픈류 문자가 하나라도 있는지 확인
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

def _ocr_variants(nickname: str) -> list[str]:
    """
    OCR 혼동 문자 쌍을 기반으로 1-depth 변형 후보 반환.
    """
    candidates = []
    for wrong, right in OCR_CONFUSABLES:
        if wrong in nickname:
            candidate = nickname.replace(wrong, right, 1)
            if candidate != nickname and candidate not in candidates:
                candidates.append(candidate)
    return candidates

def _all_variants(nickname: str) -> list[str]:
    """하이픈 변형 + OCR 변형을 합쳐 중복 제거 후 반환."""
    seen = set()
    result = []
    for c in _hyphen_variants(nickname) + _ocr_variants(nickname):
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


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
    "이터니티":    "<:Immortal:1475215908665299035>",
    "데미갓":      "<:Titan:1475215920313139261>",
    "미스릴":      "<:Mithril:1475215913778413609>",
    "메테오라이트": "<:Meteorite:1475215912083652760>",
    "다이아몬드":  "<:Diamond:1475215904789762169>",
    "플레티넘":    "<:Platinum:1475215916332482893>",
    "골드":        "<:Gold:1475215906635518012>",
    "실버":        "<:Silver:1475215918509326438>",
    "브론즈":      "<:Bronze:1475215903468556364>",
    "아이언":    "<:Iron:1475215910313656422>",
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

        # print(f"[OCR 원본 응답]\n{text}\n{'-'*30}")
        return _parse_teams(text)

    def recheck_failed_nicknames(self, image_bytes: bytes, failed_names: list[str]) -> dict[str, str]:
        """
        조회 실패한 닉네임 목록을 원본 이미지와 함께 Gemini에 재질의.
        잘못 읽혔을 가능성이 있으므로 올바른 닉네임을 다시 추출하게 한다.

        반환값: { 원래_닉네임: 수정된_닉네임 }
        (변경 없으면 원래 닉네임 그대로)
        """
        names_str = "\n".join(f"- {n}" for n in failed_names)
        prompt = (
            "이터널 리턴 대기창 스크린샷이다.\n"
            "아래 닉네임들은 OCR 인식 결과인데 게임 API 조회에 실패했다. "
            "이미지를 다시 보고 각 닉네임이 실제로 어떻게 적혀 있는지 정확히 읽어라.\n\n"
            f"실패 목록:\n{names_str}\n\n"
            "출력 형식 (변경 없으면 원래 그대로 출력):\n"
            "원래닉네임|수정된닉네임\n"
            "원래닉네임2|수정된닉네임2\n\n"
            "규칙:\n"
            "- 반드시 '|' 구분자 사용.\n"
            "- 한 줄에 하나씩.\n"
            "- 닉네임에 하이픈 모양 문자(─, -, 一, –, — 등)가 있다면 원본 그대로 출력.\n"
            "- 설명·번호·기호 절대 금지."
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

        corrections: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.split("|", 1)
            original, corrected = parts[0].strip(), parts[1].strip()
            if original and corrected:
                corrections[original] = corrected

        # 목록에 없는 닉네임은 그대로
        for n in failed_names:
            corrections.setdefault(n, n)

        return corrections

    # ── ER API ──────────────────────────────────
    async def get_user_id(self, session: aiohttp.ClientSession, nickname: str) -> str | None:
        if nickname in self._userid_cache:
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
        for attempt in range(1, MAX_RETRY_429 + 1):
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

    async def _try_variants(
        self,
        session: aiohttp.ClientSession,
        nickname: str,
    ) -> dict | None:
        """
        하이픈 변형 + OCR 변형 후보를 순서대로 시도.
        성공한 첫 번째 결과 반환. 모두 실패하면 None.
        """
        for candidate in _all_variants(nickname):
            data = await self.get_user_data(session, candidate)
            if data["tier"] is not None:
                print(f"[변형 성공] {nickname!r} → {candidate!r}, tier={data['tier']}")
                data["nickname"] = candidate
                return data
            # print(f"[변형 실패] {nickname!r} → {candidate!r}")
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

        # ── 1차 결과 임베드 즉시 표시 ──
        def build_embed(results: list[list[dict]]) -> tuple[discord.Embed, int, list[str]]:
            """임베드 생성. (embed, ok_count, fail_names) 반환"""
            embed = discord.Embed(
                title="📊 대기창 분석 결과",
                description=f"시즌 {CURRENT_SEASON} 랭크 정보",
                color=discord.Color.blue()
            )
            _ok = 0
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
                    inline=False
                )
            if _fail:
                embed.add_field(
                    name="𒄬 조회 실패 — 재시도 중...",
                    value="\n".join(f"• {n}" for n in _fail),
                    inline=False
                )
            embed.set_footer(
                text=f"총 {len(all_names)}명 | 팀 {len(teams)}개 | 조회 성공 {_ok}명 | 비공개 {hidden_count}명"
            )
            return embed, _ok, _fail

        embed, ok_count, fail_names = build_embed(team_results)
        await msg.edit(content="", embed=embed)
        print(f"[1차 완료] 팀={len(teams)}, 성공={ok_count}, 비공개={hidden_count}, 실패={len(fail_names)}")

        # ── 0단계: 하이픈/OCR 변형 전부 소진 (Gemini 호출 없음, 횟수 제한 없음) ──
        # MAX_RECHECK 카운트와 완전히 별개로, 변형 후보가 있는 닉네임은 무조건 여기서 다 시도한다.
        variant_targets = [
            (ti, pi, r)
            for ti, team_data in enumerate(team_results)
            for pi, r in enumerate(team_data)
            if not r["hidden"] and r["tier"] is None and _all_variants(r["nickname"])
        ]
        if variant_targets:
            print(f"[변형 시도] {len(variant_targets)}명 대상")
            any_variant_updated = False
            async with aiohttp.ClientSession() as session:
                for ti, pi, r in variant_targets:
                    old_name = r["nickname"]
                    candidates = _all_variants(old_name)
                    print(f"[변형 시도] {old_name!r} → {candidates}")
                    resolved = None
                    for candidate in candidates:
                        new_data = await self.get_user_data(session, candidate)
                        if new_data["tier"] is not None:
                            new_data["nickname"] = candidate
                            resolved = new_data
                            print(f"[변형 성공] {old_name!r} → {candidate!r}, tier={new_data['tier']}")
                            break
                    if resolved:
                        team_results[ti][pi] = resolved
                        any_variant_updated = True
                    else:
                        print(f"[변형 전부 실패] {old_name!r}")

            if any_variant_updated:
                embed, ok_count, fail_names = build_embed(team_results)
                await msg.edit(embed=embed)

        # ── Gemini 재질의 라운드 (남은 실패건만, 최대 MAX_RECHECK 회) ──
        # 변형으로도 못 찾은 경우에만 진입. Gemini 수정 닉 + 그 변형도 추가 시도.
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

            corrections: dict[str, str] = await asyncio.to_thread(
                self.recheck_failed_nicknames, image_bytes, failed_names_list
            )
            print(f"[Gemini 재시도 {recheck_round}] 수정안: {corrections}")

            any_updated = False
            async with aiohttp.ClientSession() as session:
                for ti, pi, r in failed_entries:
                    old_name = r["nickname"]
                    gemini_name = corrections.get(old_name, old_name)

                    if gemini_name == old_name:
                        # Gemini도 변경 없음 → 이 라운드에서 더 할 게 없음
                        print(f"[Gemini 재시도 {recheck_round}] {old_name!r}: Gemini 변경 없음, 스킵")
                        continue

                    # Gemini 수정 닉 + 그 변형까지 전부 시도
                    candidates_to_try: list[str] = [gemini_name]
                    for v in _all_variants(gemini_name):
                        if v not in candidates_to_try:
                            candidates_to_try.append(v)

                    print(f"[Gemini 재시도 {recheck_round}] {old_name!r} 후보: {candidates_to_try}")

                    resolved = None
                    for candidate in candidates_to_try:
                        new_data = await self.get_user_data(session, candidate)
                        if new_data["tier"] is not None:
                            new_data["nickname"] = candidate
                            resolved = new_data
                            print(f"[Gemini 재시도 성공] {old_name!r} → {candidate!r}, tier={new_data['tier']}")
                            break

                    if resolved:
                        team_results[ti][pi] = resolved
                        any_updated = True
                    else:
                        print(f"[Gemini 재시도 {recheck_round}] {old_name!r}: 모든 후보 실패")

            if any_updated:
                embed, ok_count, fail_names = build_embed(team_results)
                await msg.edit(embed=embed)

        # ── 재시도 끝, 최종 임베드 (실패 필드 문구 정리) ──
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

# ── OCR 응답 파서 ────────────────────────────
def _parse_teams(text: str) -> list[list[str]]:
    """
    Gemini가 반환한 팀 구분 텍스트를 파싱하여 list[list[str]] 로 변환.
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

    if not teams:
        names = [l.strip() for l in text.splitlines() if len(l.strip()) > 1]
        if names:
            teams = [names]

    return teams


async def setup(bot):
    await bot.add_cog(LobbyScan(bot))