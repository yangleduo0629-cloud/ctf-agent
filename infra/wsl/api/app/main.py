import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import from_url as redis_from_url

from backend.db.session import create_database_engine, create_session_factory
from backend.ingestion.api import IngestionRuntime
from backend.ingestion.api import router as ingestion_router
from backend.ingestion.services import ChallengeCatalog
from backend.ingestion.storage import ArtifactStorage
from backend.orchestration.api import OrchestrationRuntime
from backend.orchestration.api import router as orchestration_router
from backend.orchestration.redis_transport import EventRelay


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    engine = create_database_engine(os.environ["DATABASE_URL"])
    session_factory = create_session_factory(engine)
    redis = redis_from_url(os.environ["REDIS_URL"], decode_responses=True)
    relay = EventRelay(redis, session_factory)
    stop = asyncio.Event()
    relay_task = asyncio.create_task(relay.run(stop))
    application.state.orchestration = OrchestrationRuntime(redis, session_factory, relay)
    application.state.ingestion = IngestionRuntime(
        ChallengeCatalog(
            session_factory,
            ArtifactStorage(
                Path(os.environ["ARTIFACTS_DIR"]),
                max_size_bytes=int(os.environ.get("ARTIFACT_MAX_SIZE_BYTES", "536870912")),
            ),
        )
    )
    try:
        yield
    finally:
        stop.set()
        await relay_task
        await redis.aclose()
        engine.dispose()


app = FastAPI(title="CTF Platform API", version="0.2.0", lifespan=lifespan)
app.include_router(orchestration_router)
app.include_router(ingestion_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "api"}


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    database_url = os.environ["ASYNC_DATABASE_URL"]
    redis_url = os.environ["REDIS_URL"]

    connection = await asyncpg.connect(database_url, timeout=5)
    try:
        database_ready = await connection.fetchval("SELECT 1") == 1
        schema_revision = await connection.fetchval("SELECT version_num FROM alembic_version")
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
        "dependencies": {
            "postgres": database_ready,
            "redis": redis_ready,
            "schema_revision": schema_revision,
        },
        "storage": {name: path.is_dir() for name, path in paths.items()},
    }
