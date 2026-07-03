"""Ingestion plane: Slack events → Redis Stream → async worker → Postgres+pgvector.
Decoupled so a slow embed never blocks Slack's 3s ack, and a worker crash replays
from the consumer-group offset."""
import asyncio
import json
import logging

import redis.asyncio as aioredis

from src.config import settings
from src.llm import embed
from src.store import Message, Session, upsert_message

log = logging.getLogger(__name__)
STREAM, GROUP = "slack:events", "ingest"
_redis = aioredis.from_url(settings.redis_url, decode_responses=True)


async def push_event(event: dict) -> None:
    """Called from the Bolt listener — fast, no I/O beyond one redis XADD."""
    await _redis.xadd(STREAM, {"json": json.dumps(event)}, maxlen=100_000)


async def _persist(event: dict) -> None:
    if event.get("subtype") or not event.get("text"):
        return  # skip edits/joins/bot-noise; ponytail: handle message_changed if stale summaries bite
    vec = None
    try:
        vec = (await embed([event["text"]]))[0]
    except Exception:
        log.exception("embed failed, storing without vector")
    async with Session() as s:
        await upsert_message(s, Message(
            channel=event["channel"], ts=event["ts"],
            thread_ts=event.get("thread_ts"), user=event.get("user"),
            text=event["text"], embedding=vec,
        ))
        await s.commit()


async def run_worker() -> None:
    try:
        await _redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except aioredis.ResponseError:
        pass  # group exists
    log.info("ingestion worker started")
    while True:
        batches = await _redis.xreadgroup(GROUP, "worker-1", {STREAM: ">"}, count=10, block=5000)
        for _, entries in batches or []:
            for entry_id, fields in entries:
                try:
                    await _persist(json.loads(fields["json"]))
                    await _redis.xack(STREAM, GROUP, entry_id)
                except Exception:
                    log.exception("persist failed for %s (left pending for replay)", entry_id)
                    await asyncio.sleep(1)
