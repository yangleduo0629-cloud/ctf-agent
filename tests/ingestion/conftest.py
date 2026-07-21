from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db import Base
from backend.db.session import create_session_factory
from backend.ingestion.services import ChallengeCatalog
from backend.ingestion.storage import ArtifactStorage


@pytest.fixture
def engine() -> Iterator[Engine]:
    database_engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(database_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def catalog(
    session_factory: sessionmaker[Session],
    artifact_root: Path,
) -> ChallengeCatalog:
    return ChallengeCatalog(session_factory, ArtifactStorage(artifact_root))
