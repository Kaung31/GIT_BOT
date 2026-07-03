# Slack AI Team Productivity Agent

Multi-agent Slack bot: mirrors channel activity locally, summarizes threads, extracts action
items, syncs them to Jira/Notion via MCP — **with a human approving every external write** —
and runs automated daily standups. Free & local by default (Ollama + Docker).

## Architecture

- **Ingestion plane** — Slack events → Redis Stream → async worker → Postgres+pgvector.
  Agents only ever read the local mirror; no Slack history calls at agent-time
  (designed for the strict Marketplace-app rate limits, even though an internal dev app wouldn't hit them).
- **Agent plane** — supervised LangGraph: supervisor routes to one specialist at a time
  (summarizer, task extractor, blocker detector, MCP integrator). Append-only reducers for
  extracted artifacts. Postgres checkpointer, so an approval pause **survives a restart**.
- **Guardrails** — semantic cache (pgvector), per-user/channel token buckets (Redis),
  recursion ceiling, prompt-injection flagging, and an `interrupt()` gate before any Jira/Notion write.
- **Model plane** — everything through litellm: swap `ollama/qwen3:14b` for
  `anthropic/claude-sonnet-5` by changing one env var. Optional Langfuse tracing via env keys.

## Quick start

```bash
cp .env.example .env          # fill in Slack tokens (Socket Mode app)
ollama pull qwen3:14b && ollama pull nomic-embed-text
make up                       # postgres+pgvector, redis
uv sync
make dev                      # starts bot + ingestion worker + scheduler
make backfill CHANNEL=C0123…  # one-off history load per channel
make test
make seed                     # no Slack yet? seed fake demo data into the mirror
make evals                    # summary faithfulness (LLM-judge) + extraction precision/recall
```

Optional: `docker compose --profile app up` runs the bot containerized;
`--profile observability` adds self-hosted Langfuse (tracing activates when the
`LANGFUSE_*` env keys are set).

### Slack app config (api.slack.com/apps)
- Socket Mode **on** (needs an app-level token with `connections:write`)
- Bot scopes: `channels:history`, `groups:history`, `chat:write`, `commands`, `app_mentions:read`
- Event subscriptions: `message.channels`, `message.groups`, `app_mention`
- Slash commands: `/summarize`, `/extract` (usage: `/extract` → Jira, `/extract notion` → Notion)
- Invite the bot to the channels you want mirrored.

## Usage

| Trigger | What happens |
|---|---|
| `/summarize` | Summary of the last 24h of the channel (from the local mirror) |
| `@bot` in a thread | Summary of that thread |
| `/extract [notion]` | Extracts typed action items → posts **Approve/Reject** buttons → on approve, creates Jira issues / Notion pages via the official MCP servers |
| Standup (cron) | Posts a prompt, collects replies for `STANDUP_COLLECT_MINUTES`, then posts a digest with detected blockers |

## Security notes

- All Slack message text is wrapped in `UNTRUSTED_SLACK_MESSAGES` delimiters and treated as
  data, never instructions; instruction-like content gets flagged inline.
- Even a successful prompt injection can't write to Jira/Notion — a human must click Approve.
- **Retention:** the local mirror stores real conversations. Delete a channel's rows from
  `messages` to honor deletion requests; consider PII redaction before embedding for real teams.

## Data model note

Embeddings are `nomic-embed-text` (768-dim). If you change `EMBED_MODEL`, you must set
`EMBED_DIM` to match and **re-embed everything** — vector spaces aren't compatible across models.
