"""Data plane: messages mirrored from Slack + pgvector embeddings, one Postgres DB."""
import datetime as dt

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, String, Text, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config import settings

engine = create_async_engine(settings.database_url)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Message(Base):
    __tablename__ = "messages"
    # Slack ts is unique per channel — natural composite key
    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_ts: Mapped[str | None] = mapped_column(String(32), index=True)
    user: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embed_dim), nullable=True)

    __table_args__ = (Index("ix_messages_channel_thread", "channel", "thread_ts"),)


class CacheEntry(Base):
    """Semantic cache: embedding-keyed lookup of recent LLM outputs."""
    __tablename__ = "llm_cache"
    id: Mapped[int] = mapped_column(primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embed_dim))
    output: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    # ponytail: create_all instead of alembic — add migrations when the schema actually changes in prod


async def upsert_message(session: AsyncSession, m: Message) -> None:
    await session.merge(m)


async def thread_messages(channel: str, thread_ts: str) -> list[Message]:
    """Reconstruct a thread locally — never calls Slack."""
    async with Session() as s:
        rows = await s.execute(
            select(Message)
            .where(Message.channel == channel,
                   (Message.thread_ts == thread_ts) | (Message.ts == thread_ts))
            .order_by(Message.ts)
        )
        return list(rows.scalars())


async def channel_messages(channel: str, since: dt.datetime) -> list[Message]:
    async with Session() as s:
        rows = await s.execute(
            select(Message)
            .where(Message.channel == channel, Message.created_at >= since)
            .order_by(Message.ts)
        )
        return list(rows.scalars())


async def similar_messages(embedding: list[float], channel: str | None = None, limit: int = 20) -> list[Message]:
    async with Session() as s:
        q = select(Message).where(Message.embedding.isnot(None))
        if channel:
            q = q.where(Message.channel == channel)
        q = q.order_by(Message.embedding.cosine_distance(embedding)).limit(limit)
        return list((await s.execute(q)).scalars())
