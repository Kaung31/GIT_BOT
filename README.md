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
ollama pull qwen2.5-coder:14b && ollama pull qwen3:14b && ollama pull nomic-embed-text
make up                          # postgres+pgvector, redis
make sandbox-image               # the container the Breaker runs tests in
uv sync
make test                        # 8 offline tests, no services needed
make seed                        # local demo repo with 4 planted bugs
make evals                       # patch-applies / tests-pass / breaker-recall on the golden set
make dev                         # FastAPI on :8000 (webhook + worker + telegram poller)
make tunnel                      # ngrok for the GitHub webhook
```

## The two modes

| Trigger | What happens |
|---|---|
| Label an issue `swarm-fix` | Full swarm writes a patch → sandbox-tested → verdict → Telegram Approve → real PR opens |
| Open a PR | Breaker + Arbitrator review it (cheap mode) → Telegram Approve → review comment posted |

## Step-by-step setup

### 1. Telegram bot (2 minutes)
1. Message **@BotFather** → `/newbot` → pick a name → copy the token → `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — your `chat.id` is in the reply →
   `TELEGRAM_CHAT_ID`. Only this chat can approve; button taps from anyone else are ignored.

### 2. GitHub app-side (5 minutes)
1. Fine-grained PAT: github.com → Settings → Developer settings → Fine-grained tokens →
   scope it to **only** your target repos with Read+Write on **Contents, Pull requests,
   Issues** → `GITHUB_TOKEN`.
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

`make evals` replays the 4 seeded issues (off-by-one, None-handling, empty-list edge case,
SQL injection) through the swarm against the local mirror and reports **patch-applies %,
tests-pass %, breaker recall** on the planted bugs, plus verdicts and revise rounds.
`evals/known_good.patch` is the reference fix — the sandbox goes green with it applied
(verified), so a perfect proposer scores 4/4.

## Security notes

- Issue/PR text is public, attacker-realistic input: wrapped in `UNTRUSTED_*` delimiters,
  instruction-like content flagged, and structurally backstopped by the no-network sandbox and
  the human gate. An injected "add a backdoor" still can't merge anything — only you can.
- The PAT is fine-grained and repo-scoped; the bot never force-pushes to default branches.
- Webhooks are HMAC-verified and deduplicated by delivery id.
