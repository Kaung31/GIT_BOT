"""One-off history backfill: conversations.history + .replies, patient and rate-limited.
Run once per channel; live events keep the mirror fresh afterwards.

Usage: uv run python -m src.backfill C0123456789
"""
import asyncio
import logging
import sys

from slack_sdk.web.async_client import AsyncWebClient

from src.config import settings
from src.ingestion import push_event
from src.store import init_db

log = logging.getLogger(__name__)


async def backfill(channel: str) -> None:
    client = AsyncWebClient(token=settings.slack_bot_token)
    cursor, n = None, 0
    while True:
        resp = await client.conversations_history(channel=channel, cursor=cursor, limit=200)
        for msg in resp["messages"]:
            msg["channel"] = channel
            await push_event(msg)
            n += 1
            if msg.get("reply_count"):
                replies = await client.conversations_replies(channel=channel, ts=msg["ts"], limit=200)
                for r in replies["messages"][1:]:
                    r["channel"] = channel
                    await push_event(r)
                    n += 1
                await asyncio.sleep(1.2)  # be patient with .replies
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        await asyncio.sleep(1.2)
    log.info("queued %d messages from %s (worker will embed+persist)", n, channel)
    # ponytail: slack_sdk auto-retries 429s with Retry-After; no extra backoff code needed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(init_db())
    asyncio.run(backfill(sys.argv[1]))
