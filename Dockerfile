# syntax=docker/dockerfile:1.7
#
# Targets:
#   runtime (default)  docker build -t media-audit .   (runs `serve`: web UI on :8484 + scheduled scans)
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
    && apt-get install -y --no-install-recommends ffmpeg shellcheck \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
COPY tests ./tests
COPY scripts ./scripts
COPY config.example.yml ./
COPY docker/config/config.yml ./docker/config/config.yml
ENV PATH="/app/.venv/bin:${PATH}"
CMD ["sh", "-c", "ruff check . && ruff format --check . && mypy && pytest -v"]

# ---------------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS runtime

LABEL org.opencontainers.image.title="media-audit" \
      org.opencontainers.image.description="Scan a media library and report what's actually in it" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/Ethan-Matlack/media-audit"

COPY --from=build /app/.venv /app/.venv
COPY config.example.yml /app/config.example.yml
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MEDIA_AUDIT_CONFIG=/config/config.yml \
    MEDIA_AUDIT_MEDIA_ROOT=/media \
    MEDIA_AUDIT_AUTO_INIT=1 \
    PUID=1000 \
    PGID=1000

# Fail the build if the bundled libmediainfo can't load on this base image.
RUN python -c "from pymediainfo import MediaInfo; assert MediaInfo.can_parse(), 'libmediainfo'"

# Which build this is, stamped by CI: release | main | dev, and the git commit. Shown in
# the start-up banner and the web UI footer (dev builds read e.g. "0.4.0-dev (abc1234)").
# Declared late so a new commit only rebuilds the layers below.
ARG BUILD_CHANNEL=""
ARG BUILD_SHA=""
ENV MEDIA_AUDIT_BUILD_CHANNEL=${BUILD_CHANNEL} \
    MEDIA_AUDIT_BUILD_SHA=${BUILD_SHA}

# The entrypoint starts as root, makes /config writable for PUID:PGID (Unraid creates
# appdata folders owned by root), then drops to that user. With `--user` / compose
# `user:` it runs as that user directly. On first start with no config.yml it writes
# one from the folders under /media; `serve` then waits for "Scan now" or the schedule.
RUN mkdir -p /config
VOLUME ["/config"]
WORKDIR /config
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MEDIA_AUDIT_WEB__PORT', '8484'), timeout=4)"

# On `docker stop` a running scan stops cleanly: files already being read are saved and
# the next scan resumes from there.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
