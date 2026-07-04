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

## Full runbook: zero → demo done

Follow top to bottom. **You never point this at your real projects.** The target is a *throwaway
demo repo* the seed script creates for you — and even then the bot only ever opens a PR you can
close. Everything through Phase 5 is **free** (Ollama); only Phase 6 spends the Anthropic budget.

### Phase 0 — local setup (once, ~10 min)
```bash
cp .env.example .env
uv run python -m scripts.recommend_models     # prints your model tier; pull what it lists:
ollama pull qwen2.5-coder:14b && ollama pull qwen3:14b && ollama pull nomic-embed-text
make up            # postgres + redis
make sandbox-image # the container the Breaker runs tests in
uv sync
make test          # must print "11 passed"
```

### Phase 1 — Telegram (~2 min)
1. Message **@BotFather** → `/newbot` → name it → copy the token → `TELEGRAM_BOT_TOKEN` in `.env`.
2. DM your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a
   browser → copy the numeric `chat.id` from the JSON → `TELEGRAM_CHAT_ID`. Only that id can approve.

### Phase 2 — GitHub token (~3 min)
Fine-grained PAT (never a classic token): github.com → Settings → Developer settings →
Fine-grained tokens → **only the demo repo you'll make next**, **Read+Write on Contents +
Pull requests + Issues** (nothing else), **set a 90-day expiry** → `GITHUB_TOKEN` in `.env`.

### Phase 3 — create the throwaway test repo (~3 min)
This is the "don't touch your real projects" step. `make seed` creates a **brand-new private repo**
full of planted bugs — it is not your real code:
```bash
make seed SEED_REPO=you/swarm-demo   # creates the repo on GitHub, pushes, files 8 bug issues
```
Then wire it up:
1. `.env`: set `TARGET_REPOS=you/swarm-demo` and invent a `GITHUB_WEBHOOK_SECRET`.
2. `make tunnel` (leave running) → copy the `https://…ngrok…` URL.
3. On GitHub → the **swarm-demo** repo → Settings → Webhooks → Add webhook:
   Payload URL `https://<ngrok>/webhooks/github`, content type `application/json`,
   Secret = your `GITHUB_WEBHOOK_SECRET`, events: **Issues, Pull requests, Pushes**.
4. In that repo, Issues → Labels → create `swarm-fix`.

### Phase 4 — first live run, FREE on Ollama (~5 min)
```bash
make dev           # keep `make tunnel` running in another terminal
```
1. On GitHub, open the **swarm-demo** repo → Issues → pick one → add the **`swarm-fix`** label.
2. Telegram pings "🐝 swarm run started", then a verdict card: diff, findings, confidence,
   **[✅ Approve] [❌ Reject]**.
3. Tap **Approve** → a PR opens on swarm-demo. Open it, look, then **Close it** (don't merge —
   nothing here needs shipping). Full loop proven, **$0 spent**.
4. Try **PR-review mode** too: open any PR on swarm-demo → the bot posts an adversarial review →
   Approve → it comments on the PR. Close the PR.

### Phase 5 — Langfuse trace + local eval numbers (~10 min) — your CV figures
```bash
make langfuse      # http://localhost:3000 → sign up → new project → keys into .env → restart `make dev`
```
- Do one run → Langfuse → Traces → screenshot the `swarm-run` tree (per-agent tokens + cost). **Figure 1.**
```bash
make evals         # 8 planted bugs, labeled with the model names
```
- Screenshot the results table — your **local** patch-applies / tests-pass / breaker-recall. **Figure 2.**

### Phase 6 — the paid demo-day run (spends the £4.90) — do these IN ORDER
1. Add `ANTHROPIC_API_KEY` to `.env`.
2. **Verify caching FIRST:** `make verify-caching` (~$0.10). It must print `cache_read > 0` on call 2.
   (If it doesn't, fix it before spending more — see the reference section below.)
3. Flip models in `.env`: `PROPOSER_MODEL=anthropic/claude-sonnet-4-6`,
   `BREAKER_MODEL` and `ARBITRATOR_MODEL` = `anthropic/claude-haiku-4-5`.
4. `make evals` → screenshot the **Anthropic** numbers beside your local ones. **Figure 3.**
5. Do 2–3 live labeled-issue runs for the recording. `DAILY_SPEND_CAP_USD=1.50` halts any runaway.

### Phase 7 — the showpiece: restart survives a restart (record this)
`make verify-resume` proves it headless anytime (green, no LLM/GitHub). For the live take:
label an issue → wait for the Telegram card → `docker compose down` → wait 10s →
`docker compose up -d && make dev` → tap **Approve** → the PR still opens. Screen-record it.

### Phase 8 — done + cleanup
- Delete or archive the **swarm-demo** repo — it's disposable.
- Your PAT expires on its own; revoke it early if you like.
- Keep the four artifacts (Langfuse trace, local eval table, Anthropic eval table, restart recording).
  **That's your portfolio — the demo is done.**

### Optional: one "real code" data point without touching a finished project
Want to show it on real code? **Copy/fork one finished project into a new repo** (e.g.
`you/swarm-test-myproject`), point `TARGET_REPOS` at the *copy*, and use PR-review mode: open a
throwaway PR there, let the bot review it, close it. Your original repo is never touched.

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

## Reference: the why behind the runbook

- **Langfuse trace** — each run is one trace named `swarm-run`, grouped by run id, one generation
  per agent (`proposer`/`breaker`/`arbitrator`) tagged with the model, showing per-call tokens and
  cost. That's your "I know the exact cost of every run" figure.
- **Prompt caching minimums** — Anthropic silently skips caching if the cached prefix is below a
  per-model floor: **1,024 tokens for Sonnet, 4,096 for Haiku**. Prompts are split into a stable
  prefix (role + rules + repo context + issue, marked `cache_control`) and a variable suffix, and
  the Haiku agents retrieve more chunks (`BREAKER/ARBITRATOR_CONTEXT_CHUNKS=12`) to clear 4,096.
  The gateway logs a `caching DISABLED` warning per agent when a real prefix still falls short.
  If `make verify-caching` shows `cache_read=0` on call 2: prefix under the minimum, model isn't
  `anthropic/…`, or >5 min elapsed between calls (TTL). The toy demo repo is legitimately under
  4,096 — that's why `verify_caching.py` uses a large synthetic prefix to test the mechanism.
- **Restart-resume** — if the live restart fails but `make verify-resume` passes, the bug is in the
  Telegram callback → `on_decision` wiring, not the Postgres checkpointer.

## Security notes

- Issue/PR text is public, attacker-realistic input: wrapped in `UNTRUSTED_*` delimiters,
  instruction-like content flagged, and structurally backstopped by the no-network sandbox and
  the human gate. An injected "add a backdoor" still can't merge anything — only you can.
- The PAT is fine-grained and repo-scoped; the bot never force-pushes to default branches.
- Webhooks are HMAC-verified and deduplicated by delivery id.
