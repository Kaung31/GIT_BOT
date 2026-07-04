"""Entrypoint: FastAPI (GitHub webhook) + ingestion worker + Telegram poller, one process.
Run: uv run uvicorn src.app:app --port 8000  (expose /webhooks/github via ngrok in dev)."""
import asyncio
import contextlib
import hashlib
import hmac
import logging
import uuid

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from langgraph.types import Command

from src import github_io, ingestion, telegram_io
from src.config import settings
from src.graph import make_checkpointed_graph, run_config
from src.llm import BudgetExceeded
from src.store import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

graph = None
_redis = aioredis.from_url(settings.redis_url, decode_responses=True)


async def start_swarm(state: dict) -> None:
    thread_id = str(uuid.uuid4())
    state["run_id"] = thread_id
    label = f"{state['repo']}#{state['issue']['number']} ({state['mode']})"
    await telegram_io.send_text(f"🐝 swarm run started: {label}")
    try:
        result = await graph.ainvoke(state, run_config(thread_id))
    except BudgetExceeded as e:
        await telegram_io.send_text(f"🛑 {label}: {e}")
        return
    except Exception:
        log.exception("swarm run failed")
        await telegram_io.send_text(f"⚠️ {label} crashed — check logs")
        return
    if result.get("__interrupt__"):
        await telegram_io.send_approval(thread_id, telegram_io.verdict_card(result))
    else:
        await telegram_io.send_text(f"{label}: {result.get('result', 'done')}")


async def on_trigger(kind: str, payload: dict) -> None:
    repo = payload["repository"]["full_name"]
    if kind == "issues" and payload["action"] == "labeled" \
            and payload["label"]["name"] == settings.trigger_label:
        i = payload["issue"]
        await ingestion.sync_repo(repo)  # make sure the mirror is fresh before RAG
        await start_swarm({"mode": "issue", "repo": repo,
                           "issue": {"number": i["number"], "title": i["title"], "body": i["body"]}})
    elif kind == "pull_request" and payload["action"] in ("opened", "ready_for_review"):
        pr = payload["pull_request"]
        if pr["head"]["ref"].startswith("swarm/"):
            return  # never review our own PRs — that's a feedback loop, not a feature
        diff = await github_io.pr_diff(repo, pr["number"])
        await ingestion.sync_repo(repo)
        await start_swarm({"mode": "pr_review", "repo": repo, "patch": diff,
                           "issue": {"number": pr["number"], "title": pr["title"], "body": pr.get("body")}})


async def on_decision(decision: str, thread_id: str) -> None:
    result = await graph.ainvoke(Command(resume=decision), run_config(thread_id))
    await telegram_io.send_text(result.get("result", "done"))


@contextlib.asynccontextmanager
async def lifespan(_app):
    global graph
    await init_db()
    graph, _saver = await make_checkpointed_graph()
    tasks = [asyncio.create_task(ingestion.run_worker(on_trigger)),
             asyncio.create_task(telegram_io.poll_updates(on_decision))]
    log.info("swarm ready — watching %s", settings.repos)
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, "bad signature")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    if delivery and not await _redis.set(f"dedup:{delivery}", 1, nx=True, ex=86400):
        return {"ok": True, "dedup": True}  # GitHub redelivers; process once
    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    if event in ("issues", "pull_request", "push"):
        await ingestion.push_event(event, payload)
    return {"ok": True}
