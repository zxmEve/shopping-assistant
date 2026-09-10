FROM python:3.13-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.6.10
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY fixtures ./fixtures
COPY config ./config
COPY evals ./evals
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "procure_pilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
