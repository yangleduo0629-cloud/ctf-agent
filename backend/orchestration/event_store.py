from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from backend.db.base import utc_now
from backend.db.models import DomainEvent, EventDelivery


class IdempotencyConflictError(ValueError):
    pass


class EventStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        aggregate_version: int,
        event_type: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        correlation_id: uuid.UUID | None = None,
        causation_id: uuid.UUID | None = None,
    ) -> DomainEvent:
        event = DomainEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            event_type=event_type,
            payload=dict(payload),
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            causation_id=causation_id,
            occurred_at=utc_now(),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def get_by_idempotency_key(self, key: str) -> DomainEvent | None:
        return self.session.scalar(select(DomainEvent).where(DomainEvent.idempotency_key == key))

    def require_matching(
        self,
        event: DomainEvent,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload_values: Mapping[str, object],
    ) -> None:
        matches = (
            event.aggregate_type == aggregate_type
            and event.aggregate_id == aggregate_id
            and event.event_type == event_type
            and all(event.payload.get(key) == value for key, value in payload_values.items())
        )
        if not matches:
            raise IdempotencyConflictError(
                f"idempotency key {event.idempotency_key!r} belongs to another operation"
            )

    def list_after(self, sequence: int = 0, *, limit: int = 500) -> list[DomainEvent]:
        statement = (
            select(DomainEvent)
            .where(DomainEvent.sequence > sequence)
            .order_by(DomainEvent.sequence)
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def pending_delivery(self, transport: str, *, limit: int = 100) -> list[DomainEvent]:
        statement = (
            select(DomainEvent)
            .outerjoin(
                EventDelivery,
                and_(
                    EventDelivery.event_id == DomainEvent.id,
                    EventDelivery.transport == transport,
                    EventDelivery.deleted_at.is_(None),
                ),
            )
            .where(or_(EventDelivery.id.is_(None), EventDelivery.delivered_at.is_(None)))
            .order_by(DomainEvent.sequence)
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def record_delivery(
        self,
        event_id: uuid.UUID,
        transport: str,
        *,
        error: str | None = None,
    ) -> EventDelivery:
        delivery = self.session.scalar(
            select(EventDelivery).where(
                EventDelivery.event_id == event_id,
                EventDelivery.transport == transport,
                EventDelivery.deleted_at.is_(None),
            )
        )
        if delivery is None:
            delivery = EventDelivery(event_id=event_id, transport=transport, attempts=0)
            self.session.add(delivery)
        delivery.attempts += 1
        delivery.last_error = error
        if error is None:
            delivery.delivered_at = utc_now()
        self.session.flush()
        return delivery


def event_to_dict(event: DomainEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "id": str(event.id),
        "aggregate_type": event.aggregate_type,
        "aggregate_id": str(event.aggregate_id),
        "aggregate_version": event.aggregate_version,
        "event_type": event.event_type,
        "payload": event.payload,
        "idempotency_key": event.idempotency_key,
        "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        "causation_id": str(event.causation_id) if event.causation_id else None,
        "occurred_at": event.occurred_at.isoformat(),
    }
