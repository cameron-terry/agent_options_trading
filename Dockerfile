FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first so this layer is cached across source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY options_agent ./options_agent
COPY alembic ./alembic
COPY scripts ./scripts
COPY alembic.ini config.toml universe.txt README.md ./

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /app/data

# Liveness: the scheduler's monitor runner touches this file every interval
# (2 min default). A dead/wedged scheduler thread stops touching it and the
# container goes unhealthy instead of showing "Up" forever. 600s tolerance =
# 5× the monitor interval, generous enough for slow cycles.
ENV HEARTBEAT_FILE=/app/data/heartbeat
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import os,sys,time; p=os.environ['HEARTBEAT_FILE']; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 600 else 1)"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "options_agent"]