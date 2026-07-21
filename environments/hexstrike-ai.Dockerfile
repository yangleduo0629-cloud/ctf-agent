FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG UPSTREAM_SHA
LABEL org.opencontainers.image.source="https://github.com/0x4m4/hexstrike-ai" \
      org.opencontainers.image.revision="${UPSTREAM_SHA}" \
      org.opencontainers.image.licenses="MIT"

ENV HEXSTRIKE_PORT=8888 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/hexstrike

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.11.29
COPY third_party/locks/hexstrike-requirements.lock.txt /tmp/requirements.txt
RUN uv pip install --system --requirement /tmp/requirements.txt
COPY upstream/hexstrike-ai/hexstrike_server.py upstream/hexstrike-ai/hexstrike_mcp.py ./

EXPOSE 8888
HEALTHCHECK --interval=5s --timeout=5s --start-period=20s --retries=12 \
  CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8888/health', timeout=4)); assert d['status']=='healthy'"
CMD ["python", "hexstrike_server.py", "--port", "8888"]
