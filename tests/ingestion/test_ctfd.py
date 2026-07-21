from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import Artifact, Challenge, SolverRun
from backend.ingestion.ctfd import (
    CTFdConnectionConfig,
    CTFdSourceClient,
    CTFdSynchronizer,
    _same_origin,
)
from backend.ingestion.services import ChallengeCatalog


def json_response(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, request=request, json=payload)


@pytest.mark.asyncio
async def test_ctfd_token_pagination_details_and_hidden_filter() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Token test-token"
        if request.url.path == "/api/v1/challenges":
            page = request.url.params.get("page")
            if page == "1":
                return json_response(
                    request,
                    {
                        "success": True,
                        "data": [{"id": 1, "type": "standard"}, {"id": 9, "type": "hidden"}],
                        "meta": {"pagination": {"pages": 2}},
                    },
                )
            return json_response(
                request,
                {
                    "success": True,
                    "data": [{"id": 2, "type": "standard"}],
                    "meta": {"pagination": {"pages": 2}},
                },
            )
        challenge_id = request.url.path.rsplit("/", maxsplit=1)[-1]
        return json_response(
            request,
            {"success": True, "data": {"id": int(challenge_id), "name": f"Challenge {challenge_id}"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://ctfd.example",
    ) as http_client:
        client = CTFdSourceClient(
            CTFdConnectionConfig(base_url="https://ctfd.example", token="test-token"),
            client=http_client,
        )
        challenges = await client.fetch_all_challenges()

    assert [challenge["id"] for challenge in challenges] == [1, 2]
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_ctfd_username_password_session_authentication() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login" and request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                text='<input name="nonce" value="nonce-value">',
            )
        if request.url.path == "/login" and request.method == "POST":
            assert b"nonce=nonce-value" in request.content
            return httpx.Response(302, request=request, headers={"location": "/"})
        if request.url.path == "/":
            return httpx.Response(200, request=request)
        return json_response(
            request,
            {
                "success": True,
                "data": [],
                "meta": {"pagination": {"pages": 1}},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://ctfd.example",
        follow_redirects=True,
    ) as http_client:
        client = CTFdSourceClient(
            CTFdConnectionConfig(
                base_url="https://ctfd.example",
                username="user",
                password="password",
            ),
            client=http_client,
        )
        assert await client.fetch_all_challenges() == []

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/login"),
        ("POST", "/login"),
        ("GET", "/"),
        ("GET", "/api/v1/challenges"),
    ]


@pytest.mark.asyncio
async def test_ctfd_sync_is_idempotent_and_downloads_dictionary_file_entry(
    catalog: ChallengeCatalog,
    session_factory: sessionmaker[Session],
) -> None:
    competition = catalog.create_competition(
        name="CTFd Fixture",
        slug="ctfd-fixture",
        platform="ctfd",
        external_id="fixture",
        idempotency_key="competition:ctfd-fixture",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/challenges":
            return json_response(
                request,
                {
                    "success": True,
                    "data": [{"id": 7, "type": "standard"}],
                    "meta": {"pagination": {"pages": 1}},
                },
            )
        if request.url.path == "/api/v1/challenges/7":
            return json_response(
                request,
                {
                    "success": True,
                    "data": {
                        "id": 7,
                        "name": "Remote Socket",
                        "category": "pwn",
                        "description": "Remote fixture",
                        "value": 500,
                        "connection_info": "nc remote.example 31337",
                        "files": [{"location": "/files/remote.zip", "name": "remote.zip"}],
                        "tags": [{"value": "binary"}],
                        "hints": [],
                        "solves": 2,
                    },
                },
            )
        if request.url.path == "/files/remote.zip":
            return httpx.Response(
                200,
                request=request,
                content=b"PK\x03\x04remote-fixture",
                headers={
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="remote.zip"',
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://ctfd.example",
    ) as http_client:
        client = CTFdSourceClient(
            CTFdConnectionConfig(base_url="https://ctfd.example", token="test-token"),
            client=http_client,
        )
        synchronizer = CTFdSynchronizer(catalog, client)
        first = await synchronizer.sync(competition.id)
        second = await synchronizer.sync(competition.id)

    assert (first.artifacts_created, first.artifacts_reused) == (1, 0)
    assert (second.artifacts_created, second.artifacts_reused) == (0, 1)
    with session_factory() as session:
        challenge = session.scalar(select(Challenge).where(Challenge.external_id == "7"))
        assert challenge is not None
        assert challenge.service_host == "remote.example"
        assert challenge.service_port == 31337
        assert challenge.details["files"] == ["/files/remote.zip"]
        assert session.scalar(select(func.count()).select_from(Challenge)) == 1
        assert session.scalar(select(func.count()).select_from(Artifact)) == 1

    solver_run = catalog.create_solver_run(
        challenge.id,
        run_key="remote-1",
        solver_type="agent",
        model_spec="test-model",
        idempotency_key="solver-run:remote-1",
    )
    with session_factory() as session:
        assert session.get(SolverRun, solver_run.id) is not None


def test_same_origin_handles_invalid_ports() -> None:
    assert not _same_origin("https://ctfd.example:not-a-port/file", "https://ctfd.example")
