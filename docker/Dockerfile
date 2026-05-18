# Multi-stage build for SQL Lens.
# Stage 1: build wheels in a fat image.
# Stage 2: copy them into a slim runtime with a non-root user.

# ─────────────────────────── Stage 1: builder ────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# System libs needed to build native dependencies (psycopg2 source, etc).
# Removed from final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
COPY LICENSES ./LICENSES

# Install with all optional DB drivers so the runtime image is universal.
RUN pip install --user ".[all]"


# ─────────────────────────── Stage 2: runtime ────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

ARG BUILD_SHA=unknown
ARG BUILD_TIME=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SQLLENS_BUILD_SHA=${BUILD_SHA} \
    SQLLENS_BUILD_TIME=${BUILD_TIME} \
    PATH=/home/sqllens/.local/bin:$PATH

# libpq5 is the only system runtime dep (postgres driver). MySQL driver
# (PyMySQL) is pure Python, SQLite is in stdlib.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r sqllens --gid 1000 \
    && useradd -r -g sqllens --uid 1000 -m -d /home/sqllens -s /sbin/nologin sqllens

# Copy installed packages from builder. The `--user` install in stage 1 puts
# them under /root/.local; we relocate to the unprivileged user's home.
COPY --from=builder --chown=sqllens:sqllens /root/.local /home/sqllens/.local

USER sqllens
WORKDIR /home/sqllens

# Where ChromaDB persists by default (overridable via SQLLENS_MEMORY__PERSIST_DIR).
RUN mkdir -p /home/sqllens/data
ENV SQLLENS_MEMORY__PERSIST_DIR=/home/sqllens/data

# HTTP transport defaults — overridable via env. Stdio mode is configured by
# setting SQLLENS_SERVER__TRANSPORT=stdio.
ENV SQLLENS_SERVER__TRANSPORT=http \
    SQLLENS_SERVER__HOST=0.0.0.0 \
    SQLLENS_SERVER__PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)"

# tini handles PID 1 signal forwarding so SIGTERM from `docker stop` cleanly
# shuts down uvicorn.
ENTRYPOINT ["/usr/bin/tini", "--", "sqllens"]
CMD ["serve"]
