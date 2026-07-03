.PHONY: dev up test backfill

up:
	docker compose up -d

dev: up
	uv run python -m src.app

backfill:  # make backfill CHANNEL=C0123456789
	uv run python -m src.backfill $(CHANNEL)

test:
	uv run pytest -q
