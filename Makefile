.PHONY: dev up test evals seed sandbox-image tunnel langfuse verify-resume verify-caching recommend-models

up:
	docker compose up -d

langfuse: up  # self-hosted tracing on http://localhost:3000 (first run creates its DB)
	-docker compose exec -T postgres createdb -U swarm langfuse 2>/dev/null || true
	docker compose --profile observability up -d langfuse
	@echo "Langfuse at http://localhost:3000 — sign up, create a project, copy the keys into .env"

verify-resume: up  # prove the checkpoint survives a process restart (no LLM/GitHub needed)
	uv run python -m scripts.verify_resume

verify-caching: up  # confirm Haiku prompt caching engages (needs ANTHROPIC_API_KEY, ~$0.10)
	uv run python -m scripts.verify_caching

recommend-models:  # print the Ollama model tier for this machine's memory
	uv run python -m scripts.recommend_models

sandbox-image:
	docker build -f Dockerfile.sandbox -t swarm-sandbox .

dev: up
	uv run uvicorn src.app:app --port 8000

tunnel:  # expose the webhook for GitHub in dev
	ngrok http 8000

seed:  # create the demo repo with planted bugs (needs GITHUB_TOKEN + SEED_REPO=you/name)
	uv run python -m scripts.seed_target_repo $(SEED_REPO)

test:
	uv run pytest -q

evals: up sandbox-image
	uv run python -m evals.run_evals
