from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.base import utc_now
from backend.db.enums import ChallengeStatus, SolverRunStatus
from backend.db.models import Artifact, Challenge, Competition, SolverRun
from backend.db.session import session_scope
from backend.ingestion.storage import ArtifactStorage, StoredArtifact
from backend.orchestration.event_store import EventStore
from backend.orchestration.services import StateTransitionService


@dataclass(frozen=True)
class ArtifactIngestionResult:
    artifact: Artifact
    duplicate: bool


@dataclass(frozen=True)
class ServiceEndpoint:
    protocol: str | None
    host: str | None
    port: int | None


class ChallengeCatalog:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        storage: ArtifactStorage,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage

    def create_competition(
        self,
        *,
        name: str,
        slug: str,
        platform: str,
        idempotency_key: str,
        external_id: str | None = None,
        flag_format: str = "flag{...}",
        flag_regex: str = r"^flag\{[^}\r\n]+\}$",
        details: dict[str, Any] | None = None,
    ) -> Competition:
        re.compile(flag_regex)
        with session_scope(self.session_factory) as session:
            events = EventStore(session)
            if existing_event := events.get_by_idempotency_key(idempotency_key):
                events.require_matching(
                    existing_event,
                    aggregate_type="competition",
                    aggregate_id=existing_event.aggregate_id,
                    event_type="competition.created",
                    payload_values={"slug": slug, "platform": platform},
                )
                competition = session.get(Competition, existing_event.aggregate_id)
                if competition is None:
                    raise LookupError(f"Competition not found: {existing_event.aggregate_id}")
                return competition

            competition = Competition(
                name=name,
                slug=slug,
                platform=platform,
                external_id=external_id,
                flag_format=flag_format,
                flag_regex=flag_regex,
                details=details or {},
            )
            session.add(competition)
            session.flush()
            events.append(
                aggregate_type="competition",
                aggregate_id=competition.id,
                aggregate_version=competition.version,
                event_type="competition.created",
                payload={"slug": slug, "platform": platform},
                idempotency_key=idempotency_key,
            )
            return competition

    def create_challenge(
        self,
        competition_id: uuid.UUID,
        *,
        name: str,
        slug: str,
        category: str,
        idempotency_key: str,
        description: str = "",
        points: int = 0,
        external_id: str | None = None,
        connection_info: str | None = None,
        service_protocol: str | None = None,
        service_host: str | None = None,
        service_port: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> Challenge:
        parsed = parse_connection_info(connection_info)
        protocol = service_protocol or parsed.protocol
        host = service_host or parsed.host
        port = service_port or parsed.port
        _validate_port(port)
        with session_scope(self.session_factory) as session:
            competition = session.get(Competition, competition_id)
            if competition is None or competition.deleted_at is not None:
                raise LookupError(f"Competition not found: {competition_id}")
            events = EventStore(session)
            if existing_event := events.get_by_idempotency_key(idempotency_key):
                events.require_matching(
                    existing_event,
                    aggregate_type="challenge",
                    aggregate_id=existing_event.aggregate_id,
                    event_type="challenge.created",
                    payload_values={"competition_id": str(competition_id), "slug": slug},
                )
                challenge = session.get(Challenge, existing_event.aggregate_id)
                if challenge is None:
                    raise LookupError(f"Challenge not found: {existing_event.aggregate_id}")
                return challenge

            challenge = Challenge(
                competition_id=competition_id,
                external_id=external_id,
                slug=slug,
                name=name,
                category=category,
                description=description,
                points=points,
                connection_info=connection_info,
                service_protocol=protocol,
                service_host=host,
                service_port=port,
                details=details or {},
            )
            session.add(challenge)
            session.flush()
            events.append(
                aggregate_type="challenge",
                aggregate_id=challenge.id,
                aggregate_version=challenge.version,
                event_type="challenge.created",
                payload={"competition_id": str(competition_id), "slug": slug},
                idempotency_key=idempotency_key,
            )
            return challenge

    def add_artifact_file(
        self,
        challenge_id: uuid.UUID,
        source_path: Path,
        *,
        idempotency_key: str,
        declared_content_type: str | None = None,
        source_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ArtifactIngestionResult:
        with source_path.open("rb") as source:
            stored = self.storage.store_fileobj(
                challenge_id,
                source_path.name,
                source,
                declared_content_type=declared_content_type,
            )
        return self.record_stored_artifact(
            challenge_id,
            stored,
            idempotency_key=idempotency_key,
            source_url=source_url,
            details=details,
        )

    def add_artifact_fileobj(
        self,
        challenge_id: uuid.UUID,
        original_name: str,
        source,
        *,
        idempotency_key: str,
        declared_content_type: str | None = None,
        source_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ArtifactIngestionResult:
        stored = self.storage.store_fileobj(
            challenge_id,
            original_name,
            source,
            declared_content_type=declared_content_type,
        )
        return self.record_stored_artifact(
            challenge_id,
            stored,
            idempotency_key=idempotency_key,
            source_url=source_url,
            details=details,
        )

    def record_stored_artifact(
        self,
        challenge_id: uuid.UUID,
        stored: StoredArtifact,
        *,
        idempotency_key: str,
        source_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ArtifactIngestionResult:
        try:
            with session_scope(self.session_factory) as session:
                challenge = session.scalar(
                    select(Challenge)
                    .where(Challenge.id == challenge_id, Challenge.deleted_at.is_(None))
                    .with_for_update()
                )
                if challenge is None:
                    raise LookupError(f"Challenge not found: {challenge_id}")
                events = EventStore(session)
                if existing_event := events.get_by_idempotency_key(idempotency_key):
                    events.require_matching(
                        existing_event,
                        aggregate_type="challenge",
                        aggregate_id=challenge_id,
                        event_type="artifact.created",
                        payload_values={"sha256": stored.sha256},
                    )
                    artifact = session.get(
                        Artifact,
                        uuid.UUID(str(existing_event.payload["artifact_id"])),
                    )
                    if artifact is None:
                        raise LookupError("Artifact referenced by event was not found")
                    self.storage.remove(stored.storage_key)
                    return ArtifactIngestionResult(artifact, True)

                duplicate = session.scalar(
                    select(Artifact).where(
                        Artifact.challenge_id == challenge_id,
                        Artifact.sha256 == stored.sha256,
                        Artifact.deleted_at.is_(None),
                    )
                )
                if duplicate is not None:
                    self.storage.remove(stored.storage_key)
                    events.append(
                        aggregate_type="challenge",
                        aggregate_id=challenge_id,
                        aggregate_version=challenge.version,
                        event_type="artifact.created",
                        payload={
                            "artifact_id": str(duplicate.id),
                            "sha256": stored.sha256,
                            "storage_key": duplicate.storage_key,
                            "duplicate": True,
                        },
                        idempotency_key=idempotency_key,
                    )
                    return ArtifactIngestionResult(duplicate, True)

                artifact = Artifact(
                    challenge_id=challenge_id,
                    original_name=stored.original_name,
                    storage_key=stored.storage_key,
                    content_type=stored.content_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    source_url=source_url,
                    details=details or {},
                )
                session.add(artifact)
                challenge.updated_at = utc_now()
                session.flush()
                events.append(
                    aggregate_type="challenge",
                    aggregate_id=challenge_id,
                    aggregate_version=challenge.version,
                    event_type="artifact.created",
                    payload={
                        "artifact_id": str(artifact.id),
                        "sha256": stored.sha256,
                        "storage_key": stored.storage_key,
                    },
                    idempotency_key=idempotency_key,
                )
                if challenge.status == ChallengeStatus.NEW:
                    StateTransitionService(session).transition_challenge(
                        challenge_id,
                        ChallengeStatus.INGESTED,
                        idempotency_key=f"challenge-ingested:{challenge_id}",
                        reason="artifact available",
                    )
                return ArtifactIngestionResult(artifact, False)
        except Exception:
            self.storage.remove(stored.storage_key)
            raise

    def create_solver_run(
        self,
        challenge_id: uuid.UUID,
        *,
        run_key: str,
        solver_type: str,
        model_spec: str,
        idempotency_key: str,
        attempt: int = 1,
    ) -> SolverRun:
        with session_scope(self.session_factory) as session:
            challenge = session.get(Challenge, challenge_id)
            if challenge is None or challenge.deleted_at is not None:
                raise LookupError(f"Challenge not found: {challenge_id}")
            events = EventStore(session)
            if existing_event := events.get_by_idempotency_key(idempotency_key):
                events.require_matching(
                    existing_event,
                    aggregate_type="solver_run",
                    aggregate_id=existing_event.aggregate_id,
                    event_type="solver_run.created",
                    payload_values={"challenge_id": str(challenge_id), "run_key": run_key},
                )
                solver_run = session.get(SolverRun, existing_event.aggregate_id)
                if solver_run is None:
                    raise LookupError(f"SolverRun not found: {existing_event.aggregate_id}")
                return solver_run

            solver_run = SolverRun(
                challenge_id=challenge_id,
                run_key=run_key,
                attempt=attempt,
                solver_type=solver_type,
                model_spec=model_spec,
                status=SolverRunStatus.QUEUED,
            )
            session.add(solver_run)
            session.flush()
            events.append(
                aggregate_type="solver_run",
                aggregate_id=solver_run.id,
                aggregate_version=solver_run.version,
                event_type="solver_run.created",
                payload={"challenge_id": str(challenge_id), "run_key": run_key},
                idempotency_key=idempotency_key,
            )
            return solver_run

    def upsert_ctfd_challenge(
        self,
        competition_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> Challenge:
        external_id = str(payload["id"])
        connection_info = str(payload.get("connection_info") or "") or None
        endpoint = parse_connection_info(connection_info)
        normalized = {
            "id": external_id,
            "name": str(payload.get("name") or f"Challenge {external_id}"),
            "category": str(payload.get("category") or ""),
            "description": str(payload.get("description") or ""),
            "points": int(payload.get("value") or 0),
            "connection_info": connection_info,
            "files": _normalize_files(payload.get("files") or []),
            "tags": _normalize_tags(payload.get("tags") or []),
            "hints": payload.get("hints") or [],
            "solves": int(payload.get("solves") or 0),
        }
        payload_hash = normalized_payload_hash(normalized)
        event_key = f"ctfd-sync:{competition_id}:{external_id}:{payload_hash}"
        with session_scope(self.session_factory) as session:
            competition = session.get(Competition, competition_id)
            if competition is None or competition.deleted_at is not None:
                raise LookupError(f"Competition not found: {competition_id}")
            if competition.platform != "ctfd":
                raise ValueError("CTFd synchronization requires a ctfd competition")

            challenge = session.scalar(
                select(Challenge)
                .where(
                    Challenge.competition_id == competition_id,
                    Challenge.external_id == external_id,
                    Challenge.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if challenge is None:
                base_slug = slugify(normalized["name"])
                slug = self._unique_challenge_slug(session, competition_id, base_slug, external_id)
                challenge = Challenge(
                    competition_id=competition_id,
                    external_id=external_id,
                    slug=slug,
                    name=normalized["name"],
                    category=normalized["category"],
                )
                session.add(challenge)
                session.flush()

            events = EventStore(session)
            if events.get_by_idempotency_key(event_key) is None:
                challenge.name = normalized["name"]
                challenge.category = normalized["category"]
                challenge.description = normalized["description"]
                challenge.points = normalized["points"]
                challenge.connection_info = connection_info
                challenge.service_protocol = endpoint.protocol
                challenge.service_host = endpoint.host
                challenge.service_port = endpoint.port
                challenge.details = {
                    "source": "ctfd",
                    "tags": normalized["tags"],
                    "hints": normalized["hints"],
                    "solves": normalized["solves"],
                    "files": normalized["files"],
                    "source_payload_sha256": payload_hash,
                }
                session.flush()
                events.append(
                    aggregate_type="challenge",
                    aggregate_id=challenge.id,
                    aggregate_version=challenge.version,
                    event_type="challenge.synced",
                    payload={
                        "competition_id": str(competition_id),
                        "external_id": external_id,
                        "source_payload_sha256": payload_hash,
                    },
                    idempotency_key=event_key,
                )
            if challenge.status == ChallengeStatus.NEW:
                StateTransitionService(session).transition_challenge(
                    challenge.id,
                    ChallengeStatus.INGESTED,
                    idempotency_key=f"challenge-ingested:{challenge.id}",
                    reason="CTFd metadata synchronized",
                )
            return challenge

    @staticmethod
    def _unique_challenge_slug(
        session: Session,
        competition_id: uuid.UUID,
        base_slug: str,
        external_id: str,
    ) -> str:
        existing = session.scalar(
            select(Challenge.id).where(
                Challenge.competition_id == competition_id,
                Challenge.slug == base_slug,
            )
        )
        if existing is None:
            return base_slug
        suffix = slugify(external_id)[:24]
        return f"{base_slug[: 127 - len(suffix)]}-{suffix}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:128] or "challenge"


def parse_connection_info(connection_info: str | None) -> ServiceEndpoint:
    if not connection_info or not connection_info.strip():
        return ServiceEndpoint(None, None, None)
    value = connection_info.strip()
    if "://" in value:
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError:
            return ServiceEndpoint(parsed.scheme or None, parsed.hostname, None)
        return ServiceEndpoint(parsed.scheme or None, parsed.hostname, port)

    nc_match = re.search(r"(?:^|\s)nc\s+([^\s]+)\s+(\d{1,5})(?:\s|$)", value, re.I)
    if nc_match:
        port = int(nc_match.group(2))
        _validate_port(port)
        return ServiceEndpoint("tcp", nc_match.group(1).strip("[]"), port)

    host_port = re.search(r"\[?([^\s\[\]:]+)\]?:(\d{1,5})(?:\s|$)", value)
    if host_port:
        port = int(host_port.group(2))
        _validate_port(port)
        return ServiceEndpoint("tcp", host_port.group(1), port)
    return ServiceEndpoint(None, None, None)


def normalized_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_tags(tags: list[object]) -> list[str]:
    return [
        str(tag.get("value") or "") if isinstance(tag, dict) else str(tag)
        for tag in tags
    ]


def _normalize_files(files: list[object]) -> list[str]:
    normalized: list[str] = []
    for item in files:
        if isinstance(item, dict):
            entry = cast(dict[str, Any], item)
            location = next(
                (entry.get(key) for key in ("location", "url", "path") if entry.get(key)),
                None,
            )
            if location is None:
                raise ValueError("CTFd file entry has no URL")
            normalized.append(str(location))
        else:
            normalized.append(str(item))
    return normalized


def _validate_port(port: int | None) -> None:
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"service port out of range: {port}")
