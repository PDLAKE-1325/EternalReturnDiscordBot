# cogs/help.py
import discord
from discord.ext import commands
from datetime import datetime


PAGES = {
    "유저 등록": {
        "emoji": "📋",
        "color": 0x0fb9b1,
        "commands": [
            ("ㅇ등록 [닉네임]", "닉네임을 봇에 등록합니다. 이후 명령어에서 [닉네임] 생략 가능", ""),
            ("ㅇ삭제",          "등록된 닉네임을 삭제합니다", ""),
        ],
    },
    "전적 검색": {
        "emoji": "🎮",
        "color": 0x5865F2,
        "commands": [
            ("ㅇ전적 [닉네임]",     "전체 전적 정보 조회", "\n> 단축: ㅇㅈㅈ"),
            ("ㅇ랭크 [닉네임]",     "랭크 게임 정보 조회", "\n> 단축: ㅇㄹㅋ"),
            ("ㅇ최근게임 [닉네임]", "마지막 게임 전적 조회", "\n> 단축: ㅇㅊㄱㄱ"),
        ],
    },
    "기타": {
        "emoji": "⚙️",
        "color": 0xEB459E,
        "commands": [
            ("ㅇ도움", "이 도움말을 표시합니다", "\n> 단축: ㅇㄷㅇ"),
        ],
    },
}


# ── 임베드 빌더 ───────────────────────────────────────────────

def build_main_embed(bot_user) -> discord.Embed:
    embed = discord.Embed(
        title="이리와 봇 도움말",
        description="아래 버튼을 눌러 카테고리별 명령어 상세 정보를 확인하세요.",
        color=0xff4700,
        timestamp=datetime.now(),
    )
    for name, data in PAGES.items():
        embed.add_field(
            name=f"{data['emoji']}  {name}",
            value="\n".join(f"`{cmd}`" for cmd, _, __ in data["commands"]),
            inline=False,
        )
    embed.set_footer(
        text="이리와 봇 · 도움말",
        icon_url=bot_user.display_avatar.url if bot_user else None,
    )
    return embed


def build_detail_embed(category: str, bot_user) -> discord.Embed:
    data = PAGES[category]
    lines = "\n".join(
        f"`{cmd}`\n> {desc}{short}" for cmd, desc, short in data["commands"]
    )
    embed = discord.Embed(
        title=f"{data['emoji']}  {category}",
        description=lines,
        color=data["color"],
        timestamp=datetime.now(),
    )
    embed.set_footer(
        text=f"이리와 봇 · {category}",
        icon_url=bot_user.display_avatar.url if bot_user else None,
    )
    return embed


# ── 뷰 ───────────────────────────────────────────────────────

class MainView(discord.ui.View):
    """메인 화면: 카테고리 버튼 나열"""

    def __init__(self, bot_user):
        super().__init__(timeout=120)
        self.bot_user = bot_user

        for name, data in PAGES.items():
            self.add_item(CategoryButton(label=name, emoji=data["emoji"], bot_user=bot_user))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class CategoryButton(discord.ui.Button):
    def __init__(self, label: str, emoji: str, bot_user):
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary,
        )
        self.bot_user = bot_user

    async def callback(self, interaction: discord.Interaction):
        embed = build_detail_embed(self.label, self.bot_user)
        view = DetailView(category=self.label, bot_user=self.bot_user)
        await interaction.response.edit_message(embed=embed, view=view)


class DetailView(discord.ui.View):
    """상세 화면: 뒤로가기 버튼만 표시"""

    def __init__(self, category: str, bot_user):
        super().__init__(timeout=120)
        self.category = category
        self.bot_user = bot_user

    @discord.ui.button(label="뒤로가기", emoji="◀", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_main_embed(self.bot_user)
        view = MainView(self.bot_user)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ──────────────────────────────────────────────────────

class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="도움", aliases=["ㄷㅇ"])
    async def record_help(self, ctx: commands.Context):
        embed = build_main_embed(self.bot.user)
        view = MainView(self.bot.user)
        msg = await ctx.reply(embed=embed, view=view)

        await view.wait()
        try:
            for item in view.children:
                item.disabled = True
            await msg.edit(view=view)
        except discord.NotFound:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))