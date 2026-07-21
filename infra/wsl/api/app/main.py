import os
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import from_url as redis_from_url

app = FastAPI(title="CTF Platform API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "api"}


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    database_url = os.environ["DATABASE_URL"]
    redis_url = os.environ["REDIS_URL"]

    connection = await asyncpg.connect(database_url, timeout=5)
    try:
        database_ready = await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()

    redis = redis_from_url(redis_url, socket_connect_timeout=5)
    try:
        redis_ready = bool(await redis.ping())
    finally:
        await redis.aclose()

    paths = {
        "artifacts": Path(os.environ["ARTIFACTS_DIR"]),
        "checkpoints": Path(os.environ["CHECKPOINTS_DIR"]),
    }
    return {
        "status": "ready",
        "dependencies": {"postgres": database_ready, "redis": redis_ready},
        "storage": {name: path.is_dir() for name, path in paths.items()},
    }
