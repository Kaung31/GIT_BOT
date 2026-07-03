# Slack AI Team Productivity Agent

Multi-agent Slack bot: mirrors channel activity locally, summarizes threads, extracts action
items, syncs them to Jira/Notion — **with a human approving every external write** — and runs
automated daily standups. Free & local by default (Ollama + Docker).

## Architecture

- **Ingestion plane** — Slack events → Redis Stream → async worker → Postgres+pgvector.
  Agents only ever read the local mirror; no Slack history calls at agent-time
  (designed for the strict Marketplace-app rate limits, even though an internal dev app wouldn't hit them).
- **Agent plane** — supervised LangGraph: supervisor routes to one specialist at a time
  (summarizer, task extractor, blocker detector, integrator). Append-only reducers for
  extracted artifacts. Postgres checkpointer, so an approval pause **survives a restart**.
- **Guardrails** — semantic cache (pgvector), per-user/channel token buckets (Redis),
  recursion ceiling, prompt-injection flagging, and an `interrupt()` gate before any Jira/Notion write.
- **Model plane** — everything through litellm: swap `ollama/qwen3:14b` for
  `anthropic/claude-sonnet-5` by changing one env var. Optional Langfuse tracing via env keys.
- **Integrations** — direct REST calls to Jira Cloud v3 / Notion, authenticated with API tokens
  (see setup below). `src/integrations.py` has a `ponytail:` comment marking where to swap in
  the official MCP servers (Atlassian Rovo, Notion hosted MCP) later — the graph doesn't change,
  only those two functions do.

## Quick start

```bash
cp .env.example .env          # fill in tokens as you complete the steps below
ollama pull qwen3:14b && ollama pull nomic-embed-text
make up                       # postgres+pgvector, redis
uv sync
make test                     # 4 smoke tests, no external services needed
make seed && make evals        # no Slack yet? seed fake data and score the agents
make dev                      # starts bot + ingestion worker + scheduler
make backfill CHANNEL=C0123…  # one-off real history load per channel
```

Optional: `docker compose --profile app up` runs the bot containerized;
`--profile observability` adds self-hosted Langfuse (tracing activates when the
`LANGFUSE_*` env keys are set).

## Usage

| Trigger | What happens |
|---|---|
| `/summarize` | Summary of the last 24h of the channel (from the local mirror) |
| `@bot` in a thread | Summary of that thread |
| `/extract [notion]` | Extracts typed action items → posts **Approve/Reject** buttons → on approve, creates real Jira issues / Notion pages |
| Standup (cron) | Posts a prompt, collects replies for `STANDUP_COLLECT_MINUTES`, then posts a digest with detected blockers |

---

## Step-by-step setup

### 1. Slack app (Socket Mode — no public URL needed)

1. Go to **[api.slack.com/apps](https://api.slack.com/apps)** → **Create New App** → **From scratch**.
   Name it, pick your dev workspace.
2. **Socket Mode** (left sidebar) → toggle **on**. It'll ask you to generate an app-level token —
   name it anything, scope `connections:write`. Copy the token (`xapp-...`) → `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → scroll to **Scopes → Bot Token Scopes** → add:
   `channels:history`, `groups:history`, `chat:write`, `commands`, `app_mentions:read`.
4. **Event Subscriptions** → toggle **on** (Socket Mode means no Request URL needed) →
   **Subscribe to bot events** → add `message.channels`, `message.groups`, `app_mention`.
5. **Slash Commands** → **Create New Command** twice: `/summarize` and `/extract`
   (Request URL can be left as `https://example.com` — Socket Mode ignores it, Slack just
   requires the field to be non-empty).
6. **Install App** (top of OAuth & Permissions, or Basic Information) → **Install to Workspace** →
   approve. Copy the **Bot User OAuth Token** (`xoxb-...`) → `SLACK_BOT_TOKEN`.
7. **Basic Information** → **App Credentials** → copy **Signing Secret** → `SLACK_SIGNING_SECRET`.
8. In Slack, go to any channel you want mirrored → **Add apps** → add your bot.

**Test it:** `make dev`, watch the terminal for `starting Socket Mode handler` with no errors,
then in Slack type `/summarize` in a channel you added the bot to. First run will say no messages
found (nothing mirrored yet) — send a few messages, wait a couple seconds for the worker to embed
them, then try again. Try `@yourbot` inside a thread too.

### 2. Jira (direct API token — ~5 min, no OAuth app needed)

1. Go to **[id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)**
   → **Create API token** → copy it → `JIRA_API_TOKEN`.
2. `JIRA_BASE_URL` = your site, e.g. `https://yourteam.atlassian.net`.
3. `JIRA_EMAIL` = the email you log into that Atlassian account with.
4. `JIRA_PROJECT_KEY` = the short prefix in your issue keys (e.g. `PROJ` in `PROJ-123`) —
   find it under **Project settings** or in any existing issue key.

**Test it directly first** (isolates auth problems from the bot):
```bash
curl -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  -X POST "$JIRA_BASE_URL/rest/api/3/issue" -H "Content-Type: application/json" \
  -d '{"fields":{"project":{"key":"'"$JIRA_PROJECT_KEY"'"},"summary":"test from curl","issuetype":{"name":"Task"}}}'
```
A `201` with an issue key back means the token/project are good. Then run `/extract` in Slack on
a thread with real commitments, click **Approve**, and check the issue landed in Jira.

### 3. Notion (internal integration secret)

1. Go to **[notion.so/my-integrations](https://www.notion.so/my-integrations)** → **New integration**
   → give it a name → copy the **Internal Integration Secret** (`ntn_...`) → `NOTION_API_TOKEN`.
2. Open the database you want tasks created in → **•••** menu → **Connections** → connect your
   new integration (databases aren't visible to integrations until explicitly shared).
3. Copy the **database ID** from its URL: `notion.so/<workspace>/<DATABASE_ID>?v=...` (32 hex chars,
   dashes optional).
4. Run the helper to resolve it to a data source (Notion's 2025 API models a database as having
   one or more data sources — pages are created against the data source, not the bare database id):
   ```bash
   uv run python -m scripts.notion_data_source_id <DATABASE_ID>
   ```
   Copy the printed id → `NOTION_DATA_SOURCE_ID`.
5. `NOTION_TITLE_PROPERTY` — the name of your database's title column, usually `Name` (leave the
   default unless you renamed it).

**Test it directly first:**
```bash
curl -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_API_TOKEN" -H "Notion-Version: 2025-09-03" -H "Content-Type: application/json" \
  -d '{"parent":{"data_source_id":"'"$NOTION_DATA_SOURCE_ID"'"},"properties":{"Name":{"title":[{"text":{"content":"test from curl"}}]}}}'
```
A `200` with a page object back means it's wired correctly. Then `/extract notion` in Slack,
**Approve**, and check the page landed in the database.

### 4. Full end-to-end test

1. `make dev` running, bot in a channel, a few real messages sent (or `make seed` for fake ones).
2. `/extract` (or `/extract notion`) → bot posts a preview with **Approve/Reject** buttons.
3. Click **Approve** → bot replies with ✅ per created item, and it's now visible in Jira/Notion.
4. Restart-survival check (the "impressive" part): run `/extract` again, **before** clicking
   Approve, kill and restart `make dev` (Ctrl-C, rerun). Click Approve on the *old* message —
   it still resumes and creates the issues, because the pause was checkpointed to Postgres, not
   held in memory.
5. Click **Reject** on a different run → confirm nothing was created (bot says so, and nothing
   new appears in Jira/Notion).

## Security notes

- All Slack message text is wrapped in `UNTRUSTED_SLACK_MESSAGES` delimiters and treated as
  data, never instructions; instruction-like content gets flagged inline.
- Even a successful prompt injection can't write to Jira/Notion — a human must click Approve.
- **Retention:** the local mirror stores real conversations. Delete a channel's rows from
  `messages` to honor deletion requests; consider PII redaction before embedding for real teams.

## Data model note

Embeddings are `nomic-embed-text` (768-dim). If you change `EMBED_MODEL`, you must set
`EMBED_DIM` to match and **re-embed everything** — vector spaces aren't compatible across models.
