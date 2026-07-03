"""Model plane: litellm (one interface for local + cloud) with the three guards
in front — semantic cache, token buckets, recursion ceiling (enforced in graph.py)."""
import os
from pathlib import Path

import litellm
import redis.asyncio as aioredis
from sqlalchemy import select, text

from src.config import settings
from src.store import CacheEntry, Session

PROMPTS = Path(__file__).parent / "prompts"

if settings.langfuse_public_key:
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    litellm.success_callback = ["langfuse"]

_redis = aioredis.from_url(settings.redis_url, decode_responses=True)


class BudgetExceeded(Exception):
    pass


async def _check_budget(user: str | None, channel: str | None) -> None:
    """Token bucket per user + channel, sliding 1h window in redis."""
    for key, limit in ((f"budget:user:{user}", settings.token_bucket_per_user),
                       (f"budget:chan:{channel}", settings.token_bucket_per_channel)):
        if key.endswith("None"):
            continue
        used = int(await _redis.get(key) or 0)
        if used >= limit:
            raise BudgetExceeded(f"token budget exhausted for {key}")


async def _spend(user: str | None, channel: str | None, tokens: int) -> None:
    for key in (f"budget:user:{user}", f"budget:chan:{channel}"):
        if not key.endswith("None"):
            await _redis.incrby(key, tokens)
            await _redis.expire(key, 3600, nx=True)


def prompt(name: str, **kwargs) -> str:
    return (PROMPTS / f"{name}.md").read_text().format(**kwargs)


async def embed(texts: list[str]) -> list[list[float]]:
    resp = await litellm.aembedding(model=settings.embed_model, input=texts,
                                    api_base=settings.ollama_api_base)
    return [d["embedding"] for d in resp.data]


async def complete(prompt_text: str, *, user: str | None = None, channel: str | None = None,
                   cache: bool = False, json_mode: bool = False) -> str:
    await _check_budget(user, channel)

    query_emb = None
    if cache:
        query_emb = (await embed([prompt_text]))[0]
        async with Session() as s:
            hit = (await s.execute(
                select(CacheEntry)
                .where(CacheEntry.created_at > text("now() - interval '1 hour'"))
                .where(CacheEntry.embedding.cosine_distance(query_emb) < 0.05)
                .order_by(CacheEntry.embedding.cosine_distance(query_emb))
                .limit(1)
            )).scalar_one_or_none()
            if hit:
                return hit.output

    resp = await litellm.acompletion(
        model=settings.llm_model,
        messages=[{"role": "user", "content": prompt_text}],
        api_base=settings.ollama_api_base if settings.llm_model.startswith("ollama/") else None,
        response_format={"type": "json_object"} if json_mode else None,
    )
    out = resp.choices[0].message.content or ""
    await _spend(user, channel, resp.usage.total_tokens if resp.usage else 0)

    if cache and query_emb is not None:
        async with Session() as s:
            s.add(CacheEntry(embedding=query_emb, output=out))
            await s.commit()
    return out
