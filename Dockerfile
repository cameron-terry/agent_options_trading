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

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "options_agent"]