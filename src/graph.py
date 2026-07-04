"""Agent plane: supervised LangGraph swarm. Proposer writes, Breaker attacks (with sandbox
evidence), Arbitrator judges; revise loop caps at MAX_REVISION_ROUNDS; every external write
pauses at an interrupt() checkpointed to Postgres. The Breaker never sees the Proposer's
rationale — information isolation is what keeps the adversarial setup honest."""
import difflib
import json
import logging
import re
from operator import add
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from src import github_io, sandbox, store
from src.config import settings
from src.llm import complete, embed, prompt
from src.security import wrap_untrusted

log = logging.getLogger(__name__)


class SwarmState(TypedDict, total=False):
    mode: Literal["issue", "pr_review"]
    repo: str
    run_id: str                                 # budget key = graph thread_id
    issue: dict                                 # number, title, body (also used for PR meta)
    context: str                                # RAG-retrieved code, wrapped as untrusted
    patch: str | None                           # unified diff (overwritten per revision)
    rationale: str | None
    findings: Annotated[list[dict], add]        # APPEND-ONLY across revise rounds
    test_result: dict | None
    verdict: dict | None
    revision_round: int
    result: str                                 # human-readable outcome for Telegram


def _json(out: str) -> dict:
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, re.DOTALL)  # models love wrapping JSON in prose/fences
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {}


def _wrapped_issue(state: SwarmState) -> str:
    i = state["issue"]
    return wrap_untrusted("ISSUE", f"#{i['number']} {i['title']}\n\n{i.get('body') or ''}")


def supervisor(state: SwarmState) -> Command:
    if not state.get("context"):
        return Command(goto="retrieve")
    v = state.get("verdict")
    if v:
        if v["decision"] == "approve":
            return Command(goto="approval_gate")
        if (v["decision"] == "revise" and state["mode"] == "issue"
                and state["revision_round"] < settings.max_revision_rounds):
            return Command(goto="proposer")
        return Command(goto=END, update={"result": f"❌ swarm verdict: {v['decision']} — {v['reasoning'][:300]}"})
    if state["mode"] == "issue" and not state.get("patch"):
        return Command(goto="proposer")
    if state.get("test_result") is None:
        return Command(goto="breaker")
    return Command(goto="arbitrator")


async def retrieve(state: SwarmState) -> dict:
    i = state["issue"]
    vec = (await embed([f"{i['title']}\n{i.get('body') or ''}"]))[0]
    chunks = await store.similar_chunks(vec, state["repo"])
    ctx = "\n\n".join(f"# {c.path} :: {c.name}\n{c.content}" for c in chunks)
    return {"context": wrap_untrusted("REPO_CODE", ctx or "(no indexed code found)"),
            "revision_round": state.get("revision_round", 0)}


def _edits_to_patch(repo: str, edits: list) -> str:
    """Build a git-appliable unified diff from find/replace edits — a format small local
    models produce reliably, unlike hand-written diffs with correct hunk counts."""
    from src.ingestion import repo_path
    originals: dict[str, str] = {}
    currents: dict[str, str] = {}
    for e in edits if isinstance(edits, list) else []:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("path", "find", "replace")):
            continue
        path = e["path"].lstrip("/")
        if path not in currents:
            file = repo_path(repo) / path
            if not file.is_file():
                continue
            originals[path] = currents[path] = file.read_text()
        if e["find"] in currents[path]:
            currents[path] = currents[path].replace(e["find"], e["replace"], 1)
    parts = []
    for path, new in currents.items():
        if new == originals[path]:
            continue
        diff = difflib.unified_diff(originals[path].splitlines(keepends=True),
                                    new.splitlines(keepends=True),
                                    fromfile=f"a/{path}", tofile=f"b/{path}")
        parts.append(f"diff --git a/{path} b/{path}\n" + "".join(diff))
    return "".join(parts)


