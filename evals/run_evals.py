"""Eval harness: replay the golden seeded issues through the swarm against the local demo repo.
Scores per issue: patch applies? sandbox tests pass? Breaker caught the planted bug? verdict.
Needs: docker (sandbox image built), Ollama/your models, postgres+redis up. No GitHub/Telegram.

Usage: uv run python -m evals.run_evals
"""
import asyncio
import json
import uuid
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from src.graph import build_graph, run_config
from src.ingestion import _git, embed_file
from src.store import init_db

REPO = "local/swarm-demo"
GOLDEN = json.loads((Path(__file__).parent / "golden.json").read_text())


async def eval_issue(g: dict, number: int) -> dict:
    graph = build_graph(MemorySaver())
    state = {"mode": "issue", "repo": REPO, "run_id": str(uuid.uuid4()),
             "issue": {"number": number, "title": g["title"], "body": g["body"]}}
    result = await graph.ainvoke(state, run_config(str(uuid.uuid4())))
    if result.get("__interrupt__"):  # paused at approval — that's a completed swarm run for eval purposes
        result = result["__interrupt__"][0].value["card"]
    test = result.get("test_result") or {}
    findings_text = json.dumps(result.get("findings", [])).lower()
    return {
        "issue": g["bug_keyword"],
        "patch_applies": bool(test.get("applied")),
        "tests_pass": bool(test.get("passed")),
        "breaker_recall": any(h in findings_text for h in g["breaker_hints"]),
        "verdict": (result.get("verdict") or {}).get("decision", "none"),
        "rounds": result.get("revision_round", 0),
    }


async def main() -> None:
    await init_db()
    from src.ingestion import repo_path
    assert repo_path(REPO).exists(), "run `uv run python -m scripts.seed_target_repo` first"
    for f in (await _git(repo_path(REPO), "ls-files")).split():
        await embed_file(REPO, f)

    results = [await eval_issue(g, i + 1) for i, g in enumerate(GOLDEN)]
    print(f"\n{'planted bug':<22} {'applies':>8} {'tests':>6} {'recall':>7} {'verdict':>8} {'rounds':>7}")
    for r in results:
        print(f"{r['issue']:<22} {str(r['patch_applies']):>8} {str(r['tests_pass']):>6} "
              f"{str(r['breaker_recall']):>7} {r['verdict']:>8} {r['rounds']:>7}")
    n = len(results)
    print(f"\npatch-applies {sum(r['patch_applies'] for r in results)}/{n} · "
          f"tests-pass {sum(r['tests_pass'] for r in results)}/{n} · "
          f"breaker recall {sum(r['breaker_recall'] for r in results)}/{n}")
    # ponytail: stdout report — push to Langfuse datasets when you want CI history


if __name__ == "__main__":
    asyncio.run(main())
