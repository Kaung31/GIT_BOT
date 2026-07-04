"""Model plane: litellm with per-agent routing (Sonnet writes, Haiku critiques on demo day;
all Ollama during development) plus the guards — semantic cache, per-run token bucket,
hard daily USD spend cap. Recursion ceiling lives in graph.py.

Prompt caching: each prompt splits at ===VARIABLE=== into a STABLE prefix (role + rules + repo
context + issue — identical across revise rounds) and a VARIABLE suffix (patch/findings/test).
For Anthropic the stable prefix carries cache_control so round-2 calls read it at 90% off; for
Ollama it's a plain system message (cache_control is Anthropic-only)."""
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
CACHE_SPLIT = "===VARIABLE==="

# Anthropic silently skips prompt caching if the cached prefix is below a per-model minimum.
CACHE_MINIMUMS = {"claude-sonnet-4-6": 1024, "claude-haiku-4-5": 4096}
DEFAULT_ANTHROPIC_MIN = 1024

if settings.langfuse_public_key:
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    litellm.success_callback = ["langfuse"]

if settings.anthropic_api_key:  # so a key in .env actually reaches litellm
    os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)

_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
_cache_warned: set = set()


def estimate_tokens(text: str) -> int:
    """Cheap offline estimate (~4 chars/token, word-count floor). Enough to gate on the
    caching minimum without an API round-trip."""
    return max(len(text) // 4, len(text.split()))


def caching_minimum(model: str) -> int | None:
    """Min cached-prefix tokens for this model to cache at all; None for non-Anthropic (no cache)."""
    if not model.startswith("anthropic/"):
        return None
    name = model.split("/", 1)[1]
    return next((m for k, m in CACHE_MINIMUMS.items() if k in name), DEFAULT_ANTHROPIC_MIN)


def prefix_caches(model: str, system: str) -> bool | None:
    """True/False whether the stable prefix clears the model's cache minimum; None if N/A (local)."""
    minimum = caching_minimum(model)
    return None if minimum is None else estimate_tokens(system) >= minimum


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


async def _record(run_id: str, agent: str, resp) -> None:
    u = resp.usage
    tokens = u.total_tokens if u else 0
    await _redis.incrby(f"budget:run:{run_id}", tokens)
    await _redis.expire(f"budget:run:{run_id}", 86400, nx=True)
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    try:
        cost = litellm.completion_cost(resp) or 0.0
    except Exception:
        cost = 0.0  # local models have no price
    log.info("llm %s: %d tok, cache_read=%d cache_write=%d, $%.4f",
             agent, tokens, cache_read, cache_write, cost)
    if cost:
        await _redis.incrbyfloat(_spend_key(), cost)
        await _redis.expire(_spend_key(), 86400 * 2, nx=True)


def prompt(name: str, **kwargs) -> tuple[str, str]:
    """Return (stable_prefix, variable_suffix) split on the ===VARIABLE=== marker."""
    text_ = (PROMPTS / f"{name}.md").read_text().format(**kwargs)
    stable, _, variable = text_.partition(CACHE_SPLIT)
    return stable.strip(), variable.strip()


def _messages(model: str, system: str, user: str) -> list[dict]:
    if model.startswith("anthropic/"):
        # cache_control marks the stable prefix as a reusable cache breakpoint
        return [
            {"role": "system", "content": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": user or "Proceed."},
        ]
    return [{"role": "system", "content": system},
            {"role": "user", "content": user or "Proceed."}]


async def embed(texts: list[str]) -> list[list[float]]:
    resp = await litellm.aembedding(model=settings.embed_model, input=texts,
                                    api_base=settings.ollama_api_base)
    return [d["embedding"] for d in resp.data]


def _warn_if_prefix_too_small(model: str, system: str, agent: str) -> None:
    if prefix_caches(model, system) is False and (agent, model) not in _cache_warned:
        _cache_warned.add((agent, model))
        log.warning("prompt caching DISABLED for %s: stable prefix ~%d tok < %d min for %s — "
                    "raise %s_CONTEXT_CHUNKS to cache", agent, estimate_tokens(system),
                    caching_minimum(model), model, agent.upper())


async def complete(model: str, system: str, user: str, *, run_id: str, agent: str = "",
                   cache: bool = False, json_mode: bool = False) -> str:
    await _check_budget(run_id)
    _warn_if_prefix_too_small(model, system, agent)

    query_emb = None
    if cache:
        query_emb = (await embed([system + user]))[0]
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
        messages=_messages(model, system, user),
        api_base=settings.ollama_api_base if model.startswith("ollama/") else None,
        response_format={"type": "json_object"} if json_mode else None,
        metadata={"trace_id": run_id, "trace_name": "swarm-run",
                  "generation_name": agent or "llm", "tags": [agent, model.split("/")[-1]]},
    )
    out = resp.choices[0].message.content or ""
    await _record(run_id, agent, resp)

    if cache and query_emb is not None:
        async with Session() as s:
            s.add(CacheEntry(embedding=query_emb, output=out))
            await s.commit()
    return out
