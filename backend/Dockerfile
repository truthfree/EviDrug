FROM ghcr.io/astral-sh/uv:0.11.24 AS uv

FROM python:3.12.13-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY migrations ./migrations

RUN groupadd --system evidrug \
    && useradd --system --gid evidrug --home-dir /app evidrug

USER evidrug

EXPOSE 8000

CMD ["uvicorn", "evidrug_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
