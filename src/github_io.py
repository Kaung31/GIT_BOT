"""GitHub writes: open a PR from an approved diff (via the local clone — simpler than the
Git Data API blob/tree dance), post PR review comments. Only ever called after human approval."""
import logging
import tempfile
import uuid
from pathlib import Path

import httpx

from src.config import settings
from src.ingestion import _git, repo_path

log = logging.getLogger(__name__)
API = "https://api.github.com"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
    })


async def default_branch(repo: str) -> str:
    async with _client() as c:
        r = await c.get(f"{API}/repos/{repo}")
        r.raise_for_status()
        return r.json()["default_branch"]


async def open_pr(repo: str, issue_number: int, patch: str, title: str, body: str) -> str:
    """Branch from default, apply diff, commit, push, open PR. Returns PR URL."""
    path = repo_path(repo)
    base = await default_branch(repo)
    branch = f"swarm/issue-{issue_number}-{uuid.uuid4().hex[:6]}"

    await _git(path, "fetch", "origin", base)
    await _git(path, "checkout", "-B", branch, f"origin/{base}")
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch if patch.endswith("\n") else patch + "\n")
    try:
        await _git(path, "apply", "--whitespace=fix", f.name)
    finally:
        Path(f.name).unlink()
    await _git(path, "add", "-A")
    await _git(path, "-c", "user.name=code-review-swarm", "-c", "user.email=swarm@local",
               "commit", "-m", f"fix: {title} (#{issue_number})")
    await _git(path, "push", "origin", branch)
    await _git(path, "checkout", base)

    async with _client() as c:
        r = await c.post(f"{API}/repos/{repo}/pulls", json={
            "title": f"[swarm] {title}",
            "head": branch, "base": base,
            "body": f"{body}\n\nCloses #{issue_number}",
        })
        r.raise_for_status()
        url = r.json()["html_url"]
        await c.post(f"{API}/repos/{repo}/issues/{issue_number}/comments",
                     json={"body": f"Swarm opened {url} for this issue."})
        return url


async def post_pr_review(repo: str, pr_number: int, body: str) -> str:
    async with _client() as c:
        r = await c.post(f"{API}/repos/{repo}/pulls/{pr_number}/reviews",
                         json={"body": body, "event": "COMMENT"})
        r.raise_for_status()
        return r.json().get("html_url", "")


async def pr_diff(repo: str, pr_number: int) -> str:
    async with _client() as c:
        r = await c.get(f"{API}/repos/{repo}/pulls/{pr_number}",
                        headers={"Accept": "application/vnd.github.diff"})
        r.raise_for_status()
        return r.text[:40_000]  # ponytail: hard truncation — chunk giant PRs if they ever matter
