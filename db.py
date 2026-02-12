# db.py
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# SQLite 데이터베이스 파일 경로
DATABASE_URL = "sqlite:///./bot_database.db"

# SQLAlchemy 엔진 생성
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}  # SQLite용 설정
)

# 세션 팩토리
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base 클래스 (모든 모델이 상속받을 기본 클래스)
Base = declarative_base()

# 데이터베이스 초기화 함수
def init_db():
    """데이터베이스 테이블 생성"""
    # models를 임포트해야 Base.metadata가 테이블 정보를 알 수 있음
    import models
    Base.metadata.create_all(bind=engine)
    print("✅ 데이터베이스 테이블 생성 완료 (users, er_accounts)")