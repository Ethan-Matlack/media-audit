# syntax=docker/dockerfile:1.7
#
# Targets:
#   runtime (default)  docker build -t media-audit .
#   test               docker build --target test -t media-audit:test . && docker run --rm media-audit:test
#
# libmediainfo comes bundled in pymediainfo's manylinux wheel (needs only zlib and
# libstdc++, both in Debian slim), so no apt packages are needed at runtime.

ARG PYTHON_VERSION=3.12
ARG DEBIAN_RELEASE=bookworm

# Must read uv.lock's format (revision 5); bump together with local uv.
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# ---------------------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------- test
FROM build AS test
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
COPY tests ./tests
COPY scripts ./scripts
COPY config.example.yml ./
ENV PATH="/app/.venv/bin:${PATH}"
CMD ["sh", "-c", "ruff check . && ruff format --check . && mypy && pytest -v"]

# ---------------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS runtime

LABEL org.opencontainers.image.title="media-audit" \
      org.opencontainers.image.description="Scan a media library and report what's actually in it" \
      org.opencontainers.image.licenses="MIT"

COPY --from=build /app/.venv /app/.venv
COPY config.example.yml /app/config.example.yml

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MEDIA_AUDIT_CONFIG=/config/config.yml

# Fail the build if the bundled libmediainfo can't load on this base image.
RUN python -c "from pymediainfo import MediaInfo; assert MediaInfo.can_parse(), 'libmediainfo'"

# Runs as any UID (set with `user:` in compose); /config must be writable by it.
RUN mkdir -p /config && chmod 0777 /config
VOLUME ["/config"]
WORKDIR /config
USER 1000:1000

ENTRYPOINT ["media-audit"]
CMD ["scan"]
