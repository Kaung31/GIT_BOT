"""Agent plane: supervised LangGraph. Supervisor routes to one specialist at a time;
every external write pauses at an interrupt() gate and survives restarts via the
Postgres checkpointer."""
import datetime as dt
import json
import logging
from operator import add
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from src import integrations, store
from src.config import settings
from src.llm import complete, prompt
from src.security import wrap_untrusted

log = logging.getLogger(__name__)


class Task(BaseModel):
    title: str
    owner: str | None = None
    deadline: str | None = None
    detail: str = ""


class AgentState(TypedDict, total=False):
    goal: Literal["summarize", "extract", "standup"]
    channel: str
    thread_ts: str | None
    user: str | None
    target: Literal["jira", "notion"]
    context: str                                # wrapped untrusted messages
    summary: str
    action_items: Annotated[list[dict], add]    # append-only
    blockers: Annotated[list[dict], add]        # append-only
    digest: str
    pending_write: dict | None
    write_results: list[str]
    error: str


async def _load_context(state: AgentState) -> str:
    if state.get("thread_ts"):
        msgs = await store.thread_messages(state["channel"], state["thread_ts"])
    else:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=settings.standup_collect_minutes) \
            if state["goal"] == "standup" else dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        msgs = await store.channel_messages(state["channel"], since)
    if not msgs:
        return ""
    return wrap_untrusted([(m.user, m.text) for m in msgs])


def supervisor(state: AgentState) -> Command:
    """Routes to exactly one specialist; specialists return here."""
    goal = state["goal"]
    if not state.get("context"):
        return Command(goto="retrieve")
    if goal == "summarize":
        return Command(goto=END if state.get("summary") else "summarizer")
    if goal == "extract":
        if not state.get("summary"):
            return Command(goto="summarizer")
        if not state.get("action_items") and "pending_write" not in state:
            return Command(goto="task_extractor")
        if state.get("pending_write"):
            return Command(goto="approval_gate")
        return Command(goto=END)
    if goal == "standup":
        if not state.get("blockers") and "digest" not in state:
            return Command(goto="blocker_detector")
        if not state.get("digest"):
            return Command(goto="synthesizer")
        return Command(goto=END)
    return Command(goto=END)


async def retrieve(state: AgentState) -> dict:
    ctx = await _load_context(state)
    if not ctx:
        return {"context": "", "summary": "_No messages found — has the channel been mirrored? Try the backfill script._",
                "digest": "_No standup replies collected._", "error": "no_context"}
    return {"context": ctx}


async def summarizer(state: AgentState) -> dict:
    out = await complete(prompt("summarize", messages=state["context"]),
                         user=state.get("user"), channel=state["channel"], cache=True)
    return {"summary": out}


async def task_extractor(state: AgentState) -> dict:
    out = await complete(prompt("extract_tasks", messages=state["context"]),
                         user=state.get("user"), channel=state["channel"], json_mode=True)
    try:
        tasks = [Task(**t).model_dump() for t in json.loads(out).get("tasks", [])]
    except (json.JSONDecodeError, TypeError, ValueError):
        log.warning("extractor returned invalid JSON: %.200s", out)
        tasks = []
    pending = {"action": f"create_{state.get('target', 'jira')}_items", "tasks": tasks} if tasks else None
    return {"action_items": tasks, "pending_write": pending}


async def blocker_detector(state: AgentState) -> dict:
    out = await complete(prompt("blockers", messages=state["context"]),
                         user=state.get("user"), channel=state["channel"], json_mode=True)
    try:
        blockers = json.loads(out).get("blockers", [])
    except json.JSONDecodeError:
        blockers = []
    return {"blockers": blockers}


async def synthesizer(state: AgentState) -> dict:
    out = await complete(prompt("standup_digest", messages=state["context"],
                                blockers=json.dumps(state.get("blockers", []))),
                         channel=state["channel"], cache=True)
    return {"digest": out}


def approval_gate(state: AgentState) -> Command:
    """Graph pauses here, checkpointed to Postgres. Slack button resumes with
    Command(resume='approve'|'reject')."""
    decision = interrupt({"preview": state["pending_write"]})
    if decision == "approve":
        return Command(goto="mcp_integrator")
    return Command(goto=END, update={"pending_write": None, "write_results": ["Rejected — nothing created."]})


async def mcp_integrator(state: AgentState) -> dict:
    create = integrations.create_notion_page if state.get("target") == "notion" \
        else integrations.create_jira_issue
    results = []
    for task in state["pending_write"]["tasks"]:
        try:
            await create(task)
            results.append(f"✅ {task['title']}")
        except Exception as e:
            log.exception("MCP write failed")
            results.append(f"❌ {task['title']} — {e}")
    return {"pending_write": None, "write_results": results}


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node(supervisor)
    g.add_node(retrieve)
    g.add_node(summarizer)
    g.add_node(task_extractor)
    g.add_node(blocker_detector)
    g.add_node(synthesizer)
    g.add_node(approval_gate)
    g.add_node(mcp_integrator)
    g.set_entry_point("supervisor")
    for n in ("retrieve", "summarizer", "task_extractor", "blocker_detector",
              "synthesizer", "mcp_integrator"):
        g.add_edge(n, "supervisor")
    return g.compile(checkpointer=checkpointer)


async def make_checkpointed_graph():
    """Postgres checkpointer so interrupts survive restarts."""
    dsn = settings.database_url.replace("+asyncpg", "")
    saver_cm = AsyncPostgresSaver.from_conn_string(dsn)
    saver = await saver_cm.__aenter__()  # held open for app lifetime
    await saver.setup()
    return build_graph(saver), saver_cm


def run_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id},
            "recursion_limit": settings.graph_recursion_limit}
