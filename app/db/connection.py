from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.settings import settings


def create_db_engine() -> Engine:
    return create_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
    )


def create_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine)


def get_db(request: Request):
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
