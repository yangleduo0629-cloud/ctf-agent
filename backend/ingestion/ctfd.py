from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.parse import unquote, urljoin, urlparse

import httpx

from backend.ingestion.services import ArtifactIngestionResult, ChallengeCatalog


@dataclass(frozen=True)
class CTFdConnectionConfig:
    base_url: str
    token: str = field(default="", repr=False)
    username: str = ""
    password: str = field(default="", repr=False)
    verify_tls: bool = True
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> CTFdConnectionConfig:
        base_url = os.environ.get("CTFD_URL", "").strip()
        if not base_url:
            raise ValueError("CTFD_URL is not configured")
        return cls(
            base_url=base_url,
            token=os.environ.get("CTFD_TOKEN", ""),
            username=os.environ.get("CTFD_USERNAME", ""),
            password=os.environ.get("CTFD_PASSWORD", ""),
            verify_tls=os.environ.get("CTFD_VERIFY_TLS", "true").lower()
            not in {"0", "false", "no"},
            timeout_seconds=float(os.environ.get("CTFD_TIMEOUT_SECONDS", "30")),
        )


@dataclass(frozen=True)
class RemoteFile:
    name: str
    source_url: str
    content_type: str | None
    chunks: AsyncIterator[bytes]


@dataclass(frozen=True)
class CTFdSyncResult:
    challenges_seen: int
    challenges_synced: int
    artifacts_created: int
    artifacts_reused: int


class CTFdSourceClient:
    def __init__(
        self,
        config: CTFdConnectionConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            follow_redirects=True,
            headers={"User-Agent": "ctf-platform-ingestion/1.0"},
        )
        self._logged_in = False

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        stubs: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._get_json(
                "/api/v1/challenges",
                params={"page": page, "per_page": 100},
            )
            data = payload.get("data") or []
            stubs.extend(item for item in data if item.get("type") != "hidden")
            pages = int(payload.get("meta", {}).get("pagination", {}).get("pages") or 1)
            if page >= pages:
                break
            page += 1

        challenges: list[dict[str, Any]] = []
        for stub in stubs:
            detail = await self._get_json(f"/api/v1/challenges/{stub['id']}")
            data = detail.get("data")
            if not isinstance(data, dict):
                raise ValueError(f"CTFd challenge {stub['id']} returned invalid data")
            challenges.append(data)
        return challenges

    @asynccontextmanager
    async def stream_file(self, raw_url: str) -> AsyncIterator[RemoteFile]:
        await self._ensure_authenticated()
        source_url = urljoin(f"{self.config.base_url.rstrip('/')}/", raw_url)
        headers = self._auth_headers() if _same_origin(source_url, self.config.base_url) else {}
        async with self.client.stream("GET", source_url, headers=headers) as response:
            response.raise_for_status()
            name = _response_filename(response, source_url)
            content_type = response.headers.get("content-type")
            yield RemoteFile(name, source_url, content_type, response.aiter_bytes())

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float | None] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_authenticated()
        response = await self.client.get(path, params=params, headers=self._auth_headers())
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise ValueError(f"CTFd returned an unsuccessful response for {path}")
        return payload

    async def _ensure_authenticated(self) -> None:
        if self.config.token or self._logged_in:
            return
        if not self.config.username or not self.config.password:
            raise ValueError("CTFd token or username/password is required")
        login_page = await self.client.get("/login")
        login_page.raise_for_status()
        match = re.search(r'name="nonce"[^>]*value="([^"]+)"', login_page.text)
        if match is None:
            match = re.search(r'id="nonce"[^>]*value="([^"]+)"', login_page.text)
        if match is None:
            raise ValueError("CTFd login nonce was not found")
        response = await self.client.post(
            "/login",
            data={
                "name": self.config.username,
                "password": self.config.password,
                "nonce": match.group(1),
                "_submit": "Submit",
            },
        )
        response.raise_for_status()
        if urlparse(str(response.url)).path == "/login":
            raise ValueError("CTFd login did not establish a session")
        self._logged_in = True

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.config.token}"} if self.config.token else {}


class CTFdSynchronizer:
    def __init__(self, catalog: ChallengeCatalog, client: CTFdSourceClient) -> None:
        self.catalog = catalog
        self.client = client

    async def sync(
        self,
        competition_id,
        *,
        download_files: bool = True,
    ) -> CTFdSyncResult:
        challenges = await self.client.fetch_all_challenges()
        artifacts_created = 0
        artifacts_reused = 0
        for payload in challenges:
            challenge = await asyncio.to_thread(
                self.catalog.upsert_ctfd_challenge,
                competition_id,
                payload,
            )
            if not download_files:
                continue
            for raw_file in payload.get("files") or []:
                raw_url = _file_url(raw_file)
                async with self.client.stream_file(raw_url) as remote:
                    stored = await self.catalog.storage.store_async(
                        challenge.id,
                        remote.name,
                        remote.chunks,
                        declared_content_type=remote.content_type,
                    )
                    result: ArtifactIngestionResult = await asyncio.to_thread(
                        self.catalog.record_stored_artifact,
                        challenge.id,
                        stored,
                        idempotency_key=(
                            f"ctfd-artifact:{competition_id}:{payload['id']}:{stored.sha256}"
                        ),
                        source_url=remote.source_url,
                    )
                    if result.duplicate:
                        artifacts_reused += 1
                    else:
                        artifacts_created += 1
        return CTFdSyncResult(
            challenges_seen=len(challenges),
            challenges_synced=len(challenges),
            artifacts_created=artifacts_created,
            artifacts_reused=artifacts_reused,
        )


def _same_origin(first: str, second: str) -> bool:
    left = urlparse(first)
    right = urlparse(second)
    try:
        left_origin = (left.scheme, left.hostname, left.port)
        right_origin = (right.scheme, right.hostname, right.port)
    except ValueError:
        return False
    return left_origin == right_origin


def _response_filename(response: httpx.Response, source_url: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    if match:
        return unquote(match.group(1).strip())
    return unquote(PurePosixPath(urlparse(source_url).path).name) or "artifact.bin"


def _file_url(value: object) -> str:
    if isinstance(value, dict):
        entry = cast(dict[str, Any], value)
        for key in ("location", "url", "path"):
            if entry.get(key):
                return str(entry[key])
        raise ValueError("CTFd file entry has no URL")
    return str(value)
