"""Create the demo target repo: a small pure-stdlib ledger app with 4 planted bugs, each
exposed by a failing test and described in a GitHub-style issue (evals/golden.json).

Local only:            uv run python -m scripts.seed_target_repo
Also push to GitHub:   uv run python -m scripts.seed_target_repo you/swarm-demo
(pushing creates the repo under your token's account and files the issues)
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

from src.config import settings
from src.ingestion import _git, repo_path

GOLDEN = json.loads((Path(__file__).parent.parent / "evals" / "golden.json").read_text())

LEDGER = '''\
"""A tiny expense ledger. Entries are dicts: {"desc": str, "amount": float | None}."""
import sqlite3


def last_n(entries, n):
    """Return the most recent n entries."""
    return entries[-n:-1]  # BUG: off-by-one, drops the newest entry


def total(entries):
    """Sum all amounts."""
    return sum(e["amount"] for e in entries)  # BUG: crashes when amount is None


def average(entries):
    """Average spend per entry."""
    return total(entries) / len(entries)  # BUG: ZeroDivisionError on empty ledger


def find_by_desc(db: sqlite3.Connection, desc: str):
    """Find entries matching a description."""
    cur = db.execute(f"SELECT desc, amount FROM entries WHERE desc = '{desc}'")  # BUG: SQL injection
    return cur.fetchall()


def init_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE entries (desc TEXT, amount REAL)")
    return db
'''

TESTS = '''\
from ledger import average, find_by_desc, init_db, last_n, total


def test_total():
    assert total([{"desc": "a", "amount": 2.0}, {"desc": "b", "amount": 3.0}]) == 5.0


def test_last_n_returns_newest():
    entries = [{"desc": str(i), "amount": 1.0} for i in range(5)]
    assert last_n(entries, 2) == entries[-2:]


def test_total_skips_none_amounts():
    assert total([{"desc": "a", "amount": 2.0}, {"desc": "pending", "amount": None}]) == 2.0


def test_average_empty_ledger_is_zero():
    assert average([]) == 0


def test_find_by_desc_is_injection_safe():
    db = init_db()
    db.execute("INSERT INTO entries VALUES ('coffee', 3.5)")
    db.execute("INSERT INTO entries VALUES ('tea', 2.0)")
    assert find_by_desc(db, "coffee") == [("coffee", 3.5)]
    assert find_by_desc(db, "x' OR '1'='1") == []  # injection must not dump the table
'''


async def main(github_repo: str | None) -> None:
    repo = github_repo or "local/swarm-demo"
    path = repo_path(repo)
    path.mkdir(parents=True, exist_ok=True)
    (path / "ledger.py").write_text(LEDGER)
    (path / "test_ledger.py").write_text(TESTS)
    (path / "README.md").write_text("# swarm-demo\nSeeded target repo with planted bugs.\n")
    if not (path / ".git").exists():
        await _git(path, "init", "-b", "main")
    await _git(path, "add", "-A")
    await _git(path, "-c", "user.name=seed", "-c", "user.email=seed@local",
               "commit", "-m", "seed demo app with planted bugs", "--allow-empty")
    print(f"local repo ready at {path} ({len(GOLDEN)} golden issues in evals/golden.json)")

    if github_repo:
        owner, name = github_repo.split("/")
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {settings.github_token}",
                                              "Accept": "application/vnd.github+json"}) as c:
            r = await c.post("https://api.github.com/user/repos",
                             json={"name": name, "private": True})
            if r.status_code not in (201, 422):  # 422 = already exists
                r.raise_for_status()
            await _git(path, "push", "-u",
                       f"https://x-access-token:{settings.github_token}@github.com/{github_repo}.git",
                       "main", "--force")
            for g in GOLDEN:
                await c.post(f"https://api.github.com/repos/{github_repo}/issues",
                             json={"title": g["title"], "body": g["body"]})
            print(f"pushed to github.com/{github_repo} and filed {len(GOLDEN)} issues — "
                  f"label one '{settings.trigger_label}' to trigger the swarm")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
