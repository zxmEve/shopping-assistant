.PHONY: setup backend frontend dev test eval build

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

build:
	cd frontend && npm run build

