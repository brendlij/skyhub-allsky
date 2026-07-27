"""Persistence for the processing pipeline: settings, sessions and products."""

# Required, not stylistic: DerivedProductRepository defines a method called
# `list`, which shadows the builtin inside the class body. Without deferred
# annotations, `-> list[DerivedProduct]` on any method declared after it is
# evaluated eagerly and resolves to that method, so the module fails to import
# with "'function' object is not subscriptable" on Python 3.13 and earlier.
# Python 3.14 evaluates annotations lazily (PEP 649) and hides the problem.
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.derived_product import DerivedProduct
from app.models.processing_session import ProcessingSession
from app.models.processing_settings import ProcessingSettings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProcessingSettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, processor: str) -> ProcessingSettings | None:
        return self.db.get(ProcessingSettings, processor)

    def get_or_create(self, processor_class) -> ProcessingSettings:
        """The row is created from the processor's own declared defaults.

        Nothing seeds these at startup, so a processor added in a later release
        simply appears with its defaults the first time anything asks for it.
        """
        record = self.get(processor_class.name)

        if record is None:
            record = ProcessingSettings(
                processor=processor_class.name,
                enabled=processor_class.default_enabled,
                priority=processor_class.default_priority,
                config=processor_class.default_config(),
                updated_at=utc_now(),
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)

        return record

    def update(self, processor_class, values: dict[str, Any]) -> ProcessingSettings:
        record = self.get_or_create(processor_class)

        if "enabled" in values:
            record.enabled = bool(values["enabled"])

        if "priority" in values:
            try:
                record.priority = max(0, min(1000, int(values["priority"])))
            except (TypeError, ValueError):
                pass

        if "config" in values and isinstance(values["config"], dict):
            # Merge onto what is stored, then let the processor validate the whole
            # thing - a partial update must not silently reset the other fields.
            merged = {**(record.config or {}), **values["config"]}
            record.config = processor_class.coerce_config(merged)

        record.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(record)

        return record

    def list_all(self) -> list[ProcessingSettings]:
        return self.db.query(ProcessingSettings).all()


class ProcessingSessionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, session_key: str) -> ProcessingSession | None:
        return self.db.get(ProcessingSession, session_key)

    def open(
        self,
        session_key: str,
        node_id: str,
        archive_date: str,
        period: str,
        session_kind: str = "solar",
        label: str | None = None,
    ) -> ProcessingSession:
        record = self.get(session_key)

        if record is None:
            record = ProcessingSession(
                session_key=session_key,
                node_id=node_id,
                archive_date=archive_date,
                period=period,
                session_kind=session_kind,
                label=label,
                status="open",
                frame_count=0,
                started_at=utc_now(),
                processor_state={},
                progress={},
            )
            self.db.add(record)

        elif record.status != "open":
            # A frame arriving after the session closed - a node whose clock is
            # slightly behind the server's at the moment the period flips. Reopen
            # rather than drop it, and let the next close finalise again.
            record.status = "open"
            record.closed_at = None

        self.db.commit()
        self.db.refresh(record)

        return record

    def record_frame(self, session_key: str, captured_at: datetime | None = None) -> None:
        record = self.get(session_key)

        if record is None:
            return

        record.frame_count += 1
        record.last_frame_at = captured_at or utc_now()

        self.db.commit()

    def set_status(self, session_key: str, status: str) -> None:
        record = self.get(session_key)

        if record is None:
            return

        record.status = status

        if status in {"closed", "failed"}:
            record.closed_at = utc_now()

        self.db.commit()

    def set_processor_state(self, session_key: str, processor: str, state: dict[str, Any]) -> None:
        record = self.get(session_key)

        if record is None:
            return

        # Reassigned rather than mutated in place: SQLAlchemy does not see a
        # mutation inside a JSON column, so the write would be silently dropped.
        record.processor_state = {**(record.processor_state or {}), processor: state}

        self.db.commit()

    def set_progress(self, session_key: str, processor: str, state: dict[str, Any]) -> None:
        record = self.get(session_key)

        if record is None:
            return

        # Reassigned, not mutated: SQLAlchemy does not notice a change inside a
        # JSON column, so an in-place update would never be written.
        record.progress = {**(record.progress or {}), processor: state}

        self.db.commit()

    def list_open(self) -> list[ProcessingSession]:
        return (
            self.db.query(ProcessingSession)
            .filter(ProcessingSession.status == "open")
            .order_by(ProcessingSession.started_at.asc())
            .all()
        )

    def list_recent(self, node_id: str | None = None, limit: int = 60) -> list[ProcessingSession]:
        query = self.db.query(ProcessingSession)

        if node_id:
            query = query.filter(ProcessingSession.node_id == node_id)

        return query.order_by(ProcessingSession.archive_date.desc()).limit(limit).all()


