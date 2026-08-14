FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock* ./
COPY src/ src/
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "uvicorn", "buyback.main:app", "--host", "0.0.0.0", "--port", "8000"]
