"""Eval suite: summary faithfulness (LLM-as-judge + keyword floor) and task-extraction
precision/recall against a golden dataset. Needs Ollama (or your configured model) running;
no Slack, no Postgres.

Usage: uv run python -m evals.run_evals
"""
import asyncio
import json
from pathlib import Path

from src.graph import Task
from src.llm import complete, prompt
from src.security import wrap_untrusted

DATASET = json.loads((Path(__file__).parent / "dataset.json").read_text())

JUDGE = """Rate this summary of a Slack thread for faithfulness on a 1-5 scale
(5 = every claim is supported by the thread, nothing important missing; 1 = hallucinated).

THREAD:
{thread}

SUMMARY:
{summary}

Respond with JSON only: {{"score": int, "reason": str}}"""


def task_matches(expected: dict, got: dict) -> bool:
    title = got.get("title", "").lower()
    kw_ok = all(k in title for k in expected["title_keywords"])
    owner_ok = expected.get("owner") is None or expected["owner"] == (got.get("owner") or "").lower()
    return kw_ok and owner_ok


async def eval_case(case: dict) -> dict:
    ctx = wrap_untrusted([(u, t) for u, t in case["messages"]])

    summary = await complete(prompt("summarize", messages=ctx))
    kw_hits = sum(k.lower() in summary.lower() for k in case["summary_must_mention"])
    judge_raw = await complete(JUDGE.format(thread=ctx, summary=summary), json_mode=True)
    try:
        faithfulness = json.loads(judge_raw)["score"]
    except (json.JSONDecodeError, KeyError):
        faithfulness = 0

    raw = await complete(prompt("extract_tasks", messages=ctx), json_mode=True)
    try:
        tasks = [Task(**t).model_dump() for t in json.loads(raw).get("tasks", [])]
    except (json.JSONDecodeError, TypeError, ValueError):
        tasks = []
    expected = case["expected_tasks"]
    tp = sum(any(task_matches(e, g) for g in tasks) for e in expected)
    precision = tp / len(tasks) if tasks else (1.0 if not expected else 0.0)
    recall = tp / len(expected) if expected else 1.0

    return {"name": case["name"], "summary_keywords": f"{kw_hits}/{len(case['summary_must_mention'])}",
            "faithfulness": faithfulness, "precision": round(precision, 2), "recall": round(recall, 2)}


async def main() -> None:
    results = [await eval_case(c) for c in DATASET]
    print(f"\n{'case':<28} {'kw':>5} {'faith':>6} {'prec':>5} {'recall':>6}")
    for r in results:
        print(f"{r['name']:<28} {r['summary_keywords']:>5} {r['faithfulness']:>6} {r['precision']:>5} {r['recall']:>6}")
    n = len(results)
    print(f"\nmean faithfulness {sum(r['faithfulness'] for r in results)/n:.1f}/5, "
          f"mean precision {sum(r['precision'] for r in results)/n:.2f}, "
          f"mean recall {sum(r['recall'] for r in results)/n:.2f}")
    # ponytail: prints to stdout — push results to Langfuse datasets when you want CI history


if __name__ == "__main__":
    asyncio.run(main())
