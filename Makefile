.PHONY: setup backend frontend dev test eval live-eval live-eval-s3 live-eval-compare migrate build eval-requirements

setup:
	cd backend && uv sync --extra dev
	cd frontend && npm install

backend:
	cd backend && uv run uvicorn procure_pilot.api.app:app --host 127.0.0.1 --port 8000

frontend:
	cd frontend && npm run dev -- --host 127.0.0.1

dev:
	@trap 'kill 0' INT TERM EXIT; \
	(cd backend && uv run uvicorn procure_pilot.api.app:app --host 127.0.0.1 --port 8000) & \
	(cd frontend && npm run dev -- --host 127.0.0.1) & \
	wait

test:
	cd backend && uv run ruff check .
	cd backend && uv run pytest -q
	cd frontend && npm run build

eval:
	cd backend && uv run procure-eval

eval-requirements:
	cd backend && uv run procure-requirement-eval run --mode offline --split development

live-eval:
	cd backend && uv run procure-live-eval --runs 3

live-eval-s3:
	cd backend && uv run procure-live-eval --suite s3-smoke --topology two_agent --runs 3

live-eval-compare:
	@echo "Legacy topology comparison was removed; run 'make live-eval-s3' for the two-agent gate."

migrate:
	cd backend && uv run alembic upgrade head

build:
	cd frontend && npm run build
