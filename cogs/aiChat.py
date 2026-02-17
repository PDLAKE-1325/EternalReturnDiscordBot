from google import genai
from discord.ext import commands
from config import AI_KEY
import traceback
import discord
import asyncio
import random

CALL_CONTEXT_TURNS = 16   # 호출 판정에 사용할 이전 대화 턴 수 (전체 채널)
CHAT_CONTEXT_TURNS = 5   # 답변 생성에 사용할 이전 대화 턴 수 (해당 유저만)

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
    [
        "이리와가 정답지를 훔쳐보는중...",
        "이리와가 곰곰히 생각하는중...",
        "이리와가 책을 찾아보는중...",
        "이리와가 기억이 안나서 당황하는중...",
        "이리와가 오늘 점심 메뉴를 생각하는중... 이 아니고 대답을 생각하는중...",
    ],
    [
        "이리와가 뭐라 할지 생각하는중...",
        "이리와가 뭔가 말하려고 하는중...",
        "이리와가 고양이 생각하는중... 이 아니고 대답을 고민중.",
    ],
]

class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.client = genai.Client(api_key=AI_KEY)
        self.model = "models/gemini-3-flash-preview"
        
        # channel_id -> [(user_name, message)]
        self.channel_history: dict[int, list[tuple[str, str]]] = {}
        
        # user_id -> [(role, message)]
        self.user_chat_history: dict[int, list[tuple[str, str]]] = {}
    
    def _build_channel_context(self, channel_id: int, current_user: str) -> str:
        """전체 채널의 최근 대화 기록 구성"""
        history = self.channel_history.get(channel_id, [])
        if not history:
            return ""

        max_items = CALL_CONTEXT_TURNS * 2  # 더 많은 맥락 확보
        recent = history[-max_items:]

        text = ""
        for user_name, msg in recent:
            text += f"{user_name}: {msg}\n"

        return text

    def _build_user_context(self, user_id: int) -> str:
        """특정 유저와의 대화 기록 구성"""
        history = self.user_chat_history.get(user_id, [])
        if not history:
            return ""

        max_items = CHAT_CONTEXT_TURNS * 2
        recent = history[-max_items:]

        text = ""
        for role, msg in recent:
            prefix = "유저" if role == "user" else "이리와"
            text += f"{prefix}: {msg}\n"

        return text

    # 2️⃣ AI 호출 판정 + 필요한 지식 카테고리 반환
    async def ai_is_called(self, user_message: str, user_name: str, channel_id: int, user_id: int) -> tuple[bool, list[str], str, str]:
        """
        AI를 통해 호출 여부와 필요한 지식 카테고리를 판정
        Returns: (호출됨 여부, DB 영어 키 리스트, 확인 문자열)
        """
        channel_context = self._build_channel_context(channel_id, user_name)
        user_context = self._build_user_context(user_id)
        
        #print("🟡 호출 판정 필요")
        # print(f"📢 채널 전체 맥락:\n{channel_context}")
        #print(f"유저 {user_name}과의 대화:\n{user_context}")
        
        # 최근 봇이 이 유저에게 응답했는지 확인
        user_history = self.user_chat_history.get(user_id, [])
        recent_bot_replied = len(user_history) >= 2 and user_history[-2][0] == "bot"
        
        # 최근 봇이 확인 질문을 했는지 체크
        bot_asked_confirmation = False
        if len(user_history) >= 2:
            last_bot_msg = user_history[-1][1] if user_history[-1][0] == "bot" else ""
            if any(keyword in last_bot_msg for keyword in ["나한테", "물어본거", "말하는거", "부른거"]):
                bot_asked_confirmation = True
        
        prompt = (
            "너는 디스코드 봇 '이리와'의 호출 판정 시스템이다.\n\n"
            "아래는 디스코드 채널의 전체 대화 흐름이다.\n"
            "마지막 메시지가 봇(이리와)에게 한 말인지 판단해.\n\n"
            
            "🚨 판단 기준 (위에서 아래로 순서대로 체크, 먼저 걸리면 그걸로 결정):\n\n"
            
            "【0순위】 추임새/감탄사 필터 (최우선!)\n"
            "다음 표현들은 **무조건 NO** 처리:\n"
            "- 한글자 추임새: '엄', '흠', '음', '어'\n"
            "- 짧은 감탄: 'ㅇㅎ', 'ㅋㅋ', 'ㄷㄷ', 'ㅎㅎ', 'ㄴㄴ'\n"
            "- 단독 의문: '?', 'ㅁ?', '뭐?'\n"
            "- 단독 긍정: 'ㅇㅇ', '응', '어' (확인 질문 답변 아닐 때)\n\n"
            
            "⚠️ 유일한 예외: 봇이 직전에 확인 질문('나한테 말하는거야?' 등)을 했고\n"
            "   유저가 '응'/'어'/'ㅇㅇ'로 답한 경우만 → YES\n\n"
            
            "【1순위】 다른 유저들끼리 대화 중인지 체크\n"
            "- 채널 전체 맥락에서 2명 이상이 대화 중이고\n"
            "- 현재 메시지가 그 대화 흐름에 자연스럽게 이어지면 → NO\n"
            "- 예시:\n"
            "  유저A: 머함\n"
            "  이리와: 왜 불러? ← 잘못된 반응!\n"
            "  (정답: NO, 유저A가 유저B한테 물어본 것일 수 있음)\n\n"
            
            "【2순위】 봇 이름 직접 언급\n"
            "- '이리와', '리와', '봇', '@이리와' 등 명시 → YES\n\n"
            
            "【3순위】 확인 질문 후 긍정 답변\n"
            "- 봇이 확인 질문 했음: {bot_asked_confirmation}\n"
            "- 유저가 '응'/'어'/'ㅇㅇ'/긍정 답변 → YES\n\n"
            
            "【4순위】 명확한 후속 질문\n"
            "- 직전 봇 응답: {recent_bot_replied}\n"
            "- 단, 단순 추임새('엄', 'ㅇㅎ', 'ㅋㅋ')는 후속이 아님 → NO\n"
            "- 명확한 질문/요청일 때만 → YES\n\n"
            
            "【5순위】 게임 관련이지만 애매함\n"
            "- 게임 용어 있지만 봇 언급 없음 → UNCERTAIN\n\n"
            
            "【6순위】 그 외\n"
            "- 모든 나머지 경우 → NO\n\n"

            "중요: 다른 유저들끼리 대화하는 것과 봇에게 말하는 것을 명확히 구분해야 함!\n"
            "특히 주의: 단순 추임새는 거의 항상 NO!\n\n"
            
            "잘못 판단 예시:\n"
            "채널 전체:\n"
            "SyntaxInvalid: 머함\n"
            "이리와: 왜 불러? 시간은 금이야. < 오판!\n"
            "최익현: 엄\n"
            "이리와: [응답함] < 오판!\n"
            "→ 정답: 둘 다 NO (추임새 + 다른 유저 대화)\n\n"

            "⚠️ 특별 규칙 - 확인 질문 후 다른 유저의 긍정 답변:\n"
            "- 봇이 'A 유저'의 질문에 대해 확인 질문을 했는데, 'B 유저'가 긍정 답변('ㅇㅇ', '어', '응')을 한 경우\n"
            "- 이는 'B 유저'가 'A 유저'를 대신해서 답변한 것일 수 있음\n"
            "- 이 경우 원래 질문자('A 유저')가 무엇을 물어봤는지 채널 전체 맥락에서 찾아서 카테고리를 선택할 것!\n\n"
            
            "예시:\n"
            "유저1: 카티야가 궁금하긴 해\n"
            "이리와: 카티야에 대해서 알려줘?\n"
            "유저2: ㅇㅇ  ← 다른 사람이 대신 답변\n"
            
            f"=== 채널 전체 대화 ===\n{channel_context}\n"
            f"{user_name}: {user_message}\n\n"
            
            f"=== {user_name}과 봇의 이전 대화 ===\n{user_context}\n\n"
            
            f"직전 봇→{user_name} 응답: {'있음' if recent_bot_replied else '없음'}\n"
            f"봇의 확인 질문 여부: {'있음' if bot_asked_confirmation else '없음'}\n\n"
            
            "출력 형식 (정확히 이 형식으로):\n"
            "CALLED: YES 또는 NO 또는 UNCERTAIN\n"
            "CONFIRM_MSG: 확인 메시지 (UNCERTAIN일 때만)\n"
            "REASON: 판단 이유 (한 줄로)\n\n"

            "CONFIRM_MSG 가이드:\n"
            "매번 '나한테 말하는 거야?'만 쓰지 말고 다양하게 변형할 것\n"
            "예: '나한테 물어본거?', '내 얘기하는거야?', '날 부른거임?', '내가 알려줄까', '~를 나한테 물은거?' 등\n\n"
            
            "판단 가이드:\n"
            "- YES: 봇에게 확실히 말함 (봇 이름 언급, 확인 후 긍정 답변, 직전 대화 후 명확한 질문)\n"
            "- UNCERTAIN: 애매함 (게임 관련이지만 봇 언급 없음)\n"
            "- NO: 봇 무관 (추임새, 다른 유저와 대화, 혼잣말, 맥락 없는 의문문)\n\n"
            
            "예시 1 (추임새 → NO):\n"
            "CALLED: NO\n"
            "CONFIRM_MSG: \n"
            "REASON: 추임새 '엄'만 있음, 0순위 필터로 NO\n\n"
            
            "예시 2 (다른 유저 대화 → NO):\n"
            "CALLED: NO\n"
            "CONFIRM_MSG: \n"
            "REASON: 유저A와 유저B가 대화 중, 봇 언급 없음\n\n"
            
            "예시 3 (확인 질문 후 긍정 답변):\n"
            "CALLED: YES\n"
            "CONFIRM_MSG: \n"
            "REASON: 직전 봇이 확인 질문했고 'ㅇㅇ'로 긍정 답변\n\n"
        )
        
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt
            )
            result = response.text.strip()
            #print(f"🔵 AI 판정 결과:\n{result}")
            
            # 응답 파싱
            lines = result.split('\n')
            called = False
            confirm_msg = ""
            reason = ""
            
            for line in lines:
                line = line.strip()
                if line.startswith('CALLED:'):
                    status = line.split(':', 1)[1].strip().upper()
                    if status == 'YES':
                        called = True
                    elif status == 'UNCERTAIN':
                        called = False
                    else:  # NO
                        called = False
                elif line.startswith('CONFIRM_MSG:'):
                    confirm_msg = line.split(':', 1)[1].strip()
                elif line.startswith('REASON:'):
                    reason = line.split(':', 1)[1].strip()
            
            #print(f"✅ 파싱 - 호출: {called}, 카테고리 키: {category_keys}, 확인메시지: '{confirm_msg}', 이유: {reason}")
            return (called, confirm_msg, reason)
            
        except Exception as e:
            #print(f"⚠️ 호출 판정 실패: {e}")
            traceback.print_exc()
            return (recent_bot_replied, [], "", "")

    async def ask_ai(self, message: discord.Message, user_message: str) -> str:
        """
        메인 AI 응답 함수
        message: discord.Message 객체 (reply 및 취소 버튼을 위해 필요)
        user_message: 유저가 보낸 메시지 텍스트
        """
        user_id = message.author.id
        user_name = message.author.display_name
        channel_id = message.channel.id
        
        print(f"🟡 질문 받음 - {user_name}: {user_message}")

        # 채널 대화 기록에 추가
        channel_history = self.channel_history.setdefault(channel_id, [])
        channel_history.append((user_name, user_message))
        
        # 채널 기록 최대 100개로 제한
        if len(channel_history) > 100:
            self.channel_history[channel_id] = channel_history[-20:]

        # AI 호출 판정
        is_called, confirm_msg, reason_context = await self.ai_is_called(
            user_message, user_name, channel_id, user_id
        )
        #print(f"🔵 최종 호출 판정: {is_called}, 확인메시지: '{confirm_msg}', 필요 지식: {category_labels}")

        # 확인 메시지가 있으면 (애매한 경우) 확인 후 종료
        if confirm_msg:
            #print(f"⚠️ 애매한 상황 - 확인 요청: {confirm_msg}")
            await message.reply(confirm_msg, mention_author=False)
            # 대화 기록에 추가 (원래 질문 + 확인 메시지 모두 저장)
            user_history = self.user_chat_history.setdefault(user_id, [])
            user_history.append(("user", user_message))  # 원래 질문도 저장!
            user_history.append(("bot", confirm_msg))
            # 채널 기록에도 추가 (전체 맥락 파악용)
            channel_history = self.channel_history.setdefault(channel_id, [])
            channel_history.append(("이리와", confirm_msg))
            return ""

        if not is_called:
            #print("⚪️ 호출 아님")
            return ""
        
        # 🔔 응답 중 메시지 + 취소 버튼 (확실한 경우에만)
        cancel_view = CancelButton(timeout=30)
        cancel_view_message = random.choice(reply_templates[1])

        status_msg = await message.reply(
            f"⧖ **{cancel_view_message}**",
            view=cancel_view,
            mention_author=False
        )
        
        # 대화 기록 구성 (해당 유저와의 대화만)
        user_history = self.user_chat_history.get(user_id, [])
        max_items = CHAT_CONTEXT_TURNS * 2
        recent = user_history[-max_items:]

        history_text = ""
        for role, msg in recent:
            prefix = "유저" if role == "user" else "이리와"
            history_text += f"{prefix}: {msg}\n"

        # ✅ 추가: 채널 전체 맥락도 구성
        channel_context = self._build_channel_context(channel_id, user_name)

        prompt = (
            "너는 '이리와'라는 이터널 리턴 디스코드 봇.\n"
            "이터널 리턴 관련 정보 질문에 대답할 때 참고할것 : 현재는 2026년이며, 이터널 리턴 시즌 10이 진행중이다.\n"
            "카티야를 좋아하고, 툭툭 던지듯 짧게 대답함.\n"
            "본인 생각을 직접적으로 잘 드러내진 않음.\n\n"

            "좋은 대답 예시. \n"
            "질문: 봇아 돈내놔\n"
            "최종 응답: 크레딧은 시간 지나면 줘. 네 몫은 네가 챙겨야지.\n"
            "잘한 이유: 카티야의 말투를 잘 살렸고, 유머 감각도 있었으며, 그렇다고 과하지도 않은 수준.\n"
            "근데 이런것도 자주하면 에바야. 대화 맥락에 위와 같은 대답이 없거나 있더라도 확실한 상황 아니면 하지말고.\n\n"
            
            "⚠️ 핵심 규칙:\n"
            "1. 한 번에 2-3문장 이내로 답변 (필수!)\n"
            "2. 정보는 핵심만: '이건 뭐고, 저건 뭐야' 스타일\n"
            "3. 줄바꿈 최대 1번까지만 허용\n"
            "4. 불필요한 부연설명 금지\n\n"
            
            "말투 : 이터널 리턴의 실험체 '카티야' 스타일 > 에고 동화는 하지 말고 말투만 따라할것\n"
            
            "❌ 절대 하지 말 것:\n"
            "- 여러 단락으로 나눠서 설명\n"
            "- '버는 법은...', '쓸 곳은...' 같은 목차식 설명\n"
            "- 3문장 넘게 말하기\n\n"

             # ✅ 채널 전체 맥락 추가
            f"=== 채널 전체 대화 흐름 (참고용) ===\n{channel_context}\n\n"
            f"=== {user_name}과의 1:1 대화 ===\n{history_text}\n"
            f"=== 현재 분석에서 파악된 맥락(참고용) ===\n{reason_context}\n\n"

            f"유저: {user_message}\n\n"
            "답변 (3문장 이하, 핵심만):"
        )

        try:
            #print("🟠 Gemini 호출 시작")

            # AI 응답 생성 (취소 버튼 체크와 함께)
            response_task = asyncio.create_task(
                asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.model,
                    contents=prompt
                )
            )
            
            # 주기적으로 취소 여부 확인
            while not response_task.done():
                if cancel_view.cancelled:
                    #print("❌ 사용자가 응답을 취소했습니다")
                    response_task.cancel()
                    return ""
                await asyncio.sleep(0.5)
            
            response = await response_task
            
            # 취소되었으면 응답하지 않음
            if cancel_view.cancelled:
                #print("❌ 응답 생성 완료했지만 취소됨")
                return ""

            #print("🟢 Gemini 응답 수신")

            text = response.text.strip() if response.text else ""

            #print("✅ 최종 응답:", text if text else "응답 없음")
            
            # 🔁 대화 기록 저장 (유저별)
            user_history = self.user_chat_history.setdefault(user_id, [])
            user_history.append(("user", user_message))
            user_history.append(("bot", text))
            
            # 채널 기록에도 봇 응답 추가
            channel_history = self.channel_history.setdefault(channel_id, [])
            channel_history.append(("이리와", text))
            
            # 상태 메시지 삭제
            await status_msg.delete()

            return text if text else "몰라"

        except asyncio.CancelledError:
            #print("❌ 응답 생성이 취소되었습니다")
            return ""
        except Exception:
            #print("🔴 Gemini 호출 에러 발생")
            traceback.print_exc()
            
            # 에러 시 상태 메시지 업데이트
            try:
                await status_msg.edit(content="❌ 응답 생성 중 오류가 발생했습니다.", view=None)
            except:
                pass
            
            return "서버 오류거나 한도 다씀"




async def setup(bot):
    await bot.add_cog(AIChat(bot))