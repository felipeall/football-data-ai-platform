# Ingestion worker, with dbt for Transform Runs, and migrations. Build context: the repository root.
FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:$PATH" \
    # camoufox's browser, fetched at build time; camoufox also writes there at run time.
    XDG_CACHE_HOME=/opt/cache \
    DISPLAY=:99

WORKDIR /app

# Sofascore's Token Provider (ADR 30): camoufox runs headed under Xvfb, since headless Linux
# has no WebGL and fails the Turnstile. The fonts are those of the image the probes passed in.
# Before the Python dependencies, so a lock change does not reinstall them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb fonts-noto-core fonts-noto-color-emoji fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project

# Firefox's own libraries and camoufox's browser come with the locked Python packages.
RUN apt-get update \
    && python -m playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/* \
    && python -m camoufox fetch \
    && useradd --system --create-home worker \
    && chown -R worker /opt/cache \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY alembic.ini ./
COPY migrations ./migrations
# The Transform Run's dbt project (ADR 31); dbt writes its target and logs to a temporary directory.
COPY dbt ./dbt
COPY src ./src
RUN uv sync --locked

# The worker user has a home directory: Firefox writes its temporary profile state there.
USER worker

# Xvfb in the background with a full-size screen. A restarted container keeps the last
# run's lock file, which would stop Xvfb; exec keeps the worker as the process that receives
# the container's stop signal (compose runs an init that reaps leftover browser processes).
CMD ["sh", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & exec python -m football_data_ai_platform.ingestion.worker"]
