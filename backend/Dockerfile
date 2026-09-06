FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.7.9 /uv /bin/uv
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 relay
USER relay

EXPOSE 8000

CMD ["uvicorn", "app.api.app:default_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
