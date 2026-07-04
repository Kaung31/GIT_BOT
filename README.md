# Code-Review Swarm

Adversarial multi-agent code review: a **Proposer** writes a patch for a labeled GitHub issue,
a **Breaker** attacks it (including running the repo's tests against it in a locked-down
sandbox), an **Arbitrator** judges — and nothing touches GitHub until **you tap Approve on
Telegram**. Local-first (Ollama), with a small Anthropic budget reserved for demo day.

## Architecture

- **Ingestion plane** — GitHub webhooks → Redis Stream → async worker → local repo mirror +
  AST-chunked embeddings in Postgres/pgvector. Agents only read the local mirror mid-run.
- **Agent plane** — supervised LangGraph: `proposer → breaker → arbitrator`, revise loop capped
  at `MAX_REVISION_ROUNDS`, append-only findings across rounds, Postgres checkpointer so an
  approval pause **survives a restart**. The Breaker never sees the Proposer's rationale —
  information isolation keeps the adversarial setup honest.
- **Sandbox** — patches are untrusted code: applied to a throwaway copy and tested in a
  container with `--network=none`, a memory cap, and a hard timeout. **Failing tests can never
  be approved** — enforced in code, not just in the prompt.
- **Guardrails** — per-run token buckets, a hard **daily USD spend cap** (protects the £4.90),
  semantic cache, recursion ceiling, prompt-injection flagging on all GitHub-sourced text.
- **Model routing** — per-agent via litellm: develop everything on Ollama; flip three env vars
  on demo day (Sonnet writes, Haiku critiques). Optional Langfuse tracing via env keys.

## Quick start

```bash
cp .env.example .env             # fill in as you complete setup below
uv run python -m scripts.recommend_models   # prints the model tier for your hardware
# default tier (16–24GB): pull these three
ollama pull qwen2.5-coder:14b && ollama pull qwen3:14b && ollama pull nomic-embed-text
# big tier (32GB Mac / 24GB+ VRAM) instead of the coder:14b above:
#   ollama pull qwen3-coder:30b   # then set PROPOSER_MODEL=ollama/qwen3-coder:30b in .env
make up                          # postgres+pgvector, redis
make sandbox-image               # the container the Breaker runs tests in
uv sync
make test                        # 11 offline tests, no services needed
make seed                        # local demo repo with 8 planted bugs
make evals                       # patch-applies / tests-pass / breaker-recall (labeled with models)
make dev                         # FastAPI on :8000 (webhook + worker + telegram poller)
make tunnel                      # ngrok for the GitHub webhook
```

## The two modes

| Trigger | What happens |
|---|---|
| Label an issue `swarm-fix` | Full swarm writes a patch → sandbox-tested → verdict → Telegram Approve → real PR opens |
| Open a PR | Breaker + Arbitrator review it (cheap mode) → Telegram Approve → review comment posted |

## What's left (your side) — in order

The code is built and tested; these are the manual steps, and **order matters** — verify caching
before you spend on a paid eval:

