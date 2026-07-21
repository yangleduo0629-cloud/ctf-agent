FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30

ARG UPSTREAM_SHA
LABEL org.opencontainers.image.source="https://github.com/verialabs/ctf-agent" \
      org.opencontainers.image.revision="${UPSTREAM_SHA}" \
      org.opencontainers.image.licenses="MIT"

ENV UV_NO_PROGRESS=1 \
    UV_PROJECT_ENVIRONMENT=/opt/veria/.venv
WORKDIR /opt/veria

RUN python -m pip install --no-cache-dir uv==0.11.29
COPY pyproject.toml uv.lock README.md ./
COPY backend ./backend
RUN uv sync --frozen --no-dev

ENTRYPOINT ["/opt/veria/.venv/bin/ctf-solve"]
CMD ["--help"]
