# HTTP API image: `docker build -t f2m .` then `docker run -p 8000:8000 -e F2M_API_TOKEN=... f2m`
FROM python:3.10-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra api

COPY src ./src
RUN uv sync --frozen --no-dev --extra api

RUN useradd --create-home --uid 10001 f2m
USER f2m

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Listen on all interfaces inside the container. Set F2M_API_TOKEN to require a bearer token.
CMD ["f2m", "serve", "--host", "0.0.0.0", "--port", "8000"]
