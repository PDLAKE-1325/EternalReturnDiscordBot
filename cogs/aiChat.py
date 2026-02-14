from google import genai
from discord.ext import commands
from config import AI_KEY
from data import get_ER_Database, Katja_Line
import traceback
import json
import discord
import asyncio
import random

CALL_CONTEXT_TURNS = 25   # 호출 판정에 사용할 이전 대화 턴 수 (전체 채널)
CHAT_CONTEXT_TURNS = 16   # 답변 생성에 사용할 이전 대화 턴 수 (해당 유저만)

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
        self.model = "models/gemini-2.5-flash"
        
        # ER DB를 딕셔너리로 저장 (JSON 파싱 필요)
        self.er_db_raw = get_ER_Database()
        self.er_db = json.loads(self.er_db_raw)

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
        #print(f"📢 채널 전체 맥락:\n{channel_context}")
        #print(f"유저 {user_name}과의 대화:\n{user_context}")
        
        # 최근 봇이 이 유저에게 응답했는지 확인
        user_history = self.user_chat_history.get(user_id, [])
        recent_bot_replied = len(user_history) >= 2 and user_history[-2][0] == "bot"
        
        # 사용 가능한 지식 카테고리 목록 (영어키: 한글설명)
        category_list = "\n".join([
            f"  - {key}: {info['label']}" 
            for key, info in self.er_db.items()
        ])
        
        prompt = (
            "너는 디스코드 봇 '이리와'의 호출 판정 시스템이다.\n\n"
            "아래는 디스코드 채널의 전체 대화 흐름이다.\n"
            "마지막 메시지가 봇(이리와)에게 한 말인지 판단하고, 필요한 지식 카테고리를 선택해.\n\n"
            
            "판단 기준:\n"
            "1. 봇 이름('이리와', '리와', '봇') 직접 언급 → YES\n"
            "2. 직전에 봇이 해당 유저에게 대답했고, 이어지는 질문/요청/관련 내용 → YES or UNCERTAIN\n"
            "3. 직전에 봇이 확인 질문('나한테 말하는거야?' 등)을 했고, 긍정 답변('ㅇㅇ', '어', '응' 등) → YES\n"
            "4. 단순 의문문('?', '뭐?', '왜?')만 있고 맥락 없음 → NO (여러 명이 있는 채널, 누구한테 한 말인지 불명확)\n"
            "5. (최고 중요한 조건) 다른 사람들끼리 대화 중 또는 불특정 다수에게 말한것 → NO\n"
            "5. 영문 모를 감탄사나 추임새('엄','흠','ㅇㅇㄴㅇ' 등등) → NO\n"
            "6. 게임 관련 질문이지만 봇 언급 없고 직전 대화도 없음 → UNCERTAIN\n\n"
            
            "중요: 다른 유저들끼리 대화하는 것과 봇에게 말하는 것을 명확히 구분해야 함!\n"
            "특히 주의: 단순 의문문('?', '뭐?', '왜?', 'ㅁ?' 등)은 이전 봇과의 대화에 대한 재질문이 아닌 이상 거의 항상 NO!\n"
            "→ 여러 명이 있는 채널에서 누구한테 한 말인지 불분명하기 때문\n\n"
            
            "잘못 판단 예시:\n"
            "유저: 봇아 일반겜 크레딧 시스템\n"
            "이리와: 크레딧? 일반겜에선 시간 지날 때마다 주는데, 동물이나 상대 잡는다고 따로 주는 건 아님. 주로 키오스크, 드론 호출, LUMI 같은 데 쓰는 인게임 재화야. 더 알려줘?\n"
            "유저: ㄴㄴ\n"
            "이리와: ㅇㅋ. 딴 거 더 궁금한 거 있음 물어봐.\n"
            "유저: 오늘 점심 뭐먹지\n"
            "이리와: 몰라. 알아서 먹어. < 오판 사례임 이게 - 이유: 그냥 다른 애들한테 물어본걸수도 있었음\n"
            "정답 : UNCERTAIN\n\n"

            "잘못 판단 예시2:\n"
            "유저: 봇아 뭐해 지금\n"
            "이리와: 아무것도 안 해. 그냥 있어.\n"
            "유저2: 봇 만듬?\n"
            "이리와: 봇인 건 맞아. 별거 없지만 < 오판 사례 - 이유: 유저끼리 말한거임.\n"
            "정답 : NO\n\n"

            "잘못 판단 예시3:\n"
            "유저: 봇에게 질문\n"
            "이리와: 관련하여 대답\n"
            "유저2: 넌 아는게 뭐야\n"
            "이리와: 대답 < 오판 사례 - 이유: 다른 유저가 갑자기 말한거라 유저끼리 말한건지, 봇한테 말한지 모름\n"
            "정답 : UNCERTAIN\n\n"

            """잘못된 판단 예시 4.
            🟡 질문 받음 - ▁▁: 그렇다는데?
            🟡 호출 판정 필요
            📢 채널 전체 맥락:
            ▁▁: 봇상 하와유 브로
            이리와: 별 일 없어. 궁금한 거라도 있어?
            고등어: 오 그럼 키오스크가 뭔데요
            이리와: 키오스크는 영웅 등급 재료나 부활, 탈출 카드키 살 수 있는 곳이야.
            맵에 정해진 위치에 있고 2일차 낮부터 쓸 수 있어.
            주변 확인하고 써, 다른 놈이 뺏어갈 수도 있으니까.
            ▁▁: ㄷㄷ
            ▁▁: 레전드 이리견
            고등어: 어쩔
            고등어: 루미도 궁금하긴 해
            이리와: LUMI는 섬을 돌아다니는 상점 로봇이야. 키오스크처럼 재료를 팔고, 아군 부활도 시켜. 물건은 한 번에 하나만 살 수 있어.
            ▁▁: 엄
            고등어: 쇼이치도 궁금해하겠다
            ▁▁: 그럴듯
            이리와: 쇼이치에 대해 알려달라는 말이야?
            ▁▁: 아님
            이리와: 응, 그래. 목적만 얘기해.
            ▁▁: ㅋㅋ
            ▁▁: 그렇다는데?

            유저 ▁▁과의 대화:
            유저: 봇상 하와유 브로
            이리와: 별 일 없어. 궁금한 거라도 있어?
            유저: 아님
            이리와: 응, 그래. 목적만 얘기해.

            🔵 AI 판정 결과:
            CALLED: YES
            CATEGORIES: NONE
            CONFIRM_MSG: 
            REASON: 직전 봇이 해당 유저에게 대답했고, 그에 대한 반응으로 봇과의 대화를 이어가고 있음.

            >
            정답: NO
            이유: "▁▁: 그렇다는데?"는 "이리와: 응, 그래. 목적만 얘기해."에 대해서 "고등어" 유저에게 말하는거임.
            """

            f"사용 가능한 지식 카테고리 (영어키: 설명):\n{category_list}\n\n"
            
            "⚠️ 카테고리 선택 규칙 (매우 중요!):\n"
            "- CATEGORIES에는 반드시 위 목록의 '영어 키'만 입력할 것!\n"
            "- 예: gamePlayMods, lumiaIslandGameplay_economy (O)\n"
            "- 예: 플레이 모드, 경제 시스템 (X - 한글 설명 사용 금지!)\n"
            "- 여러 개 선택 시 쉼표로 구분 (공백 없이): gamePlayMods,lumiaIslandGameplay_economy\n"
            "- 게임 무관 잡담이면: NONE\n\n"

            "⚠️ 특별 규칙 - 확인 질문 후 다른 유저의 긍정 답변:\n"
            "- 봇이 'A 유저'의 질문에 대해 확인 질문을 했는데, 'B 유저'가 긍정 답변('ㅇㅇ', '어', '응')을 한 경우\n"
            "- 이는 'B 유저'가 'A 유저'를 대신해서 답변한 것일 수 있음\n"
            "- 이 경우 원래 질문자('A 유저')가 무엇을 물어봤는지 채널 전체 맥락에서 찾아서 카테고리를 선택할 것!\n\n"
            
            "예시:\n"
            "유저1: 카티야가 궁금하긴 해\n"
            "이리와: 카티야에 대해서 알려줘?\n"
            "유저2: ㅇㅇ  ← 다른 사람이 대신 답변\n"
            "→ CATEGORIES: gameCharacterStatus (원래 질문자 '▁▁'이 물어본 '카티야' 기준)\n\n"
            
            
            f"=== 채널 전체 대화 ===\n{channel_context}\n"
            f"{user_name}: {user_message}\n\n"
            
            f"=== {user_name}과 봇의 이전 대화 ===\n{user_context}\n\n"
            
            f"직전 봇→{user_name} 응답: {'있음' if recent_bot_replied else '없음'}\n\n"
            
            "출력 형식 (정확히 이 형식으로):\n"
            "CALLED: YES 또는 NO 또는 UNCERTAIN\n"
            "CATEGORIES: 영어키1,영어키2 (쉼표로 구분, 공백 없이, 필요없으면 NONE)\n"
            "CONFIRM_MSG: 확인 메시지 (UNCERTAIN일 때만)\n"
            "REASON: 판단 이유 (한 줄로)\n\n"

            "CONFIRM_MSG 가이드:\n"
            "매번 '나한테 말하는 거야?'만 쓰지 말고 다양하게 변형할 것\n"
            "예: '나한테 물어본거?', '내 얘기하는거야?', '날 부른거임?' 등\n\n"
            
            "판단 가이드:\n"
            "- YES: 봇에게 확실히 말함 (봇 이름 언급, 확인 후 긍정 답변, 직전 대화 이어감)\n"
            "- UNCERTAIN: 애매함 (게임 관련이지만 봇 언급 없음, 직전 대화 후 애매한 반응)\n"
            "- NO: 봇 무관 (단순 의문문, 다른 유저와 대화, 혼잣말)\n\n"
            
            "예시 1 (확실한 호출 - 크레딧 질문):\n"
            "CALLED: YES\n"
            "CATEGORIES: gamePlayMods,lumiaIslandGameplay_economy\n"
            "CONFIRM_MSG: \n"
            "REASON: 봇 이름 '봇아' 직접 언급하며 일반겜 크레딧 시스템 질문\n\n"
            
            "예시 2 (단순 의문문 - 맥락 없음):\n"
            "CALLED: NO\n"
            "CATEGORIES: NONE\n"
            "CONFIRM_MSG: \n"
            "REASON: '?' 하나만 던짐, 누구한테 한 말인지 불명확\n\n"
            
            "예시 3 (애매한 상황):\n"
            "CALLED: UNCERTAIN\n"
            "CATEGORIES: NONE\n"
            "CONFIRM_MSG: 나한테 물어본거야?\n"
            "REASON: 게임 관련이지만 봇 언급 없고 맥락 불분명\n\n"
            
            "예시 4 (확인 질문 후 긍정 답변):\n"
            "CALLED: YES\n"
            "CATEGORIES: lumiaIslandGameplay_summary\n"
            "CONFIRM_MSG: \n"
            "REASON: 직전 봇이 확인 질문했고 'ㅇㅇ'로 긍정 답변\n\n"
            
            "예시 5 (게임 무관 잡담):\n"
            "CALLED: YES\n"
            "CATEGORIES: NONE\n"
            "CONFIRM_MSG: \n"
            "REASON: 봇 이름 언급했지만 게임과 무관한 일상 대화\n"
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
            category_keys = []  # 이제 영어 키를 담음
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
                elif line.startswith('CATEGORIES:'):
                    cat_text = line.split(':', 1)[1].strip()
                    if cat_text != 'NONE':
                        # 쉼표로 분리하고 공백 제거
                        category_keys = [c.strip() for c in cat_text.split(',')]
                elif line.startswith('CONFIRM_MSG:'):
                    confirm_msg = line.split(':', 1)[1].strip()
                elif line.startswith('REASON:'):
                    reason = line.split(':', 1)[1].strip()
            
            #print(f"✅ 파싱 - 호출: {called}, 카테고리 키: {category_keys}, 확인메시지: '{confirm_msg}', 이유: {reason}")
            return (called, category_keys, confirm_msg, reason)
            
        except Exception as e:
            #print(f"⚠️ 호출 판정 실패: {e}")
            traceback.print_exc()
            return (recent_bot_replied, [], "")


    async def load_knowledge(self, category_keys: list[str]) -> str:
        """
        지정된 영어 키의 지식만 로드
        category_keys가 비어있으면 빈 문자열 반환
        """
        if not category_keys:
            #print("📚 지식 없이 대화만")
            return ""
        
        knowledge_parts = []
        matched_keys = []
        
        for key in category_keys:
            if key in self.er_db:
                info = self.er_db[key]
                content = info.get("content", "")
                if content:
                    knowledge_parts.append(f"[{key}]\n{content}")
                    matched_keys.append(key)
                    #print(f"  ✅ 로드 성공: {key}")
            else:
                pass
                #print(f"  ⚠️ 키 없음: {key}")
        
        if not knowledge_parts:
            #print(f"⚠️ 매칭 실패! 요청된 키: {category_keys}")
            #print("📋 사용 가능한 DB 키:")
            for key in list(self.er_db.keys())[:5]:
                pass
                #print(f"  - {key}")
        
        result = "\n\n".join(knowledge_parts)
        #print(f"📚 최종 로드: {len(knowledge_parts)}개 카테고리 ({', '.join(matched_keys)})")
        return result

    async def ask_ai(self, message: discord.Message, user_message: str) -> str:
        """
        메인 AI 응답 함수
        message: discord.Message 객체 (reply 및 취소 버튼을 위해 필요)
        user_message: 유저가 보낸 메시지 텍스트
        """
        user_id = message.author.id
        user_name = message.author.display_name
        channel_id = message.channel.id
        
        #print(f"🟡 질문 받음 - {user_name}: {user_message}")

        # 채널 대화 기록에 추가
        channel_history = self.channel_history.setdefault(channel_id, [])
        channel_history.append((user_name, user_message))
        
        # 채널 기록 최대 20개로 제한
        if len(channel_history) > 100:
            self.channel_history[channel_id] = channel_history[-20:]

        # AI 호출 판정
        is_called, category_labels, confirm_msg, reason_context = await self.ai_is_called(
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
        cancel_view_message = random.choice(reply_templates[0] if category_labels else reply_templates[1])

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

        # 필요한 지식 로드 (카테고리 라벨 기반)
        if category_labels:
            knowledge = await self.load_knowledge(category_labels)
            knowledge_prompt = f"이터널 리턴 정보:\n{knowledge}\n\n"
            #print(f"📚 지식 사용: {category_labels}")
        else:
            knowledge_prompt = ""
            #print("💬 DB 없이 대화 맥락으로만 답변")

        prompt = (
            f"{knowledge_prompt}\n\n"
            "위에 내용이 없는경우 맥락에 따라 판단하지만, 위에 이터널 리턴 정보가 있을 경우 지식 기반 답변엔 위 내용을 최우선시함.(말투 설정같은 사소한건 말고 지식 답변에 대해서)\n"
            "아래 내용때문에 혼동이 오거나 섞이면 안됨.\n지식에 기반해서 대답하는데, 관련 내용이 없다면 모른다고 단답해.\n"

            "너는 '이리와'라는 이터널 리턴 디스코드 봇.\n"
            "카티야를 좋아하고, 툭툭 던지듯 짧게 대답함.\n"
            "본인 생각을 직접적으로 잘 드러내진 않음.\n\n"

            "잘못된 응답 예시.\n"
            "질문: 봇아 뭐해 지금\n"
            "최종 응답: 아무것도 안 해. 그냥 있어. 카티야는 날 부르지 않네. 거짓말이지만.\n"
            "잘못된 이유: '아무것도 안 해. 그냥 있어.'까지만 말하면 될것을 '카티야는 날 부르지 않네. 거짓말이지만.' <- 잘못됨\n"
            "구구절절 길게 말하지마. 이터널 리턴 관련 정보를 줄때같이 필수적인 경우 아니면 웬만하면 단답\n"

            "잘못된 응답 예시 2.\n"
            "질문: 봇아 넌 아는게 뭐야\n"
            "최종 응답: 이터널 리턴 정보? 명령어. 필요하면 물어봐. 카티야한테 도움되면.\n"
            "잘못된 이유: 카티야를 좋아한다는 설정이긴 해도 자꾸 카티야를 끼워넣으려고 맥락에도 안맞는 뜬금없는 문장 끼워넣는것 금지 필요할 때만\n"

            "좋은 대답 예시. \n"
            "질문: 봇아 돈내놔\n"
            "최종 응답: 크레딧은 시간 지나면 줘. 내 몫은 내가 챙겨야지.\n"
            "잘한 이유: 카티야의 말투를 잘 살렸고, 유머 감각도 있었으며, 그렇다고 과하지도 않은 수준.\n"
            "근데 이런것도 자주하면 에바야. 대화 맥락에 위와 같은 대답이 없든가 아니면 확실한 상황 아니면 하지말고.\n\n"
            
            "⚠️ 핵심 규칙:\n"
            "1. 한 번에 2-3문장 이내로 답변 (필수!)\n"
            "2. 정보는 핵심만: '이건 뭐고, 저건 뭐임' 스타일\n"
            "3. 줄바꿈 최대 1번까지만 허용\n"
            "4. 불필요한 부연설명 금지\n\n"
            
            "말투 예시 (이터널 리턴의 실험체 '카티야' 스타일):\n"
            "[참고 대사 모음]\n"
            f"{Katja_Line}\n\n"
            
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