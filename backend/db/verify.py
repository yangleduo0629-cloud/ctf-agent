from __future__ import annotations

import json
import os
import uuid

from sqlalchemy.orm import Session

from backend.db.enums import CallStatus, EvidenceKind, SolverRunStatus
from backend.db.models import Artifact, Challenge, Competition, Evidence, SolverRun, ToolCall
from backend.db.repositories import Repositories
from backend.db.session import create_database_engine


def verify_round_trip(database_url: str) -> dict[str, object]:
    engine = create_database_engine(database_url)
    marker = uuid.uuid4().hex

    with Session(engine, expire_on_commit=False) as session:
        transaction = session.begin()
        try:
            repositories = Repositories(session)
            competition = repositories.competitions.add(
                Competition(name=f"Verification {marker}", slug=f"verify-{marker}")
            )
            challenge = repositories.challenges.add(
                Challenge(
                    competition_id=competition.id,
                    slug="round-trip",
                    name="Round Trip",
                    category="verification",
                )
            )
            artifact = repositories.artifacts.add(
                Artifact(
                    challenge_id=challenge.id,
                    original_name="fixture.txt",
                    storage_key=f"verification/{marker}/fixture.txt",
                    content_type="text/plain",
                    size_bytes=7,
                    sha256="0" * 64,
                )
            )
            solver_run = repositories.solver_runs.add(
                SolverRun(
                    challenge_id=challenge.id,
                    run_key="attempt-1",
                    solver_type="verification",
                    model_spec="fixture/model",
                    status=SolverRunStatus.RUNNING,
                )
            )
            tool_call = repositories.tool_calls.add(
                ToolCall(
                    solver_run_id=solver_run.id,
                    sequence=1,
                    tool_name="read_file",
                    status=CallStatus.SUCCEEDED,
                    arguments={"path": "fixture.txt"},
                    result={"bytes": 7},
                )
            )
            repositories.evidence.add(
                Evidence(
                    challenge_id=challenge.id,
                    solver_run_id=solver_run.id,
                    tool_call_id=tool_call.id,
                    artifact_id=artifact.id,
                    kind=EvidenceKind.FILE,
                    title="Fixture read",
                    content="verified",
                )
            )

            session.flush()
            competition_id = competition.id
            solver_run_id = solver_run.id
            tool_call_id = tool_call.id
            session.expunge_all()

            graph = repositories.competitions.get_graph(competition_id)
            timeline = repositories.solver_runs.get_timeline(solver_run_id)
            event = repositories.tool_calls.get(tool_call_id)
            if graph is None or timeline is None or event is None:
                raise RuntimeError("Repository round trip returned an incomplete graph")
            if len(graph.challenges) != 1 or len(timeline.tool_calls) != 1:
                raise RuntimeError("Repository round trip returned unexpected relationship counts")

            return {
                "status": "passed",
                "competition_id": str(graph.id),
                "challenge_id": str(graph.challenges[0].id),
                "artifact_count": len(graph.challenges[0].artifacts),
                "solver_run_id": str(timeline.id),
                "event_id": str(event.id),
                "event_name": event.tool_name,
            }
        finally:
            transaction.rollback()


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    print(json.dumps(verify_round_trip(database_url), sort_keys=True))


if __name__ == "__main__":
    main()
