# Multi-stage Dockerfile for the unified Cloud Run deployment.
#
#   Stage 1 builds the venv with uv (fast, deterministic).
#   Stage 2 is a slim runtime image that only ships the venv + app code.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv 0.5+: single binary, ~10x faster than pip.
RUN pip install --no-cache-dir uv==0.5.14

WORKDIR /app

# Install only the prod deps first so dep changes don't bust the cache
# every time we touch app code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the rest and install the project itself (uv treats it as a workspace).
COPY . .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Copy the prepared venv + app code from the builder.
COPY --from=builder /app /app

# Cloud Run injects $PORT — bind to it. Single uvicorn worker keeps the
# in-memory state machine + ranker happy on the small free-tier VM.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:main --host 0.0.0.0 --port ${PORT:-8000}"]