async def proposer(state: SwarmState) -> dict:
    prior = ""
    if state.get("verdict"):  # revise round: show what must change
        prior = ("Previous round findings (fix these):\n"
                 + json.dumps(state["findings"]) + "\nArbitrator instructions: "
                 + state["verdict"].get("revise_instructions", ""))
    out = await complete(settings.proposer_model,
                         prompt("proposer", issue=_wrapped_issue(state), context=state["context"],
                                prior_findings=prior),
                         run_id=state["run_id"], json_mode=True)
    data = _json(out)
    patch = _edits_to_patch(state["repo"], data.get("edits", []))
    if not patch and isinstance(data.get("patch"), str):
        patch = data["patch"]  # big models sometimes hand back a ready diff — take it
    if not patch.strip():  # no usable patch = reject, never an infinite proposer loop
        return {"verdict": {"decision": "reject", "confidence": 0.0,
                            "reasoning": "proposer produced no applicable edits"}}
    rationale = data.get("rationale", "")
    return {"patch": patch, "rationale": rationale if isinstance(rationale, str) else json.dumps(rationale),
            "revision_round": state["revision_round"] + (1 if state.get("verdict") else 0),
            "test_result": None, "verdict": None}


async def breaker(state: SwarmState) -> dict:
    test = await sandbox.run_tests(state["repo"], state.get("patch"))
    out = await complete(settings.breaker_model,
                         prompt("breaker", issue=_wrapped_issue(state),
                                patch=state.get("patch") or "(no patch — review mode)",
                                context=state["context"], test_result=json.dumps(test)),
                         run_id=state["run_id"], json_mode=True)
    findings = _json(out).get("findings", [])
    return {"test_result": test, "findings": findings}


async def arbitrator(state: SwarmState) -> dict:
    out = await complete(settings.arbitrator_model,
                         prompt("arbitrator", issue=_wrapped_issue(state),
                                patch=state.get("patch") or "(review mode)",
                                findings=json.dumps(state["findings"]),
                                test_result=json.dumps(state["test_result"])),
                         run_id=state["run_id"], json_mode=True)
    v = _json(out)
    v.setdefault("decision", "reject")
    v.setdefault("confidence", 0.0)
    v.setdefault("reasoning", out[:300])
    # HARD RULE in code, not just prompt: failing sandbox tests can never be approved
    if state["mode"] == "issue" and not state["test_result"].get("passed") and v["decision"] == "approve":
        v["decision"] = "revise"
        v["reasoning"] = "[enforced] sandbox tests failed — approval overridden. " + v["reasoning"]
    return {"verdict": v}


def approval_gate(state: SwarmState) -> Command:
    """Pauses here (checkpointed). Telegram button resumes with Command(resume=...)."""
    decision = interrupt({"card": state})
    if decision == "approve":
        return Command(goto="integrator")
    return Command(goto=END, update={"result": "❌ rejected by human — nothing written to GitHub"})


async def integrator(state: SwarmState) -> dict:
    i, v = state["issue"], state["verdict"]
    if state["mode"] == "issue":
        body = (f"{state.get('rationale', '')}\n\n---\nArbitrator: {v['decision']} "
                f"(confidence {v['confidence']})\n{v['reasoning']}")
        url = await github_io.open_pr(state["repo"], i["number"], state["patch"], i["title"], body)
        return {"result": f"✅ PR opened: {url}"}
    findings_md = "\n".join(f"- **{f.get('severity')}**: {f.get('title')} — {f.get('detail', '')}"
                            for f in state["findings"]) or "No defects found."
    body = (f"## 🤖 Adversarial review\n**Verdict:** {v['decision']} (confidence {v['confidence']})\n"
            f"{v['reasoning']}\n\n### Findings\n{findings_md}")
    url = await github_io.post_pr_review(state["repo"], i["number"], body)
    return {"result": f"✅ review posted: {url}"}


def build_graph(checkpointer=None):
    g = StateGraph(SwarmState)
    for node in (supervisor, retrieve, proposer, breaker, arbitrator, approval_gate, integrator):
        g.add_node(node)
    g.set_entry_point("supervisor")
    for n in ("retrieve", "proposer", "breaker", "arbitrator", "integrator"):
        g.add_edge(n, "supervisor") if n != "integrator" else g.add_edge(n, END)
    return g.compile(checkpointer=checkpointer)


async def make_checkpointed_graph():
    dsn = settings.database_url.replace("+asyncpg", "")
    saver_cm = AsyncPostgresSaver.from_conn_string(dsn)
    saver = await saver_cm.__aenter__()  # held open for app lifetime
    await saver.setup()
    return build_graph(saver), saver_cm


def run_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id},
            "recursion_limit": settings.graph_recursion_limit}
