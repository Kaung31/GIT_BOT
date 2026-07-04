"""Data plane: local mirror of repo code (AST chunks + embeddings) in one Postgres DB."""
import datetime as dt

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, String, Text, delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config import settings

engine = create_async_engine(settings.database_url)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class CodeChunk(Base):
    __tablename__ = "code_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(200), index=True)
    path: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200))     # function/class name or path for non-py
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embed_dim), nullable=True)


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
    # ponytail: create_all instead of alembic — add migrations when the schema changes in prod


async def replace_file_chunks(repo: str, path: str, chunks: list[CodeChunk]) -> None:
    async with Session() as s:
        await s.execute(delete(CodeChunk).where(CodeChunk.repo == repo, CodeChunk.path == path))
        s.add_all(chunks)
        await s.commit()


async def similar_chunks(embedding: list[float], repo: str, limit: int = 8) -> list[CodeChunk]:
    async with Session() as s:
        q = (select(CodeChunk)
             .where(CodeChunk.repo == repo, CodeChunk.embedding.isnot(None))
             .order_by(CodeChunk.embedding.cosine_distance(embedding))
             .limit(limit))
        return list((await s.execute(q)).scalars())