1. **Pick models for your hardware** — `uv run python -m scripts.recommend_models`, pull them.
2. **Telegram** bot token + chat id ([step 1](#1-telegram-bot-2-minutes)).
3. **GitHub** fine-grained PAT (with expiry) + webhook + `swarm-fix` label ([step 2](#2-github-app-side-5-minutes)).
4. **Langfuse** — `make langfuse`, paste keys, do a run, screenshot the trace tree.
5. **Verify caching** — add `ANTHROPIC_API_KEY`, run `scripts/verify_caching.py` (~$0.10). **Do this before step 6.**
6. **Paid eval run** — flip models to Anthropic, `make evals` (results are labeled with the model names).
7. **Rehearse the restart-resume demo** ([below](#restart-resume-demo-rehearse-last)).

## Step-by-step setup

### 1. Telegram bot (2 minutes)
1. Message **@BotFather** → `/newbot` → pick a name → copy the token → `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — your `chat.id` is in the reply →
   `TELEGRAM_CHAT_ID`. Only this chat can approve; button taps from anyone else are ignored.

### 2. GitHub app-side (5 minutes)
1. **Fine-grained PAT** (never a classic full-scope token): github.com → Settings → Developer
   settings → Fine-grained tokens. Scope it to **only your demo repos**, with **Read+Write on
   Contents, Pull requests, and Issues** (nothing else), and **set an expiry date** (e.g. 90 days
   — don't pick "no expiration"). → `GITHUB_TOKEN`.
2. Per target repo: Settings → Webhooks → Add webhook →
   Payload URL `https://<your-ngrok-domain>/webhooks/github`, content type `application/json`,
   a secret you invent (→ `GITHUB_WEBHOOK_SECRET`), events: **Issues, Pull requests, Pushes**.
3. Create the label `swarm-fix` in the repo (Issues → Labels).
4. List the repos in `TARGET_REPOS=owner/name,owner/name2`.

### 3. Demo repo + first run
```bash
make seed SEED_REPO=you/swarm-demo   # creates the repo on GitHub, pushes, files the 4 issues
make dev & make tunnel               # or two terminals
```
Then label one of the filed issues `swarm-fix` and watch: Telegram pings you that the run
started, then delivers the verdict card with the diff, findings, confidence, and
**[✅ Approve] [❌ Reject]**. Tap Approve → a real PR appears on the repo.

**The restart trick (interview centerpiece):** while a verdict card sits unanswered on your
phone, kill `make dev`, restart it, then tap Approve — the run resumes from its Postgres
checkpoint and the PR still opens.

## Budget guardrails for the £4.90

Development never touches the paid API — the default models are all `ollama/…`. On demo day set
`PROPOSER_MODEL=anthropic/claude-sonnet-4-6`, `BREAKER_MODEL=ARBITRATOR_MODEL=anthropic/claude-haiku-4-5`.
`DAILY_SPEND_CAP_USD` (default $1.50) is enforced before every LLM call from litellm's real
per-response cost — a runaway loop stops itself the same day it starts. Per-run token buckets
and the graph recursion ceiling back that up.

## Evals

`make evals` replays the 8 seeded issues (off-by-one slice, None-handling, empty-list edge case,
SQL injection, wrong comparison operator, mutable default argument, integer-division money bug,
off-by-one pagination) through the swarm against the local mirror and reports **patch-applies %,
tests-pass %, breaker recall** on the planted bugs, plus verdicts and revise rounds.
`evals/known_good.patch` is the reference fix for all 8 — the sandbox goes green with it applied
(verified), so a perfect proposer scores 8/8.

## Observability, caching & the demo (your side)

The code side of all of these is done and tested. Here's the manual half:

### Langfuse tracing
1. `make langfuse` — brings up Langfuse on http://localhost:3000 and creates its DB.
2. Sign up (local account), create a project, copy the public + secret keys into `.env`
   (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST=http://localhost:3000`).
3. Do one swarm run. Open Langfuse → Traces. Each run is one trace named `swarm-run`, grouped by
   run id, with a generation per agent (`proposer`/`breaker`/`arbitrator`) tagged with the model
   and showing per-call tokens and cost. The gateway already emits this metadata — screenshot the
   trace tree; that's your "exact cost of every run" README figure.

### Prompt caching (verify before spending)
Prompts are split into a stable prefix (role + rules + repo context + issue) and a variable suffix
(patch/findings/test); for `anthropic/*` models the stable prefix carries `cache_control`.
**Anthropic silently skips caching below a per-model minimum: 1,024 tokens for Sonnet but 4,096
for Haiku.** The Breaker/Arbitrator run on Haiku, so they retrieve more context chunks
(`BREAKER/ARBITRATOR_CONTEXT_CHUNKS=12`) to clear it, and the gateway logs a `caching DISABLED`
warning per agent whenever a real prefix still falls short.

To confirm caching is actually engaging **before any paid run**:
1. Add `ANTHROPIC_API_KEY` to `.env`.
2. `uv run python -m scripts.verify_caching` — makes two identical cheap Haiku calls (~$0.10,
   refuses to run without the key, respects the daily spend cap) and prints
   `cache_creation_input_tokens` / `cache_read_input_tokens` for each. It exits non-zero if the
   second call didn't read from cache.
3. **Caveat:** the tiny demo repo's context is well under 4,096 tokens, so caching legitimately
   won't engage on it — `verify_caching.py` uses a large synthetic prefix to test the mechanism,
   and real target repos with 12 real-function chunks clear the minimum. If it's 0 on a big repo,
   the wiring is broken; 0 on the toy repo is expected.

### Restart-resume demo (rehearse last)
- `make verify-resume` proves the checkpoint survives a process restart automatically (no LLM,
  no GitHub) — run it any time; it's green.
- For the live version: label an issue → wait for the Telegram card → `docker compose down` →
  count to ten → `docker compose up -d && make dev` → tap Approve → the PR opens. Screen-record
  it once as a backup. If the live one ever fails but `make verify-resume` passes, the problem is
  in the Telegram callback → `on_decision` wiring, not the checkpointer.

## Security notes

- Issue/PR text is public, attacker-realistic input: wrapped in `UNTRUSTED_*` delimiters,
  instruction-like content flagged, and structurally backstopped by the no-network sandbox and
  the human gate. An injected "add a backdoor" still can't merge anything — only you can.
- The PAT is fine-grained and repo-scoped; the bot never force-pushes to default branches.
- Webhooks are HMAC-verified and deduplicated by delivery id.
