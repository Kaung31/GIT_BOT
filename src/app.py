"""Entrypoint: async Bolt (Socket Mode) + ingestion worker + standup scheduler."""
import asyncio
import json
import logging
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from langgraph.types import Command
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from src import ingestion
from src.config import settings
from src.graph import make_checkpointed_graph, run_config
from src.llm import BudgetExceeded
from src.store import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = AsyncApp(token=settings.slack_bot_token)
graph = None  # set in main()


def approval_blocks(preview: dict, thread_id: str) -> list[dict]:
    tasks = "\n".join(f"• *{t['title']}*" + (f" — {t['owner']}" if t.get("owner") else "")
                      for t in preview["tasks"])
    return [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f":clipboard: *{preview['action']}* ({len(preview['tasks'])} items)\n{tasks}"}},
        {"type": "actions", "block_id": "approval", "elements": [
            {"type": "button", "action_id": "approve", "style": "primary",
             "text": {"type": "plain_text", "text": "Approve"}, "value": thread_id},
            {"type": "button", "action_id": "reject", "style": "danger",
             "text": {"type": "plain_text", "text": "Reject"}, "value": thread_id},
        ]},
    ]


async def run_graph(state: dict, say, thread_ts: str | None = None) -> None:
    thread_id = str(uuid.uuid4())
    try:
        result = await graph.ainvoke(state, run_config(thread_id))
    except BudgetExceeded:
        await say(text=":no_entry: Token budget exhausted for this hour — try later.", thread_ts=thread_ts)
        return
    except Exception:
        log.exception("graph run failed")
        await say(text=":warning: Agent run failed — check logs.", thread_ts=thread_ts)
        return

    if result.get("__interrupt__"):
        preview = result["__interrupt__"][0].value["preview"]
        await say(blocks=approval_blocks(preview, thread_id),
                  text="Approval needed", thread_ts=thread_ts)
    elif state["goal"] == "summarize":
        await say(text=f":memo: *Summary*\n{result['summary']}", thread_ts=thread_ts)
    elif state["goal"] == "extract" and not result.get("action_items"):
        await say(text="No action items found.", thread_ts=thread_ts)
    elif state["goal"] == "standup":
        await say(text=f":sunrise: *Daily standup digest*\n{result['digest']}")


# --- events: mirror everything into the local store ---

@app.event("message")
async def on_message(event, ack):
    await ack()
    await ingestion.push_event(event)


@app.event("app_mention")
async def on_mention(event, say, ack):
    await ack()
    await run_graph({"goal": "summarize", "channel": event["channel"],
                     "thread_ts": event.get("thread_ts", event["ts"]), "user": event["user"]},
                    say, thread_ts=event.get("thread_ts", event["ts"]))


# --- slash commands ---

@app.command("/summarize")
async def cmd_summarize(command, say, ack):
    await ack("Summarizing from the local mirror…")
    await run_graph({"goal": "summarize", "channel": command["channel_id"],
                     "thread_ts": None, "user": command["user_id"]}, say)


@app.command("/extract")
async def cmd_extract(command, say, ack):
    await ack("Extracting action items…")
    target = "notion" if command.get("text", "").strip() == "notion" else "jira"
    await run_graph({"goal": "extract", "channel": command["channel_id"],
                     "thread_ts": None, "user": command["user_id"], "target": target}, say)


# --- approval buttons: resume the checkpointed graph ---

async def _resume(decision: str, body, say):
    thread_id = body["actions"][0]["value"]
    result = await graph.ainvoke(Command(resume=decision), run_config(thread_id))
    lines = result.get("write_results") or ["Done."]
    who = body["user"]["id"]
    await say(text=f"<@{who}> chose *{decision}*:\n" + "\n".join(lines))


@app.action("approve")
async def on_approve(ack, body, say):
    await ack()
    await _resume("approve", body, say)


@app.action("reject")
async def on_reject(ack, body, say):
    await ack()
    await _resume("reject", body, say)


# --- standup ---

async def standup_prompt():
    await app.client.chat_postMessage(
        channel=settings.standup_channel,
        text=":wave: *Standup time!* Reply in this channel: what you did, what's next, any blockers.")
    asyncio.get_event_loop().call_later(
        settings.standup_collect_minutes * 60,
        lambda: asyncio.create_task(standup_digest()))


async def standup_digest():
    async def say(**kw):
        await app.client.chat_postMessage(channel=settings.standup_channel, **kw)
    # ponytail: digest posts back to the same channel it read from — no external write, so no
    # interrupt gate; route via a leads channel + approval if the digest needs review first
    await run_graph({"goal": "standup", "channel": settings.standup_channel, "thread_ts": None}, say)


async def main():
    global graph
    await init_db()
    graph, _saver = await make_checkpointed_graph()

    if settings.standup_channel:
        sched = AsyncIOScheduler()
        sched.add_job(standup_prompt, CronTrigger.from_crontab(settings.standup_cron))
        sched.start()

    asyncio.create_task(ingestion.run_worker())
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    log.info("starting Socket Mode handler")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