class DerivedProductRepository:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def product_id(node_id: str, archive_date: str, period: str, kind: str) -> str:
        return f"{node_id}/{archive_date}/{period}/{kind}"

    def upsert(self, values: dict[str, Any]) -> DerivedProduct:
        """One row per product, replaced in place.

        A live keogram is rewritten on every capture; without an upsert a night
        would leave a row per frame behind.
        """
        product_id = self.product_id(
            values["node_id"], values["archive_date"], values["period"], values["kind"]
        )
        record = self.db.get(DerivedProduct, product_id)
        now = utc_now()

        if record is None:
            record = DerivedProduct(product_id=product_id, created_at=now)
            self.db.add(record)

        for field_name in (
            "node_id", "archive_date", "period", "session_key", "processor", "kind",
            "category", "relative_path", "preview_path", "web_path", "media_type",
            "state", "version", "frame_count", "size_bytes",
            "width", "height", "duration_seconds",
        ):
            if field_name in values:
                setattr(record, field_name, values[field_name])

        if "product_metadata" in values:
            record.product_metadata = values["product_metadata"]

        record.updated_at = now
        self.db.commit()
        self.db.refresh(record)

        return record

    def get(self, product_id: str) -> DerivedProduct | None:
        return self.db.get(DerivedProduct, product_id)

    def list(
        self,
        node_id: str | None = None,
        archive_date: str | None = None,
        period: str | None = None,
        kind: str | None = None,
        category: str | None = None,
        processor: str | None = None,
        state: str | None = None,
        metadata_key: str | None = None,
        metadata_value: str | None = None,
        limit: int = 200,
    ) -> list[DerivedProduct]:
        query = self.db.query(DerivedProduct)

        if node_id:
            query = query.filter(DerivedProduct.node_id == node_id)
        if archive_date:
            query = query.filter(DerivedProduct.archive_date == archive_date)
        if period:
            query = query.filter(DerivedProduct.period == period)
        if kind:
            query = query.filter(DerivedProduct.kind == kind)
        if category:
            query = query.filter(DerivedProduct.category == category)
        if processor:
            query = query.filter(DerivedProduct.processor == processor)
        if state:
            query = query.filter(DerivedProduct.state == state)

        if metadata_key:
            # SQLite has no JSON index, so this is filtered in Python after the
            # column filters have already cut the candidate set down. Adequate for
            # a metadata search on one node's night; not a general query engine.
            records = query.order_by(DerivedProduct.archive_date.desc()).limit(limit * 5).all()

            return [
                record for record in records
                if self._metadata_matches(record, metadata_key, metadata_value)
            ][:limit]

        return (
            query.order_by(
                DerivedProduct.archive_date.desc(),
                DerivedProduct.period.asc(),
                DerivedProduct.kind.asc(),
            )
            .limit(limit)
            .all()
        )

    @staticmethod
    def _metadata_matches(record: DerivedProduct, key: str, value: str | None) -> bool:
        """Whether a product carries this metadata key, and optionally this value.

        Compared as strings, because metadata is arbitrary JSON from processors
        that may store a number as either. An absent value means "has this key at
        all", which is how a caller finds every product a detector flagged.
        """
        metadata = record.product_metadata or {}

        if key not in metadata:
            return False

        return value is None or str(metadata[key]) == str(value)

    def list_for_session(self, session_key: str) -> list[DerivedProduct]:
        return (
            self.db.query(DerivedProduct)
            .filter(DerivedProduct.session_key == session_key)
            .all()
        )

    def dates(self, node_id: str | None = None) -> list[str]:
        query = self.db.query(DerivedProduct.archive_date).distinct()

        if node_id:
            query = query.filter(DerivedProduct.node_id == node_id)

        return sorted((row[0] for row in query.all()), reverse=True)

    def delete_for_session(self, node_id: str, archive_date: str, period: str) -> int:
        removed = (
            self.db.query(DerivedProduct)
            .filter(
                DerivedProduct.node_id == node_id,
                DerivedProduct.archive_date == archive_date,
                DerivedProduct.period == period,
            )
            .delete(synchronize_session=False)
        )
        self.db.commit()

        return removed
