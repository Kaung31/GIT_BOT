.PHONY: dev up test evals seed sandbox-image tunnel

up:
	docker compose up -d

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
