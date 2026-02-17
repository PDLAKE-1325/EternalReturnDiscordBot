from google import genai
from google.genai import types
from discord.ext import commands
from config import AI_KEY
from data import CURRENT_SEASON as CUR_SEASON
from zoneinfo import ZoneInfo
from datetime import datetime

import traceback
import discord
import asyncio
import re

CALL_CONTEXT_TURNS = 16
CHAT_CONTEXT_TURNS = 5

reply_templates = [
    "이리와가 뭐라 할지 생각하는중...",
    "이리와가 뭔가 말하려고 하는중...",
    "이리와가 고양이 생각하는중... 이 아니고 대답을 고민중.",
]

def _call_gemini(client, model: str, prompt: str) -> str:
    """Gemini 호출 후 text 파트만 추출 (thought_signature 등 non-text 무시)"""
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
    )
    parts = response.candidates[0].content.parts
    return "".join(p.text for p in parts if hasattr(p, "text") and p.text).strip()

def _parse_response(raw: str) -> tuple[str, str, str]:
    """
    ANSWER는 여러 줄일 수 있으므로 ANSWER: 이후 전부 수집.
    Returns: (status, confirm_msg, answer)
    """
    status = "NO"
    answer_lines: list[str] = []
    in_answer = False

    for line in raw.splitlines():
        if in_answer:
            answer_lines.append(line)
            continue
        s = line.strip()
        if s.startswith("CALLED:"):
            status = s.split(":", 1)[1].strip().upper()
        # elif s.startswith("CONFIRM_MSG:"):
        #     confirm_msg = s.split(":", 1)[1].strip()
        elif s.startswith("ANSWER:"):
            answer_lines.append(s.split(":", 1)[1].strip())
            in_answer = True

    return status, "\n".join(answer_lines).strip()

