"""Sandboxed test runner: apply the (untrusted) patch to a throwaway copy of the repo and run
its test suite in a locked-down container — no network, memory cap, hard timeout, destroyed after.
A failing test is objective evidence the Arbitrator cannot argue past."""
import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from src.config import settings
from src.ingestion import _git, repo_path

log = logging.getLogger(__name__)


async def run_tests(repo: str, patch: str | None, test_filter: str | None = None) -> dict:
    """Returns {"passed": bool, "log": str, "applied": bool}. test_filter (pytest -k) scopes to
    the labeled issue's test — the demo repo has other planted bugs whose tests always fail."""
    workdir = Path(tempfile.mkdtemp(prefix="swarm-sandbox-"))
    name = f"swarm-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(repo_path(repo), workdir / "repo", dirs_exist_ok=True)
        if patch:
            patch_file = workdir / "swarm.patch"
            patch_file.write_text(patch if patch.endswith("\n") else patch + "\n")
            try:
                await _git(workdir / "repo", "apply", "--whitespace=fix", str(patch_file))
            except RuntimeError as e:
                return {"passed": False, "applied": False, "log": f"patch failed to apply: {e}"}

        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", "--name", name,
            "--network=none", f"--memory={settings.sandbox_memory_mb}m", "--cpus=2",
            "-v", f"{workdir / 'repo'}:/work", "-w", "/work",
            settings.sandbox_image, "python", "-m", "pytest", "-x", "-q", "--no-header",
            *(["-k", test_filter] if test_filter else []),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), settings.sandbox_timeout_s)
        except TimeoutError:
            await (await asyncio.create_subprocess_exec("docker", "kill", name)).wait()
            return {"passed": False, "applied": True, "log": f"timed out after {settings.sandbox_timeout_s}s"}
        return {"passed": proc.returncode == 0, "applied": True,
                "log": out.decode(errors="replace")[-3000:]}  # tail is where pytest failures live
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
