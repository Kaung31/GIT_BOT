"""Model plane: litellm with per-agent routing (Sonnet writes, Haiku critiques on demo day;
all Ollama during development) plus the guards — semantic cache, per-run token bucket,
hard daily USD spend cap. Recursion ceiling lives in graph.py."""
import datetime as dt
import logging
import os
from pathlib import Path

import litellm
import redis.asyncio as aioredis
from sqlalchemy import select, text

from src.config import settings
from src.store import CacheEntry, Session

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).parent / "prompts"

if settings.langfuse_public_key:
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    litellm.success_callback = ["langfuse"]

_redis = aioredis.from_url(settings.redis_url, decode_responses=True)


class BudgetExceeded(Exception):
    pass


async def _check_budget(run_id: str) -> None:
    used = int(await _redis.get(f"budget:run:{run_id}") or 0)
    if used >= settings.token_bucket_per_run:
        raise BudgetExceeded(f"token bucket exhausted for run {run_id}")
    spent = float(await _redis.get(_spend_key()) or 0)
    if spent >= settings.daily_spend_cap_usd:
        raise BudgetExceeded(f"daily spend cap ${settings.daily_spend_cap_usd} reached")


def _spend_key() -> str:
    return f"spend:usd:{dt.date.today().isoformat()}"


async def _record(run_id: str, resp) -> None:
    tokens = resp.usage.total_tokens if resp.usage else 0
    await _redis.incrby(f"budget:run:{run_id}", tokens)
    await _redis.expire(f"budget:run:{run_id}", 86400, nx=True)
    try:
        cost = litellm.completion_cost(resp) or 0.0
    except Exception:
        cost = 0.0  # local models have no price
    if cost:
        await _redis.incrbyfloat(_spend_key(), cost)
        await _redis.expire(_spend_key(), 86400 * 2, nx=True)


def prompt(name: str, **kwargs) -> str:
    return (PROMPTS / f"{name}.md").read_text().format(**kwargs)


async def embed(texts: list[str]) -> list[list[float]]:
    resp = await litellm.aembedding(model=settings.embed_model, input=texts,
                                    api_base=settings.ollama_api_base)
    return [d["embedding"] for d in resp.data]


async def complete(model: str, prompt_text: str, *, run_id: str,
                   cache: bool = False, json_mode: bool = False) -> str:
    await _check_budget(run_id)

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
        model=model,
        messages=[{"role": "user", "content": prompt_text}],
        api_base=settings.ollama_api_base if model.startswith("ollama/") else None,
        response_format={"type": "json_object"} if json_mode else None,
    )
    out = resp.choices[0].message.content or ""
    await _record(run_id, resp)

    if cache and query_emb is not None:
        async with Session() as s:
            s.add(CacheEntry(embedding=query_emb, output=out))
            await s.commit()
    return out