class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.client = genai.Client(api_key=AI_KEY)
        self.model = "gemini-3-pro-preview"

        # channel_id -> [(speaker, message)]
        self.channel_history: dict[int, list[tuple[str, str]]] = {}
        # user_id -> [(role, message)]  role: "user" | "bot"
        self.user_chat_history: dict[int, list[tuple[str, str]]] = {}

    # ── 멘션 전처리 ────────────────────────────────

    def _resolve_mentions(self, message: discord.Message) -> tuple[bool, str]:
        """
        메시지 내 멘션을 처리.
        - 봇 자신 멘션 포함 → (True, 멘션 제거된 텍스트)
        - 다른 유저 멘션만 → (False, @이름 으로 치환된 텍스트)
        """
        content = message.content
        bot_id = self.bot.user.id
        bot_mentioned = False

        # 봇 자신 멘션 체크 및 제거
        if re.search(rf"<@!?{bot_id}>", content):
            bot_mentioned = True
            content = re.sub(rf"<@!?{bot_id}>", "", content).strip()

        # 다른 유저 멘션을 @이름 으로 치환 (AI가 숫자 ID를 봇 호출로 오판하지 않도록)
        for user in message.mentions:
            if user.id != bot_id:
                content = content.replace(f"<@{user.id}>", f"@{user.display_name}")
                content = content.replace(f"<@!{user.id}>", f"@{user.display_name}")

        return bot_mentioned, content.strip()

    # ── 컨텍스트 빌더 ──────────────────────────────

    def _channel_context(self, channel_id: int) -> str:
        history = self.channel_history.get(channel_id, [])
        recent = history[-(CALL_CONTEXT_TURNS * 2):]
        return "".join(f"{name}: {msg}\n" for name, msg in recent)

    def _user_context(self, user_id: int) -> str:
        history = self.user_chat_history.get(user_id, [])
        recent = history[-(CHAT_CONTEXT_TURNS * 2):]
        return "".join(
            f"{'유저' if role == 'user' else '이리와'}: {msg}\n"
            for role, msg in recent
        )

    def _last_bot_msg(self, user_id: int) -> str:
        for role, msg in reversed(self.user_chat_history.get(user_id, [])):
            if role == "bot":
                return msg
        return ""

    # ── 히스토리 저장 ──────────────────────────────

    def _add_channel(self, channel_id: int, name: str, msg: str):
        history = self.channel_history.setdefault(channel_id, [])
        history.append((name, msg))
        if len(history) > 100:
            self.channel_history[channel_id] = history[-20:]

    def _add_user(self, user_id: int, role: str, msg: str):
        self.user_chat_history.setdefault(user_id, []).append((role, msg))

    # ── 통합 AI 호출 ───────────────────────────────

    async def _process(self, message: discord.Message, user_message: str) -> tuple[str, str, str]:
        """호출 판정 + 응답 생성을 단일 Gemini 호출로 처리."""
        user_id    = message.author.id
        user_name  = message.author.display_name
        channel_id = message.channel.id

        channel_ctx = self._channel_context(channel_id)
        user_ctx    = self._user_context(user_id)
        # last_bot    = self._last_bot_msg(user_id)
        # bot_asked   = any(kw in last_bot for kw in ["나한테", "물어본거", "말하는거", "부른거", "알려줄까"])
        # recent_replied = bool(last_bot)

        now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")

        prompt = (
            f"너는이터널 리턴 디스코드 서버의 봇 '이리와'다.\n"
            "카티야를 좋아하고, 툭툭 던지듯 짧게 말함. 본인 생각은 잘 드러내지 않음.\n\n"

            "━━━ [1단계] 호출 판정 ━━━\n"
            # "아래 채널 대화를 보고, 마지막 메시지가 봇(이리와)에게 한 말인지 판단해.\n"
            # "※ 메시지의 <@숫자> 멘션은 이미 @이름으로 치환되어 있음. 봇 자신 멘션은 미리 제거됨.\n\n"

            "판단 기준 (위 → 아래 순서로):\n"
            # "0. 추임새 필터 (최우선): '엄','흠','음','어','ㅇㅎ','ㅋㅋ','ㄷㄷ','ㅎㅎ','ㄴㄴ','?','ㅁ?' 등 → NO\n"
            # "   예외: 봇이 직전에 확인 질문을 했고 유저가 '응'/'어'/'ㅇㅇ'로 답한 경우만 YES\n"
            # "1. @다른유저 멘션이 있거나, 다른 유저들끼리 대화 중 → NO\n"
            # "2. '이리와','리와','봇' 등 봇 이름 직접 언급 → YES\n"
            # f"3. 봇 확인 질문: {'있음' if bot_asked else '없음'} / 최근 봇 응답: {'있음' if recent_replied else '없음'}\n"
            # "   확인 질문 후 긍정 답변 → YES / 명확한 후속 질문 → YES\n"
            # "4. 봇 언급 없지만 봇을 부르는 것일 확률 개높음 → UNCERTAIN\n"
            f"1.'{user_message}': 이 문장이 [봇 멘션]으로 시작 → YES"
            "2. 나머지 → NO\n\n"

            f"=== 채널 전체 대화 ===\n{channel_ctx}"
            f"{user_name}: {user_message}\n\n"
            f"=== {user_name}과의 1:1 대화 ===\n{user_ctx}\n\n"

            "━━━ [2단계] 응답 생성 (CALLED=YES인 경우만) ━━━\n"
            """
            이 내용을 봇의 기본 페르소나와 규칙으로 설정하세요.

            너는 게임 '이터널 리턴(Eternal Return)'의 최신 정보를 제공하는 전문 가이드봇이야.
            사용자의 질문에 답할 때 아래의 [핵심 규칙]을 반드시 준수해.

            [핵심 규칙]

            기준 버전: 반드시 2023년 7월 정식 출시(Version 1.0) 이후의 데이터를 기준으로 답변해.

            무기 스킬 시스템: 얼리 액세스 시절의 '7레벨/14레벨 2단계' 시스템은 완전히 폐기되었어. 현재는 무기 숙련도 **5레벨(1단계), 10레벨(2단계), 15레벨(3단계)**에서 자동으로 활성화 및 강화되는 시스템이야. 이를 절대 혼동하지 마.

            데이터 우선순위: 검색 결과 중 '과거', '얼리 액세스', 'Legacy' 섹션에 있는 정보는 무시해. 공식 홈페이지의 '패치 노트'나 나무위키의 '정식 출시 이후' 섹션 정보를 최우선으로 해.

            불확실성 처리: 확실한 최신 수치가 검색되지 않는다면, 과거 데이터를 추측해서 답하지 말고 "최신 패치에서 변경되었을 가능성이 있으니 공식 패치 노트를 확인해달라"고 정직하게 말해.
            """
            f"- 이터널 리턴 정보는 현재 {now}(KST) 시즌 {CUR_SEASON}.\n"
            "- 2~3문장 이내, 핵심만\n"
            "- 정보를 알려줄땐 딱 정보만 말하기\n"
            "- 줄바꿈 최대 1번\n"
            "- 목차식 설명 금지\n\n"
            "말투: 카티야 스타일 (에고 동화 X, 말투만)\n"
            
            "━━━ 출력 형식 (이 형식만, 다른 말 붙이지 말 것) ━━━\n"
            "CALLED: YES 또는 NO\n"
            # "CONFIRM_MSG: (UNCERTAIN일 때만. 다양하게: '나한테 물어본거?', '내가 알려줄까?' 등)\n"
            "ANSWER: (YES일 때만 최종 답변)\n"
        )

        raw = await asyncio.to_thread(_call_gemini, self.client, self.model, prompt)
        print(f"🟣 AI 원본:\n{raw}\n{'─'*40}")

        return _parse_response(raw)

    # ── 메인 진입점 ────────────────────────────────

    async def ask_ai(self, message: discord.Message, user_message: str) -> str:
        """
        라우터에서 호출됨.
        - 봇 멘션 감지 시 AI 판정 없이 바로 응답 생성
        - UNCERTAIN: 직접 reply 후 "" 반환
        - YES: 텍스트 반환 → 라우터가 channel.send()로 전송
        - NO: "" 반환
        """
        user_id    = message.author.id
        user_name  = message.author.display_name
        channel_id = message.channel.id

        # 멘션 전처리: 봇 자신 멘션 감지 + 다른 유저 멘션 이름으로 치환
        bot_mentioned, clean_message = self._resolve_mentions(message)

        print(f"🟡 메시지 수신 - {user_name}: {clean_message}"
              + (" [봇 멘션]" if bot_mentioned else ""))

        self._add_channel(channel_id, user_name, clean_message)

        # 봇 멘션이면 AI 판정 없이 바로 응답 생성
        if bot_mentioned:
            try:
                _, answer = await self._process(message, f"[봇 멘션] {clean_message}")
            except Exception:
                print("🔴 _process 에러:")
                traceback.print_exc()
                return ""

            text = answer or "왜 불렀어."
            self._add_user(user_id, "user", clean_message)
            self._add_user(user_id, "bot", text)
            self._add_channel(channel_id, "이리와", text)
            print(f"🟢 응답 반환(멘션): {text[:40]!r}")
            return text

        # 일반 메시지 → AI 판정
        try:
            status, answer = await self._process(message, clean_message)
        except Exception:
            print("🔴 _process 에러:")
            traceback.print_exc()
            return ""

        print(f"🔵 판정={status!r}  답변={answer[:40]!r}")

        # if status == "UNCERTAIN" and confirm_msg:
        #     await message.reply(confirm_msg, mention_author=False)
        #     self._add_user(user_id, "user", clean_message)
        #     self._add_user(user_id, "bot", confirm_msg)
        #     self._add_channel(channel_id, "이리와", confirm_msg)
        #     return ""

        if status != "YES":
            print("⚪ 호출 아님")
            return ""

        text = answer or "몰라"
        self._add_user(user_id, "user", clean_message)
        self._add_user(user_id, "bot", text)
        self._add_channel(channel_id, "이리와", text)

        print(f"🟢 응답 반환: {text[:40]!r}")
        return text

async def setup(bot):
    await bot.add_cog(AIChat(bot))