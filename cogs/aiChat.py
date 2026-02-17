from google import genai
from google.genai import types
from discord.ext import commands
from config import AI_KEY
import traceback
import discord
import asyncio
import random

CALL_CONTEXT_TURNS = 16   # 호출 판정에 사용할 이전 대화 턴 수 (전체 채널)
CHAT_CONTEXT_TURNS = 5    # 답변 생성에 사용할 이전 대화 턴 수 (해당 유저만)

class CancelButton(discord.ui.View):
    def __init__(self, timeout=30):
        super().__init__(timeout=timeout)
        self.cancelled = False

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cancelled = True
        await interaction.response.edit_message(content="✅ 응답이 취소되었습니다.", view=None)
        self.stop()


reply_templates = [
    "이리와가 뭐라 할지 생각하는중...",
    "이리와가 뭔가 말하려고 하는중...",
    "이리와가 고양이 생각하는중... 이 아니고 대답을 고민중.",
]


def _call_gemini(client, model, prompt) -> str:
    """Gemini 호출 후 텍스트만 추출 (thought_signature 등 non-text parts 무시)"""
    response = client.models.generate_content(model=model, contents=prompt)
    # candidates.content.parts에서 text 타입만 추출 → thought_signature 경고 우회
    text_parts = [
        part.text
        for part in response.candidates[0].content.parts
        if hasattr(part, "text") and part.text
    ]
    return "".join(text_parts).strip()


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.client = genai.Client(api_key=AI_KEY)
        self.model = "models/gemini-2.5-flash-preview-04-17"

        # channel_id -> [(speaker, message)]
        self.channel_history: dict[int, list[tuple[str, str]]] = {}

        # user_id -> [(role, message)]  role: "user" | "bot"
        self.user_chat_history: dict[int, list[tuple[str, str]]] = {}

    # ────────────────────────────────────────────
    # 컨텍스트 빌더
    # ────────────────────────────────────────────

    def _channel_context(self, channel_id: int) -> str:
        history = self.channel_history.get(channel_id, [])
        recent = history[-(CALL_CONTEXT_TURNS * 2):]
        return "".join(f"{name}: {msg}\n" for name, msg in recent)

    def _user_context(self, user_id: int) -> str:
        history = self.user_chat_history.get(user_id, [])
        recent = history[-(CHAT_CONTEXT_TURNS * 2):]
        lines = []
        for role, msg in recent:
            lines.append(f"{'유저' if role == 'user' else '이리와'}: {msg}\n")
        return "".join(lines)

    def _last_bot_msg(self, user_id: int) -> str:
        history = self.user_chat_history.get(user_id, [])
        for role, msg in reversed(history):
            if role == "bot":
                return msg
        return ""

    # ────────────────────────────────────────────
    # 통합 AI 호출: 판정 + 응답 한 번에
    # ────────────────────────────────────────────

    async def _process(self, message: discord.Message, user_message: str) -> tuple[str, str, str]:
        """
        단일 AI 호출로 호출 판정 + 응답 생성을 동시에 처리.
        Returns: (status, confirm_msg, answer)
          status: "YES" | "NO" | "UNCERTAIN"
          confirm_msg: UNCERTAIN일 때 확인 메시지
          answer: YES일 때 최종 답변
        """
        user_id    = message.author.id
        user_name  = message.author.display_name
        channel_id = message.channel.id

        channel_ctx = self._channel_context(channel_id)
        user_ctx    = self._user_context(user_id)
        last_bot    = self._last_bot_msg(user_id)
        bot_asked   = any(kw in last_bot for kw in ["나한테", "물어본거", "말하는거", "부른거", "알려줄까"])
        recent_replied = bool(last_bot)

        prompt = (
            "너는 디스코드 봇 '이리와'다. 이터널 리턴 봇이며, 현재는 2026년 시즌 10.\n"
            "카티야를 좋아하고, 툭툭 던지듯 짧게 말함. 본인 생각은 잘 드러내지 않음.\n\n"

            "━━━ [1단계] 호출 판정 ━━━\n"
            "아래 채널 대화를 보고, 마지막 메시지가 봇(이리와)에게 한 말인지 판단해.\n\n"

            "판단 기준 (위 → 아래 순서대로 체크):\n"
            "0. 추임새 필터 (최우선): '엄','흠','음','어','ㅇㅎ','ㅋㅋ','ㄷㄷ','ㅎㅎ','ㄴㄴ','?','ㅁ?','뭐?' 등 → 무조건 NO\n"
            "   예외: 봇이 직전에 확인 질문('나한테 말하는거야?' 등)을 했고 유저가 '응'/'어'/'ㅇㅇ'로 답한 경우만 YES\n"
            "1. 다른 유저들끼리 대화 중 → NO\n"
            "2. '이리와','리와','봇','@이리와' 등 이름 직접 언급 → YES\n"
            f"3. 봇 확인 질문 여부: {'있음' if bot_asked else '없음'} / 최근 봇 응답: {'있음' if recent_replied else '없음'}\n"
            "   확인 질문 후 긍정 답변 → YES / 명확한 후속 질문 → YES\n"
            "4. 게임 관련이지만 봇 언급 없고 애매함 → UNCERTAIN\n"
            "5. 나머지 → NO\n\n"

            "⚠️ 특별 규칙: 봇이 A 유저에게 확인 질문했는데 B 유저가 긍정 답변한 경우,\n"
            "   채널 전체 맥락에서 원래 질문을 파악해서 카테고리 선택.\n\n"

            f"=== 채널 전체 대화 ===\n{channel_ctx}"
            f"{user_name}: {user_message}\n\n"
            f"=== {user_name}과의 1:1 대화 ===\n{user_ctx}\n\n"

            "━━━ [2단계] 응답 생성 (CALLED=YES인 경우만) ━━━\n"
            "말투: 카티야 스타일 (에고 동화 X, 말투만)\n"
            "규칙:\n"
            "- 2~3문장 이내, 핵심만\n"
            "- 줄바꿈 최대 1번\n"
            "- 목차식 설명 금지\n\n"

            "━━━ 출력 형식 (정확히 이 형식으로만) ━━━\n"
            "CALLED: YES 또는 NO 또는 UNCERTAIN\n"
            "CONFIRM_MSG: (UNCERTAIN일 때만 확인 메시지, 맥락을 보고 어울리도록 다양하게 변형할 것)\n"
            "ANSWER: (YES일 때만 최종 답변)\n\n"

            "CONFIRM_MSG 예시: '나한테 물어본거?', '내 얘기하는거야?', '날 부른거임?', '내가 알려줄까?'\n"
        )

        raw = await asyncio.to_thread(_call_gemini, self.client, self.model, prompt)

        status, confirm_msg, answer = "NO", "", ""
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("CALLED:"):
                status = line.split(":", 1)[1].strip().upper()
            elif line.startswith("CONFIRM_MSG:"):
                confirm_msg = line.split(":", 1)[1].strip()
            elif line.startswith("ANSWER:"):
                answer = line.split(":", 1)[1].strip()

        return status, confirm_msg, answer

    # ────────────────────────────────────────────
    # 히스토리 관리
    # ────────────────────────────────────────────

    def _add_channel(self, channel_id: int, name: str, msg: str):
        history = self.channel_history.setdefault(channel_id, [])
        history.append((name, msg))
        if len(history) > 100:
            self.channel_history[channel_id] = history[-20:]

    def _add_user(self, user_id: int, role: str, msg: str):
        self.user_chat_history.setdefault(user_id, []).append((role, msg))

    # ────────────────────────────────────────────
    # 메인 진입점
    # ────────────────────────────────────────────

    async def ask_ai(self, message: discord.Message, user_message: str) -> str:
        user_id    = message.author.id
        user_name  = message.author.display_name
        channel_id = message.channel.id

        print(f"🟡 메시지 수신 - {user_name}: {user_message}")

        self._add_channel(channel_id, user_name, user_message)

        # 통합 AI 호출
        status, confirm_msg, answer = await self._process(message, user_message)
        print(f"🔵 판정: {status} | 확인: '{confirm_msg}' | 답변: '{answer[:30]}...' " if answer else f"🔵 판정: {status}")

        # UNCERTAIN → 확인 메시지 발송 후 종료
        if status == "UNCERTAIN" and confirm_msg:
            await message.reply(confirm_msg, mention_author=False)
            self._add_user(user_id, "user", user_message)
            self._add_user(user_id, "bot", confirm_msg)
            self._add_channel(channel_id, "이리와", confirm_msg)
            return ""

        if status != "YES":
            return ""

        # YES → 응답 중 메시지 + 취소 버튼
        cancel_view = CancelButton(timeout=30)
        status_msg = await message.reply(
            f"⧖ **{random.choice(reply_templates)}**",
            view=cancel_view,
            mention_author=False
        )

        # 취소 버튼 감시 (응답은 이미 생성됨)
        await asyncio.sleep(0)  # 이벤트 루프 양보
        if cancel_view.cancelled:
            return ""

        text = answer or "몰라"

        self._add_user(user_id, "user", user_message)
        self._add_user(user_id, "bot", text)
        self._add_channel(channel_id, "이리와", text)

        await status_msg.delete()
        return text


async def setup(bot):
    await bot.add_cog(AIChat(bot))