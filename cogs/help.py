# cogs/help.py
import discord
from discord.ext import commands
from datetime import datetime


PAGES = [
    {
        "title": "📋  유저 등록",
        "color": 0x0fb9b1,
        "commands": [
            ("ㅇ등록 [닉네임]", "닉네임을 봇에 등록합니다. 이후 명령어에서 닉네임 생략 가능"),
            ("ㅇ삭제",          "등록된 닉네임을 삭제합니다"),
        ],
    },
    {
        "title": "🎮  전적 검색",
        "color": 0x5865F2,
        "commands": [
            ("ㅇ전적 [닉네임]",    "전체 전적 정보 조회  ·  단축: ㅇㅈㅈ"),
            ("ㅇ랭크 [닉네임]",    "랭크 티어 / LP 조회  ·  단축: ㅇㄹㅋ"),
            ("ㅇ최근게임 [닉네임]", "마지막 게임 전적 조회  ·  단축: ㅇㅊㄱㄱ"),
        ],
    },
    {
        "title": "⚙️  기타",
        "color": 0xEB459E,
        "commands": [
            ("ㅇ도움 / ㅇㄷㅇ", "이 도움말을 표시합니다"),
        ],
    },
]


def build_embed(page_idx: int, total: int, bot_user) -> discord.Embed:
    page = PAGES[page_idx]

    lines = "\n\n".join(
        f"`{cmd}`\n{desc}" for cmd, desc in page["commands"]
    )

    embed = discord.Embed(
        title=page["title"],
        description=lines,
        color=page["color"],
        timestamp=datetime.now(),
    )
    embed.set_footer(
        text=f"이리와 봇  ·  {page_idx + 1} / {total}",
        icon_url=bot_user.display_avatar.url if bot_user else None,
    )
    return embed


class HelpView(discord.ui.View):
    def __init__(self, bot_user, start: int = 0):
        super().__init__(timeout=120)
        self.bot_user = bot_user
        self.page = start
        self.total = len(PAGES)
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == self.total - 1
        self.page_indicator.label = PAGES[self.page]["title"].split("  ", 1)[-1]

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=build_embed(self.page, self.total, self.bot_user),
            view=self,
        )

    @discord.ui.button(label="—", style=discord.ButtonStyle.primary, disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=build_embed(self.page, self.total, self.bot_user),
            view=self,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="도움", aliases=["ㄷㅇ"])
    async def record_help(self, ctx: commands.Context):
        view = HelpView(self.bot.user, start=0)
        embed = build_embed(0, len(PAGES), self.bot.user)
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