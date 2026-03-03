# models.py
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from db import Base

class User(Base):
    """디스코드 유저 정보"""
    __tablename__ = "users"
    
    id = Column(String, primary_key=True)
    active_er_nickname = Column(String, nullable=True)
    
    er_accounts = relationship("ERAccount", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User(id={self.id}, active_er_nickname={self.active_er_nickname})>"

class ERAccount(Base):
    """이터널 리턴 계정 정보"""
    __tablename__ = "er_accounts"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    nickname = Column(String, nullable=False, unique=False)
    registered_at = Column(DateTime, default=datetime.now)
    
    user = relationship("User", back_populates="er_accounts")
    
    def __repr__(self):
        return f"<ERAccount(nickname={self.nickname}, user_id={self.user_id})>"

class GuildConfig(Base):
    """디스코드 서버별 봇 설정"""
    __tablename__ = "guild_configs"

    guild_id = Column(String, primary_key=True)        # Discord Guild ID
    bot_channel_id = Column(String, nullable=True)     # 봇 전용 채널 ID (None이면 전체 허용)
    set_at = Column(DateTime, default=datetime.now)    # 최종 설정 일시

    def __repr__(self):
        return f"<GuildConfig(guild_id={self.guild_id}, bot_channel_id={self.bot_channel_id})>"