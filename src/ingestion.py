"""Ingestion plane: GitHub webhooks → Redis Stream → async worker → local repo mirror
+ AST-chunked embeddings in pgvector. Decoupled so slow clone/embed never blocks webhook acks."""
import ast
import asyncio
import json
import logging
from pathlib import Path

import redis.asyncio as aioredis

from src.config import settings
from src.llm import embed
from src.store import CodeChunk, replace_file_chunks

log = logging.getLogger(__name__)
STREAM, GROUP = "github:events", "ingest"
_redis = aioredis.from_url(settings.redis_url, decode_responses=True)

CODE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".md", ".toml", ".yaml", ".yml"}


async def push_event(kind: str, payload: dict) -> None:
    await _redis.xadd(STREAM, {"kind": kind, "json": json.dumps(payload)}, maxlen=10_000)


def repo_path(repo: str) -> Path:
    return Path(settings.repos_dir) / repo.replace("/", "__")


async def _git(cwd: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {err.decode()[:500]}")
    return out.decode()


def chunk_python(path: str, source: str) -> list[tuple[str, str]]:
    """AST-aware: whole top-level functions/classes. Falls back to whole file on syntax errors."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [(path, source[:4000])]
    lines = source.splitlines()
    chunks = [(node.name, "\n".join(lines[node.lineno - 1:node.end_lineno]))
              for node in tree.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    header = "\n".join(l for l in lines[:tree.body[0].lineno - 1] if l.strip()) if tree.body else source
    if header.strip():
        chunks.insert(0, (f"{path}:header", header))
    return chunks or [(path, source[:4000])]


async def embed_file(repo: str, rel_path: str) -> None:
    file = repo_path(repo) / rel_path
    if not file.is_file() or file.suffix not in CODE_EXTS:
        return
    source = file.read_text(errors="replace")
    pieces = chunk_python(rel_path, source) if file.suffix == ".py" else [(rel_path, source[:4000])]
    pieces = [(n, c) for n, c in pieces if c.strip()]
    if not pieces:
        return
    vectors = await embed([c for _, c in pieces])
    await replace_file_chunks(repo, rel_path, [
        CodeChunk(repo=repo, path=rel_path, name=n, content=c, embedding=v)
        for (n, c), v in zip(pieces, vectors)])


async def sync_repo(repo: str, full: bool = False) -> None:
    """Clone or pull, then (re)embed changed files — or everything on first sync/backfill."""
    path = repo_path(repo)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://x-access-token:{settings.github_token}@github.com/{repo}.git"
        await _git(path.parent, "clone", url, path.name)
        full = True
    else:
        old = (await _git(path, "rev-parse", "HEAD")).strip()
        await _git(path, "pull", "--ff-only")
        new = (await _git(path, "rev-parse", "HEAD")).strip()
        changed = (await _git(path, "diff", "--name-only", old, new)).split() if old != new else []
        if not full:
            for f in changed:
                await embed_file(repo, f)
            log.info("synced %s: %d changed files re-embedded", repo, len(changed))
            return
    files = (await _git(path, "ls-files")).split()
    for f in files:
        await embed_file(repo, f)
    log.info("full sync of %s: %d files embedded", repo, len(files))


async def run_worker(on_trigger) -> None:
    """Consume events; mirror on push, call on_trigger(kind, payload) for swarm-worthy events."""
    try:
        await _redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except aioredis.ResponseError:
        pass  # group exists
    log.info("ingestion worker started")
    while True:
        batches = await _redis.xreadgroup(GROUP, "worker-1", {STREAM: ">"}, count=5, block=5000)
        for _, entries in batches or []:
            for entry_id, fields in entries:
                try:
                    kind, payload = fields["kind"], json.loads(fields["json"])
                    if kind == "push":
                        await sync_repo(payload["repository"]["full_name"])
                    else:
                        await on_trigger(kind, payload)
                    await _redis.xack(STREAM, GROUP, entry_id)
                except Exception:
                    log.exception("event %s failed (left pending for replay)", entry_id)
                    await asyncio.sleep(1)
