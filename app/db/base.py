from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.database_url,
    # A connection pool exists because opening a Postgres connection costs
    # ~1-5ms and a real process. Under 500 concurrent requests you do NOT want
    # 500 connections — Postgres degrades badly past a few hundred. You want a
    # small pool that requests queue for.
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,   # drop connections killed by a network/idle timeout
    echo=False,
)

SessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,   # keep ORM objects usable after commit()
    autoflush=False,          # flush only where we say so; makes ordering explicit
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session (and one transaction) per request."""
    async with SessionLocal() as session:
        yield session
