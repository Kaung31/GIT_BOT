# Code-Review Swarm

Label a GitHub issue and three LLM agents write a patch, attack it, run the repo's real tests against it in a sandbox, and hand you a verdict on Telegram — nothing merges until you tap Approve.

![Python 3.12](https://img.shields.io/badge/python-3.12-blue)

<!-- TODO: record a GIF of the full loop — label an issue, the Telegram verdict card arriving, tapping Approve, and the PR opening on GitHub. This is the whole pitch in 15 seconds. -->

## Overview

Fixing a small bug still means writing the patch, checking it doesn't break anything, and reviewing it — three steps, one person. This project splits those into three adversarial agents (Proposer, Breaker, Arbitrator) that argue over a fix before you ever see it, and backs the Breaker's opinion with an actual test run in an isolated Docker container instead of just an LLM's word. It's built for one person watching one or two repos, not a team tool — the whole point is a human still approves every write, just once, at the end, from their phone.

## Features

- Label an issue `swarm-fix` and get back a real pull request — written, sandbox-tested, and reviewed before you see it
- Every patch is tested for real: the Breaker applies it in a `--network=none` Docker container and runs the repo's actual test suite
- A failing test can never be approved — that's enforced in code, not just requested in a prompt
- Nothing reaches GitHub without a tap on a Telegram Approve/Reject button, even if an agent gets talked into something bad
- Survives a restart mid-review — the approval pause is checkpointed to Postgres, not held in a Python variable
- Runs free during development on local Ollama models; flip three env vars to route through Claude for a paid demo run
- A hard daily USD spend cap is checked before every single LLM call, using the real per-call cost from the provider
- Also reviews plain pull requests (not just labeled issues) with the same Breaker + Arbitrator pass, no Proposer involved

## Tech Stack

| Tech | Used for |
|---|---|
| FastAPI + uvicorn | Webhook receiver + background task host |
| LangGraph + langgraph-checkpoint-postgres | Agent state machine, checkpointed human-approval pause |
| litellm | One call signature across Ollama and Anthropic |
| Postgres + pgvector | Code-chunk embeddings, RAG retrieval, LangGraph checkpoints |
| SQLAlchemy (async) + asyncpg | ORM and DB driver for the above |
| Redis | Event queue (Streams), webhook dedup, spend/token counters |
| Docker | Isolated, network-off sandbox for running a repo's tests |
| httpx | GitHub REST API and Telegram Bot API calls |
| Langfuse (optional) | Per-agent LLM call tracing |
| pytest + pytest-asyncio | 11 offline unit tests |
| uv | Dependency management |

## Getting Started

### Prerequisites

- Python 3.12 (pinned in `pyproject.toml`, not 3.13)
- [uv](https://github.com/astral-sh/uv)
- Docker
- [Ollama](https://ollama.com), running locally

### Installation

```bash
cp .env.example .env
uv run python -m scripts.recommend_models   # tells you which models fit your machine
ollama pull qwen2.5-coder:14b && ollama pull qwen3:14b && ollama pull nomic-embed-text
make up                                     # postgres+pgvector, redis
make sandbox-image                          # the container the Breaker runs tests in
uv sync
make test                                   # should print 11 passed, no services needed
```

### Environment variables

Everything lives in [`.env.example`](.env.example). The ones you actually have to fill in:

| Variable | What it's for | Where to get it |
|---|---|---|
| `GITHUB_TOKEN` | Fine-grained PAT — Contents + Pull requests + Issues write, scoped to your target repo(s) | github.com → Settings → Developer settings → Fine-grained tokens |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret to verify inbound GitHub webhooks | invent one yourself |
| `TARGET_REPOS` | Comma-separated `owner/name` list to watch | your own repo(s) |
| `TELEGRAM_BOT_TOKEN` | Bot credential for sending/receiving approval messages | message @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | The only chat id allowed to approve a patch | DM your bot once, then check `api.telegram.org/bot<TOKEN>/getUpdates` |
| `ANTHROPIC_API_KEY` | Paid demo-day models only — leave blank to stay on free Ollama | console.anthropic.com |
| `PROPOSER_MODEL` / `BREAKER_MODEL` / `ARBITRATOR_MODEL` | litellm model string per agent | defaults to `ollama/...`, see `.env.example` |
| `DATABASE_URL` / `REDIS_URL` | Connection strings | defaults match `make up` |
| `DAILY_SPEND_CAP_USD` | Hard USD ceiling checked before every LLM call | default `1.50` |

### Run it

```bash
make dev      # FastAPI + ingestion worker + Telegram poller on :8000
make tunnel   # in a second terminal — ngrok, so GitHub can reach your webhook
```

Point a GitHub webhook at `https://<ngrok-url>/webhooks/github` (content type `application/json`, events: Issues, Pull requests, Pushes), create a `swarm-fix` label on the repo, and label an issue.

## Usage

**Fix an issue.** Label it `swarm-fix`. A Telegram message arrives:

```
🐝 swarm run started: you/demo-repo#4 (issue)
```

...followed a bit later by a verdict card, diff included, with inline **Approve** / **Reject** buttons:

```
🤖 Swarm verdict: approve (confidence 0.8)
The patch fixes the reported crash and doesn't touch unrelated code.

✅ tests pass

Findings (1):
• [minor] no test for the empty-list case

diff --git a/ledger.py b/ledger.py
...
```

Tap **Approve** and a real PR opens on the repo. Tap **Reject** and nothing is written anywhere.

**Review a PR.** Open any pull request on a watched repo — no label needed. The same Breaker + Arbitrator pass runs against the PR's actual diff, and on approval posts a review comment instead of opening a new PR.

**Check it without spending anything.** `make seed SEED_REPO=you/swarm-demo` creates a throwaway repo with 8 planted bugs, and `make evals` runs the swarm against all 8 and prints patch-applies / tests-pass / breaker-recall rates.

## Project Structure

```
src/
├── app.py           # FastAPI entrypoint: webhook route + background workers
├── graph.py         # The LangGraph state machine — supervisor, agents, revise loop
├── llm.py           # litellm calls, budget cap, prompt caching
├── ingestion.py     # GitHub → Redis Stream → local git mirror + embeddings
├── sandbox.py       # Runs a repo's tests against a patch in an isolated container
├── github_io.py     # Open PRs, post reviews, fetch diffs
├── telegram_io.py   # Verdict cards, Approve/Reject buttons, long-polling
├── security.py      # Prompt-injection flagging, untrusted-text wrapping
├── store.py         # SQLAlchemy models + DB session
└── prompts/         # System prompt templates, one per agent
tests/                # 11 offline unit tests — LLM and sandbox mocked
evals/                # Golden 8-bug eval set + harness
scripts/              # recommend_models, seed_target_repo, verify_caching, verify_resume
repos/                # Local git mirrors, created at runtime (gitignored)
```

## How It Works

A GitHub webhook lands, gets HMAC-verified, and is pushed onto a Redis Stream so the webhook can ack instantly — the actual work happens in a background worker. That worker refreshes a local git mirror, embeds changed code with pgvector, and hands off to a LangGraph state machine: Proposer writes a patch, Breaker runs the repo's tests against it in a network-off Docker container and critiques it, Arbitrator rules on the outcome (and can't approve a patch whose tests failed, by code, not by prompt). An approved run pauses at a LangGraph `interrupt()` checkpointed to Postgres and sends you a Telegram card; your tap resumes the exact same run from that checkpoint and opens the PR or posts the review.

## Roadmap / Known Limitations

- No CI configured — `make test` has to be run by hand
- No index on the embeddings column, so retrieval is a brute-force scan; fine at demo scale, not at "watching 50 repos" scale
- No retry/backoff on GitHub or Telegram API calls if they rate-limit or fail
- `DAILY_SPEND_CAP_USD` is one global cap, not per-repo — a busy repo can eat the whole day's budget
- The Telegram chat id is the only access control there is; there's no broader auth system

## License

<!-- TODO: no LICENSE file in this repo yet. MIT is the common default for a solo portfolio project — add a LICENSE file if you want one. -->

## Contact

<!-- TODO: add your name -->
GitHub: [@Kaung31](https://github.com/Kaung31)
<!-- TODO: add LinkedIn -->
