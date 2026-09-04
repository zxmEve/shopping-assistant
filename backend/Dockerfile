FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir uv==0.6.10
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY fixtures ./fixtures
COPY evals ./evals
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "procure_pilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
