# cogs/help.py
import discord
from discord.ext import commands
from datetime import datetime


HELP_PAGES = {
    "유저 등록": {
        "emoji": "📋",
        "color": 0x0fb9b1,
        "fields": [
            {
                "name": "📌 `ㅇ등록 [닉네임]`",
                "value": "```닉네임을 봇에 등록합니다.\n이후 명령어에서 닉네임 생략 가능```",
            },
            {
                "name": "🗑️ `ㅇ삭제`",
                "value": "```등록된 닉네임을 삭제합니다```",
            },
        ],
    },
    "전적 검색": {
        "emoji": "🎮",
        "color": 0x5865F2,
        "fields": [
            {
                "name": "📊 `ㅇ전적 [닉네임]` / `ㅇㅈㅈ`",
                "value": "```전체 전적 정보를 검색합니다```",
            },
            {
                "name": "🏆 `ㅇ랭크 [닉네임]` / `ㅇㄹㅋ`",
                "value": "```랭크 티어 및 LP 정보를 검색합니다```",
            },
            {
                "name": "⚡ `ㅇ최근게임 [닉네임]` / `ㅇㅊㄱㄱ`",
                "value": "```가장 최근 게임의 전적을 검색합니다```",
            },
        ],
    },
    "기타": {
        "emoji": "⚙️",
        "color": 0xEB459E,
        "fields": [
            {
                "name": "❓ `ㅇ도움` / `ㅇㄷㅇ`",
                "value": "```이 도움말을 표시합니다```",
            },
        ],
    },
}


def build_overview_embed(bot: commands.Bot) -> discord.Embed:
    embed = discord.Embed(
        title="<:iriwha:1> 이리와 봇 도움말",
        description=(
            "아래 **드롭다운 메뉴**에서 카테고리를 선택해 명령어를 확인하세요.\n"
            "단축 명령어(`ㅇㅈㅈ`, `ㅇㄹㅋ` 등)도 동일하게 작동합니다."
        ),
        color=0x0fb9b1,
        timestamp=datetime.now(),
    )
    embed.add_field(
        name="📋 유저 등록",
        value="`ㅇ등록` `ㅇ삭제`",
        inline=True,
    )
    embed.add_field(
        name="🎮 전적 검색",
        value="`ㅇ전적` `ㅇ랭크` `ㅇ최근게임`",
        inline=True,
    )
    embed.add_field(
        name="⚙️ 기타",
        value="`ㅇ도움`",
        inline=True,
    )
    embed.set_footer(
        text=f"이리와 봇 | 명령어 접두사: ㅇ",
        icon_url=bot.user.display_avatar.url if bot.user else None,
    )
    return embed


def build_category_embed(category: str) -> discord.Embed:
    data = HELP_PAGES[category]
    embed = discord.Embed(
        title=f"{data['emoji']}  {category}",
        color=data["color"],
        timestamp=datetime.now(),
    )
    for field in data["fields"]:
        embed.add_field(name=field["name"], value=field["value"], inline=False)
    embed.set_footer(text="이리와 봇 | ← 다른 카테고리는 드롭다운에서 선택")
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=category,
                description=f"{data['emoji']} {category} 관련 명령어 보기",
                emoji=data["emoji"],
                value=category,
            )
            for category, data in HELP_PAGES.items()
        ]
        options.insert(
            0,
            discord.SelectOption(
                label="전체 보기",
                description="모든 카테고리 한눈에 보기",
                emoji="🏠",
                value="overview",
            ),
        )
        super().__init__(
            placeholder="📂  카테고리를 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "overview":
            embed = build_overview_embed(interaction.client)
        else:
            embed = build_category_embed(selected)
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(HelpSelect())

    async def on_timeout(self):
        # 타임아웃 시 드롭다운 비활성화
        for item in self.children:
            item.disabled = True


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="도움", aliases=["ㄷㅇ"])
    async def record_help(self, ctx: commands.Context):
        embed = build_overview_embed(self.bot)
        view = HelpView()
        msg = await ctx.reply(embed=embed, view=view)

        # 타임아웃 후 메시지 업데이트 (드롭다운 비활성화)
        await view.wait()
        try:
            for item in view.children:
                item.disabled = True
            await msg.edit(view=view)
        except discord.NotFound:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))