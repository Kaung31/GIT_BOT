"""Prove the restart-resume demo works: pause at the approval interrupt on one graph instance,
throw it away, build a brand-new graph + Postgres connection (what a real process restart gives
you), and resume from the checkpoint alone. If this passes, the live demo (docker compose down/up
between the Telegram card and your Approve tap) will too.

Needs postgres up (`make up`). No LLM, no GitHub, no Telegram — a pre-approved state routes
straight to the gate, and the PR write is stubbed.

Usage: uv run python -m scripts.verify_resume
"""
import asyncio

from langgraph.types import Command

from src import github_io
from src.graph import make_checkpointed_graph, run_config
from src.store import init_db

THREAD = "verify-resume-demo"
PRE_APPROVED = {
    "mode": "issue", "repo": "local/swarm-demo", "run_id": THREAD,
    "issue": {"number": 1, "title": "fix the bug", "body": ""},
    "context": "<<<code>>>", "revision_round": 0,
    "patch": "diff --git a/x b/x", "rationale": "fixes it",
    "test_result": {"passed": True, "applied": True, "log": ""},
    "verdict": {"decision": "approve", "confidence": 0.9, "reasoning": "looks good"},
}


async def main() -> None:
    await init_db()

    async def fake_open_pr(repo, number, patch, title, body):
        return "https://github.com/example/pr/1"
    github_io.open_pr = fake_open_pr  # integrator calls github_io.open_pr

    graph1, cm1 = await make_checkpointed_graph()
    paused = await graph1.ainvoke(PRE_APPROVED, run_config(THREAD))
    assert paused.get("__interrupt__"), "expected a pause at the approval gate"
    print("1) paused at interrupt, state checkpointed to Postgres")
    await cm1.__aexit__(None, None, None)  # close the connection === process exits
    del graph1
    print("2) discarded graph + closed its DB connection (simulated restart)")

    graph2, cm2 = await make_checkpointed_graph()  # fresh instance, fresh connection
    done = await graph2.ainvoke(Command(resume="approve"), run_config(THREAD))
    await cm2.__aexit__(None, None, None)
    print("3) resumed on the fresh graph:", done.get("result"))
    assert "PR opened" in done.get("result", ""), "resume did not complete the write"
    print("\n✅ restart-resume verified — the checkpoint lived entirely in Postgres")


if __name__ == "__main__":
    asyncio.run(main())
