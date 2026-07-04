"""Prove Anthropic prompt caching is actually engaging on Haiku before you spend real money on
a full eval run. Makes TWO identical cheap Haiku calls with a >4096-token cached prefix and prints
the usage cache fields. Exit 0 only if the second call reads from cache.

- Refuses to run without ANTHROPIC_API_KEY.
- Respects DAILY_SPEND_CAP_USD (reuses the gateway's budget check + spend recorder).
- Costs ~$0.01–0.10. Run it ONCE before any paid eval run.

Usage: uv run python -m scripts.verify_caching
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import litellm

from src.config import settings
from src.llm import _check_budget, _messages, _record, caching_minimum, estimate_tokens

MODEL = "anthropic/claude-haiku-4-5"
SRC = Path(__file__).parent.parent / "src"


def _big_prefix() -> str:
    """A genuine, stable prefix (this project's own source) sized past Haiku's 4096-tok minimum."""
    text = "You are a code reviewer. Reference implementation follows.\n\n"
    for f in sorted(SRC.glob("*.py")):
        text += f"# === {f.name} ===\n{f.read_text()}\n\n"
        if estimate_tokens(text) > caching_minimum(MODEL) * 1.3:
            break
    return text


def _cache_tokens(resp) -> tuple[int, int]:
    u = resp.usage
    return (getattr(u, "cache_creation_input_tokens", 0) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0)


async def _call(prefix: str, run_id: str):
    await _check_budget(run_id)  # honors the daily spend cap + per-run token bucket
    resp = await litellm.acompletion(
        model=MODEL, messages=_messages(MODEL, prefix, "Reply with exactly: OK"), max_tokens=5)
    await _record(run_id, "verify_caching", resp)
    return resp


async def main() -> int:
    if not (os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key):
        print("ANTHROPIC_API_KEY is unset — refusing to run. Set it in .env or the environment.")
        return 2

    prefix = _big_prefix()
    run_id = f"verify-caching-{uuid.uuid4()}"
    print(f"prefix ~{estimate_tokens(prefix)} tok (min for {MODEL}: {caching_minimum(MODEL)})\n")

    c1, r1 = _cache_tokens(await _call(prefix, run_id))
    print(f"call 1: cache_creation_input_tokens={c1}  cache_read_input_tokens={r1}")
    c2, r2 = _cache_tokens(await _call(prefix, run_id))
    print(f"call 2: cache_creation_input_tokens={c2}  cache_read_input_tokens={r2}")

    if r2 > 0:
        print("\n✅ caching works — call 2 read the cached prefix (90% cheaper input)")
        return 0
    print("\n❌ call 2 read nothing from cache. Check: prefix under the minimum, cache_control "
          "not applied (is the model 'anthropic/...'?), or >5min between calls (TTL expired).")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
