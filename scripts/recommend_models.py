"""Detect this machine's memory and print the Ollama model tier to use. Re-run it anywhere you
deploy (laptop, Oracle ARM box) — it just prints env lines; it never pulls or writes anything.

Usage: uv run python -m scripts.recommend_models
"""
import platform
import subprocess


def _sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def detect() -> tuple[str, float, str]:
    """Return (kind, gigabytes, label). kind is 'nvidia' | 'mac' | 'ram'."""
    vram = _sh("nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits")
    if vram:
        gb = max(int(x) for x in vram.splitlines()) / 1024
        return "nvidia", gb, f"NVIDIA GPU, {gb:.0f} GB VRAM"
    if platform.system() == "Darwin":
        b = _sh("sysctl", "-n", "hw.memsize")
        gb = int(b) / 1024**3 if b else 0
        chip = _sh("sysctl", "-n", "machdep.cpu.brand_string") or "Apple Silicon"
        return "mac", gb, f"{chip}, {gb:.0f} GB unified memory"
    meminfo = _sh("cat", "/proc/meminfo")
    kb = next((int(l.split()[1]) for l in meminfo.splitlines() if l.startswith("MemTotal")), 0)
    gb = kb / 1024**2
    return "ram", gb, f"{gb:.0f} GB system RAM"


def recommend(kind: str, gb: float) -> tuple[str, dict]:
    # Macs share memory with the OS: usable GPU is ~70%, and the swarm runs 3 models + embeddings,
    # so gate the 30B proposer at a 32 GB Mac (vs 24 GB of dedicated VRAM).
    big = (kind == "nvidia" and gb >= 24) or (kind != "nvidia" and gb >= 32)
    if big:
        return "30b (big)", {"PROPOSER_MODEL": "ollama/qwen3-coder:30b",
                             "BREAKER_MODEL": "ollama/qwen3:14b",
                             "ARBITRATOR_MODEL": "ollama/qwen3:14b"}
    if gb >= 16:
        return "14b (mid)", {"PROPOSER_MODEL": "ollama/qwen2.5-coder:14b",
                             "BREAKER_MODEL": "ollama/qwen3:14b",
                             "ARBITRATOR_MODEL": "ollama/qwen3:14b"}
    return "8b (small)", {"PROPOSER_MODEL": "ollama/qwen2.5-coder:7b",
                          "BREAKER_MODEL": "ollama/qwen3:8b",
                          "ARBITRATOR_MODEL": "ollama/qwen3:8b"}


def main() -> None:
    kind, gb, label = detect()
    tier, models = recommend(kind, gb)
    print(f"detected: {label}")
    print(f"picked tier: {tier}\n")
    for k, v in models.items():
        print(f"{k}={v}")
    if kind != "nvidia" and 24 <= gb < 32:
        print("\n# note: 24 GB Mac — qwen3-coder:30b (~18 GB) is too tight to run alongside the")
        print("# breaker/arbitrator + embeddings. Use the 14b proposer, or free memory and raise the")
        print("# GPU limit (sudo sysctl iogpu.wired_limit_mb=...) if you want to try 30b.")


if __name__ == "__main__":
    main()
