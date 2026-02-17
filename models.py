# models.py
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from db import Base

class User(Base):
    """디스코드 유저 정보"""
    __tablename__ = "users"
    
    id = Column(String, primary_key=True)  # Discord User ID
    active_er_nickname = Column(String, nullable=True)  # 활성화된 이터널 리턴 닉네임
    
    # 관계 설정
    er_accounts = relationship("ERAccount", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User(id={self.id}, active_er_nickname={self.active_er_nickname})>"

class ERAccount(Base):
    """이터널 리턴 계정 정보"""
    __tablename__ = "er_accounts"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)  # Discord User ID
    nickname = Column(String, nullable=False, unique=False)  # 이터널 리턴 닉네임
    registered_at = Column(DateTime, default=datetime.now)  # 등록 일시
    
    # 관계 설정
    user = relationship("User", back_populates="er_accounts")
    
    def __repr__(self):
        return f"<ERAccount(nickname={self.nickname}, user_id={self.user_id})>"