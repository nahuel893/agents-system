# Production Dockerfile for agents-system (ADR-001 Stage A runtime topology)
FROM python:3.12-slim-bookworm AS builder

# Install uv for fast, deterministic dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependency specifications first to leverage layer caching
COPY pyproject.toml uv.lock ./

# Install production dependencies only into the system environment. A plain
# install of agents-system is the library core (issue #188); the served app
# needs its extras, and `all` is every one of them (the server, the database,
# the SQL tool, embeddings and every chat model provider).
RUN uv sync --frozen --no-dev --no-install-project --extra all

# Runtime stage
FROM python:3.12-slim-bookworm AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Create unprivileged user
RUN groupadd -g 10001 agents && \
    useradd -u 10001 -g agents -s /bin/bash -m agents

# Copy installed venv from builder
COPY --from=builder --chown=agents:agents /app/.venv /app/.venv

# Copy application source code and configuration
COPY --chown=agents:agents alembic.ini ./
COPY --chown=agents:agents alembic/ ./alembic/
COPY --chown=agents:agents platform/ ./platform/
COPY --chown=agents:agents src/ ./src/

USER agents

EXPOSE 8000

# Default command: run uvicorn server in single-process mode (ADR-001 Stage A)
CMD ["uvicorn", "agents_system.server:app", "--host", "0.0.0.0", "--port", "8000"]
