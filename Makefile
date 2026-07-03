.PHONY: dev up test backfill

up:
	docker compose up -d

dev: up
	uv run python -m src.app

backfill:  # make backfill CHANNEL=C0123456789
	uv run python -m src.backfill $(CHANNEL)

test:
	uv run pytest -q

seed: up  # fake workspace data, no Slack needed
	uv run python -m scripts.seed_demo_workspace

evals: up
	uv run python -m evals.run_evals
