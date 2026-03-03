# cogs/router.py
import discord
from discord.ext import commands
from datetime import datetime

from db import SessionLocal
from models import GuildConfig

TEST_CHENNEL_ID = 1474687636940787753

class MessageRouter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────────────────
    # 헬퍼: 서버에 설정된 봇 채널 ID 조회
    #   - 채널이 실제로 존재하지 않으면 DB에서 자동 삭제 후 None 반환
    # ──────────────────────────────────────────────────────
    def _get_valid_bot_channel(self, guild: discord.Guild) -> int | None:
        session = SessionLocal()
        try:
            config = session.get(GuildConfig, str(guild.id))
            if config is None or config.bot_channel_id is None:
                return None

            channel_id = int(config.bot_channel_id)

            # 채널이 실제로 서버에 존재하는지 확인
            if guild.get_channel(channel_id) is None:
                config.bot_channel_id = None
                session.commit()
                return None

            return channel_id

        except Exception:
            session.rollback()
            return None
        finally:
            session.close()

    # ──────────────────────────────────────────────────────
    # 메시지 라우팅
    # ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # DM은 제한 없이 처리
        if message.guild is None:
            await self.bot.process_commands(message)
            return
        
        if message.channel.id == TEST_CHENNEL_ID:
            await self.bot.process_commands(message)
            return

        bot_channel_id = self._get_valid_bot_channel(message.guild)

        # 봇 채널이 설정돼 있고, 현재 채널이 아니면 무시
        if bot_channel_id is not None and message.channel.id != bot_channel_id:
            return

        await self.bot.process_commands(message)

    # ──────────────────────────────────────────────────────
    # ㅇ봇채널설정 <#채널>
    # ──────────────────────────────────────────────────────
    @commands.command(name="봇채널설정")
    @commands.has_permissions(administrator=True)
    async def set_bot_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if channel.guild.id != ctx.guild.id:
            return await ctx.reply("❌ 해당 서버에 존재하는 채널만 설정할 수 있습니다.")
        
        if not channel:
            return await ctx.reply(embed=discord.Embed(
                title="❌ 오류",
                description="`ㅇ봇채널설정 #채널명` 형식으로 채널을 입력해주세요.",
                color=0xFF6B6B,
            ))

        session = SessionLocal()
        try:
            config = session.get(GuildConfig, str(ctx.guild.id))
            if config is None:
                config = GuildConfig(
                    guild_id=str(ctx.guild.id),
                    bot_channel_id=str(channel.id),
                    set_at=datetime.now()
                )
                session.add(config)
            else:
                config.bot_channel_id = str(channel.id)
                config.set_at = datetime.now()
            session.commit()

            embed = discord.Embed(
                title="봇 채널 설정 완료",
                description=f"{channel.mention} 채널에서만 봇 명령어를 사용할 수 있습니다.",
                color=0x0fb9b1,
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"{ctx.author.display_name} | 봇 채널 설정", icon_url=ctx.author.display_avatar.url)
            await ctx.reply(embed=embed)

        except Exception as e:
            session.rollback()
            await ctx.reply(f"설정 중 오류가 발생했습니다: {e}")
        finally:
            session.close()

    # ──────────────────────────────────────────────────────
    # ㅇ봇채널제거
    # ──────────────────────────────────────────────────────
    @commands.command(name="봇채널제거")
    @commands.has_permissions(administrator=True)
    async def remove_bot_channel(self, ctx: commands.Context):
        session = SessionLocal()
        try:
            config = session.get(GuildConfig, str(ctx.guild.id))
            if config is None or config.bot_channel_id is None:
                return await ctx.reply("❌ 설정된 봇 채널이 없습니다.")

            config.bot_channel_id = None
            session.commit()

            embed = discord.Embed(
                title="봇 채널 제거 완료",
                description="봇 채널 설정이 해제되었습니다.\n이제 **모든 채널**에서 봇 명령어를 사용할 수 있습니다.",
                color=0xff6b6b,
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"{ctx.author.display_name} | 봇 채널 제거", icon_url=ctx.author.display_avatar.url)
            await ctx.reply(embed=embed)

        except Exception as e:
            session.rollback()
            await ctx.reply(f"제거 중 오류가 발생했습니다: {e}")
        finally:
            session.close()

    # ──────────────────────────────────────────────────────
    # 에러 핸들링
    # ──────────────────────────────────────────────────────
    @set_bot_channel.error
    @remove_bot_channel.error
    async def channel_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply(embed=discord.Embed(
                title="❌ 오류",
                description="관리자 권한이 필요합니다.",
                color=0xFF6B6B,
            ))
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(embed=discord.Embed(
                title="❌ 오류",
                description="`ㅇ봇채널설정 #채널명` 형식으로 채널을 입력해주세요.",
                color=0xFF6B6B,
            ))
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(embed=discord.Embed(
                title="❌ 오류",
                description="`ㅇ봇채널설정 #채널명` 형식으로 채널명을 입력해주세요.",
                color=0xFF6B6B,
            ))
        else:
            await ctx.reply(embed=discord.Embed(
                title="❌ 오류",
                description=f"기타 에러: {error}",
                color=0xFF6B6B,
            ))


async def setup(bot):
    await bot.add_cog(MessageRouter(bot))