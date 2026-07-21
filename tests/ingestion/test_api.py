import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ingestion.api import IngestionRuntime, router
from backend.ingestion.services import ChallengeCatalog


def test_local_ingestion_api_creates_complete_solver_input(catalog: ChallengeCatalog) -> None:
    app = FastAPI()
    app.state.ingestion = IngestionRuntime(catalog)
    app.include_router(router)

    with TestClient(app) as client:
        competition_response = client.post(
            "/api/competitions",
            headers={"Idempotency-Key": "api:competition:local"},
            json={
                "name": "Local Exercises",
                "slug": "local-exercises",
                "platform": "local",
                "flag_format": "flag{...}",
                "flag_regex": r"^flag\{[^}\r\n]+\}$",
            },
        )
        assert competition_response.status_code == 201
        competition = competition_response.json()

        challenge_response = client.post(
            f"/api/competitions/{competition['id']}/challenges",
            headers={"Idempotency-Key": "api:challenge:simple-socket"},
            json={
                "name": "SimpleSocket",
                "slug": "simple-socket",
                "category": "misc",
                "connection_info": "nc simple-socket.example 31337",
            },
        )
        assert challenge_response.status_code == 201
        challenge = challenge_response.json()
        assert challenge["service_protocol"] == "tcp"
        assert challenge["service_host"] == "simple-socket.example"
        assert challenge["service_port"] == 31337

        content = b"PK\x03\x04api-fixture"
        artifact_response = client.post(
            f"/api/challenges/{challenge['id']}/artifacts",
            headers={"Idempotency-Key": "api:artifact:simple-socket"},
            files={"file": ("SimpleSocket.zip", content, "application/octet-stream")},
        )
        assert artifact_response.status_code == 201
        artifact = artifact_response.json()
        assert artifact["duplicate"] is False
        assert artifact["artifact"]["content_type"] == "application/zip"
        assert artifact["artifact"]["sha256"] == hashlib.sha256(content).hexdigest()

        run_response = client.post(
            f"/api/challenges/{challenge['id']}/solver-runs",
            headers={"Idempotency-Key": "api:solver-run:simple-socket"},
            json={
                "run_key": "simple-socket-1",
                "solver_type": "agent",
                "model_spec": "test-model",
            },
        )
        assert run_response.status_code == 201
        solver_run = run_response.json()
        assert solver_run["challenge_id"] == challenge["id"]
        assert solver_run["status"] == "queued"


def test_api_requires_and_enforces_idempotency_key(catalog: ChallengeCatalog) -> None:
    app = FastAPI()
    app.state.ingestion = IngestionRuntime(catalog)
    app.include_router(router)

    with TestClient(app) as client:
        missing_header = client.post(
            "/api/competitions",
            json={"name": "Missing", "slug": "missing", "platform": "local"},
        )
        assert missing_header.status_code == 422

        first = client.post(
            "/api/competitions",
            headers={"Idempotency-Key": "api:competition:replay"},
            json={"name": "Replay", "slug": "replay", "platform": "local"},
        )
        replay = client.post(
            "/api/competitions",
            headers={"Idempotency-Key": "api:competition:replay"},
            json={"name": "Replay", "slug": "replay", "platform": "local"},
        )
        conflict = client.post(
            "/api/competitions",
            headers={"Idempotency-Key": "api:competition:replay"},
            json={"name": "Other", "slug": "other", "platform": "local"},
        )

        assert first.status_code == replay.status_code == 201
        assert first.json()["id"] == replay.json()["id"]
        assert conflict.status_code == 409
