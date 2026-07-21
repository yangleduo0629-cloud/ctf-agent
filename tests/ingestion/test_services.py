import io
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.enums import ChallengeStatus, SolverRunStatus
from backend.db.models import Artifact, Challenge, Competition, DomainEvent, SolverRun
from backend.ingestion.services import ChallengeCatalog, parse_connection_info


def test_local_challenge_artifact_and_solver_run_share_domain_model(
    catalog: ChallengeCatalog,
    session_factory: sessionmaker[Session],
    artifact_root: Path,
) -> None:
    competition = catalog.create_competition(
        name="Local Exercises",
        slug="local-exercises",
        platform="local",
        flag_format="flag{...}",
        flag_regex=r"^flag\{[^}\r\n]+\}$",
        idempotency_key="competition:local-exercises",
    )
    challenge = catalog.create_challenge(
        competition.id,
        name="SimpleSocket",
        slug="simple-socket",
        category="misc",
        description="Local fixture",
        connection_info="nc challenge.example 31337",
        idempotency_key="challenge:simple-socket",
    )
    result = catalog.add_artifact_fileobj(
        challenge.id,
        "SimpleSocket.zip",
        io.BytesIO(b"PK\x03\x04fixture"),
        idempotency_key="artifact:simple-socket",
    )
    solver_run = catalog.create_solver_run(
        challenge.id,
        run_key="simple-socket-1",
        solver_type="agent",
        model_spec="test-model",
        idempotency_key="solver-run:simple-socket-1",
    )

    with session_factory() as session:
        persisted_competition = session.get(Competition, competition.id)
        persisted_challenge = session.get(Challenge, challenge.id)
        persisted_artifact = session.get(Artifact, result.artifact.id)
        persisted_run = session.get(SolverRun, solver_run.id)
        assert persisted_competition is not None
        assert persisted_competition.flag_format == "flag{...}"
        assert persisted_challenge is not None
        assert persisted_challenge.status == ChallengeStatus.INGESTED
        assert persisted_challenge.service_protocol == "tcp"
        assert persisted_challenge.service_host == "challenge.example"
        assert persisted_challenge.service_port == 31337
        assert persisted_artifact is not None
        assert persisted_artifact.content_type == "application/zip"
        assert persisted_run is not None
        assert persisted_run.status == SolverRunStatus.QUEUED
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 5

    assert (artifact_root / result.artifact.storage_key).is_file()


def test_duplicate_sha_reuses_database_row_and_physical_file(
    catalog: ChallengeCatalog,
    session_factory: sessionmaker[Session],
    artifact_root: Path,
) -> None:
    competition = catalog.create_competition(
        name="Local",
        slug="local",
        platform="local",
        idempotency_key="competition:local",
    )
    challenge = catalog.create_challenge(
        competition.id,
        name="Duplicate",
        slug="duplicate",
        category="misc",
        idempotency_key="challenge:duplicate",
    )
    first = catalog.add_artifact_fileobj(
        challenge.id,
        "first.bin",
        io.BytesIO(b"same-content"),
        idempotency_key="artifact:first",
    )
    second = catalog.add_artifact_fileobj(
        challenge.id,
        "second.bin",
        io.BytesIO(b"same-content"),
        idempotency_key="artifact:second",
    )
    replay = catalog.add_artifact_fileobj(
        challenge.id,
        "third.bin",
        io.BytesIO(b"same-content"),
        idempotency_key="artifact:second",
    )

    assert not first.duplicate
    assert second.duplicate
    assert replay.duplicate
    assert first.artifact.id == second.artifact.id == replay.artifact.id
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Artifact)) == 1
        artifact_events = session.scalars(
            select(DomainEvent).where(DomainEvent.event_type == "artifact.created")
        ).all()
        assert len(artifact_events) == 2
    assert len([path for path in artifact_root.rglob("*") if path.is_file()]) == 1


def test_record_failure_removes_stored_file(
    catalog: ChallengeCatalog,
    artifact_root: Path,
) -> None:
    missing_challenge = uuid.uuid4()

    with pytest.raises(LookupError, match="Challenge not found"):
        catalog.add_artifact_fileobj(
            missing_challenge,
            "orphan.bin",
            io.BytesIO(b"orphan"),
            idempotency_key="artifact:orphan",
        )

    assert len([path for path in artifact_root.rglob("*") if path.is_file()]) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://challenge.example:8443/path", ("https", "challenge.example", 8443)),
        ("nc challenge.example 31337", ("tcp", "challenge.example", 31337)),
        ("challenge.example:9000", ("tcp", "challenge.example", 9000)),
        ("not an endpoint", (None, None, None)),
        ("https://challenge.example:not-a-port", ("https", "challenge.example", None)),
    ],
)
def test_parse_connection_info(raw: str, expected: tuple[str | None, str | None, int | None]) -> None:
    endpoint = parse_connection_info(raw)
    assert (endpoint.protocol, endpoint.host, endpoint.port) == expected
