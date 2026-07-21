FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY infra/wsl/api/requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY backend/__init__.py ./backend/__init__.py
COPY backend/db ./backend/db
COPY backend/orchestration ./backend/orchestration
COPY infra/wsl/api/app ./app

USER 65532:65532
EXPOSE 8080
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8080"]
