"""Seed the local mirror with fake Slack activity for a demo — no real workspace needed.
Pushes events through the real ingestion path (Redis Stream → worker → Postgres).

Usage: docker compose up -d && uv run python -m scripts.seed_demo_workspace
Then run `make dev` (worker consumes the stream) or call ingestion.run_worker here directly.
"""
import asyncio
import json
import time
from pathlib import Path

from src.ingestion import STREAM, _persist, _redis
from src.store import init_db

DEMO_CHANNEL = "C_DEMO"
THREADS = json.loads((Path(__file__).parent.parent / "evals" / "dataset.json").read_text())


async def main() -> None:
    await init_db()
    ts = time.time()
    for thread in THREADS:
        root_ts = f"{ts:.6f}"
        for i, (user, text) in enumerate(thread["messages"]):
            ts += 1
            event = {"channel": DEMO_CHANNEL, "ts": f"{ts:.6f}", "user": user, "text": text,
                     "thread_ts": root_ts if i else None}
            await _persist({k: v for k, v in event.items() if v is not None})
    await _redis.aclose()
    print(f"seeded {sum(len(t['messages']) for t in THREADS)} messages into channel {DEMO_CHANNEL}")
    print(f"try: /summarize in {DEMO_CHANNEL}, or query the messages table directly")


if __name__ == "__main__":
    asyncio.run(main())
