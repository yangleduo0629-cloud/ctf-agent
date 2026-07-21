FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN pip install --no-cache-dir "litellm[proxy]==1.93.0"
COPY config.yaml /app/config.yaml

USER 65532:65532
EXPOSE 4000
ENTRYPOINT ["litellm"]
