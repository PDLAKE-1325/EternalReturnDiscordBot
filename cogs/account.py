# cogs/er_account.py
import discord
from discord.ext import commands
from sqlalchemy.orm import Session
from datetime import datetime

from db import SessionLocal
from models import User, ERAccount

class ERAccountCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────
    # 닉네임 등록
    # ──────────────────────────────
    @commands.command(name="등록")
    async def register_nickname(self, ctx, *, nickname: str):
        user_id = str(ctx.author.id)
        session = SessionLocal()

        try:
            # 유저 조회 또는 생성
            user = session.get(User, user_id)
            if user is None:
                user = User(id=user_id)
                session.add(user)
                session.flush()

            # 이미 등록된 계정이 있는지 확인
            existing = session.query(ERAccount).filter(ERAccount.user_id == user_id).first()

            if existing:
                # 기존 닉네임 업데이트
                old_nickname = existing.nickname
                existing.nickname = nickname
                existing.registered_at = datetime.now()
                user.active_er_nickname = nickname
                session.commit()
                
                embed = discord.Embed(
                    title="닉네임 변경 완료",
                    description=f"**{old_nickname}** → **{nickname}**",
                    color=0x0fb9b1,
                    timestamp=datetime.now()
                )
                embed.set_footer(text=f"{ctx.author.display_name} | 닉네임 변경", icon_url=ctx.author.display_avatar.url)
                
                await ctx.reply(embed=embed)
                return

            # 새 계정 등록
            new_account = ERAccount(
                user_id=user_id,
                nickname=nickname,
                registered_at=datetime.now()
            )
            session.add(new_account)
            user.active_er_nickname = nickname
            session.commit()

            embed = discord.Embed(
                title="닉네임 등록 완료",
                description=f"**{nickname}** 님의 전적 검색이 간편해집니다!",
                color=0x00ff00,
                timestamp=datetime.now()
            )
            embed.add_field(
                name="자동 검색 기능 활성화",
                value=(
                    "이제 명령어 입력시 닉네임을 생략하면\n"
                    "자동으로 등록된 닉네임으로 검색됩니다!"
                ),
                inline=False
            )
            embed.set_footer(text=f"{ctx.author.display_name} | 닉네임 등록", icon_url=ctx.author.display_avatar.url)
            
            await ctx.reply(embed=embed)

        except Exception as e:
            session.rollback()
            #print(f"[ERROR] 닉네임 등록 중 오류: {e}")
            await ctx.reply(f"등록 중 오류가 발생했습니다: {e}")
        finally:
            session.close()

    # ──────────────────────────────
    # 닉네임 삭제
    # ──────────────────────────────
    @commands.command(name="삭제")
    async def delete_nickname(self, ctx):
        """등록된 닉네임 삭제"""
        user_id = str(ctx.author.id)
        session = SessionLocal()

        try:
            account = session.query(ERAccount).filter(ERAccount.user_id == user_id).first()

            if not account:
                return await ctx.reply("❌ 등록된 닉네임이 없습니다.")

            nickname = account.nickname
            
            # 삭제
            session.delete(account)

            # 활성 닉네임 비활성화
            user = session.get(User, user_id)
            if user:
                user.active_er_nickname = None

            session.commit()

            embed = discord.Embed(
                title="닉네임 삭제 완료",
                description=f"**{nickname}** 닉네임을 삭제했습니다.",
                color=0xff6b6b,
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"{ctx.author.display_name} | 닉네임 삭제", icon_url=ctx.author.display_avatar.url)

            await ctx.reply(embed=embed)

        except Exception as e:
            session.rollback()
            #print(f"[ERROR] 닉네임 삭제 중 오류: {e}")
            await ctx.reply(f"삭제 중 오류가 발생했습니다: {e}")
        finally:
            session.close()

async def setup(bot: commands.Bot):
    await bot.add_cog(ERAccountCog(bot))