"""The /api/processing surface: what exists, how it is configured, and the files.

Products are served from their own route rather than from the captures one so that
retention, caching and permissions can diverge later without untangling them. The
path traversal guard is the same shape as the capture route's, for the same reason:
`archive_date` and friends arrive from the client.
"""

from __future__ import annotations

from pathlib import Path
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import structlog

from app.config import get_settings
from app.db.database import get_db_session
from app.processing.base import CATEGORIES, registered_processors
from app.processing.pipeline import pipeline
from app.processing.products import product_to_dict
from app.processing.retention import (
    GLOBAL_SCOPE,
    RetentionRepository,
    apply_retention,
    describe_policies,
)
from app.processing.video import ffmpeg_available, ffmpeg_version
from app.repositories.processing_repository import (
    DerivedProductRepository,
    ProcessingSessionRepository,
    ProcessingSettingsRepository,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/processing", tags=["processing"])

SAFE_PART = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_part(value: str) -> str:
    """Reject anything that could climb out of the derived directory."""
    if not value or not SAFE_PART.match(value) or value in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid path component")

    return value


class ProcessorSettingsUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    config: dict | None = None


class SessionCloseRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=100)
    archive_date: str = Field(min_length=8, max_length=20)
    # Any session kind, not only the two the sun produces. Constrained to safe
    # path characters because it becomes a directory name.
    period: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9._-]+$")


class SessionOpenRequest(SessionCloseRequest):
    label: str | None = Field(default=None, max_length=120)


class RetentionUpdate(BaseModel):
    node_id: str | None = Field(default=None, max_length=100)
    keep_days: int | None = Field(default=None, ge=1, le=36500)
    max_gb: float | None = Field(default=None, ge=0.1, le=100000)
    keep_versions: int | None = Field(default=None, ge=1, le=100)


def session_to_dict(record) -> dict:
    return {
        "session_key": record.session_key,
        "node_id": record.node_id,
        "archive_date": record.archive_date,
        "period": record.period,
        "session_kind": record.session_kind,
        "label": record.label,
        "status": record.status,
        "frame_count": record.frame_count,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "last_frame_at": record.last_frame_at.isoformat() if record.last_frame_at else None,
        "closed_at": record.closed_at.isoformat() if record.closed_at else None,
        "processor_state": record.processor_state or {},
        # Live progress for a running session; the last recorded state for a
        # closed one, which is what a page loaded after the fact should show.
        "progress": pipeline.progress_for(record.session_key) or (record.progress or {}),
    }


@router.get("/status")
async def processing_status(db: Session = Depends(get_db_session)) -> dict:
    """Everything the settings screen needs in one call.

    Includes each processor's declared config fields, so the UI renders controls
    for a processor added after the UI was written without being changed.
    """
    settings_repo = ProcessingSettingsRepository(db)
    processors = []

    for name, processor_class in sorted(registered_processors().items()):
        stored = settings_repo.get_or_create(processor_class)

        processors.append(
            {
                **processor_class.describe(),
                "enabled": stored.enabled,
                "priority": stored.priority,
                "config": processor_class.coerce_config(stored.config),
                "available": not processor_class.requires_ffmpeg or ffmpeg_available(),
                "unavailable_reason": (
                    "ffmpeg is not installed on this server"
                    if processor_class.requires_ffmpeg and not ffmpeg_available()
                    else None
                ),
            }
        )

    return {
        "pipeline": pipeline.stats(),
        "ffmpeg": {"available": ffmpeg_available(), "version": ffmpeg_version()},
        "processors": processors,
        "progress": pipeline.progress_snapshot(),
        "categories": list(CATEGORIES),
    }


@router.put("/processors/{name}")
async def update_processor(
    name: str,
    payload: ProcessorSettingsUpdate,
    db: Session = Depends(get_db_session),
) -> dict:
    processor_class = registered_processors().get(name)

    if processor_class is None:
        raise HTTPException(status_code=404, detail="No such processor")

    stored = ProcessingSettingsRepository(db).update(
        processor_class, payload.model_dump(exclude_none=True)
    )

    logger.info("processing.settings_updated", processor=name, enabled=stored.enabled)

    return {
        **processor_class.describe(),
        "enabled": stored.enabled,
        "priority": stored.priority,
        "config": processor_class.coerce_config(stored.config),
    }


@router.get("/products")
async def list_products(
    node_id: str | None = None,
    archive_date: str | None = None,
    period: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    processor: str | None = None,
    state: str | None = None,
    metadata_key: str | None = Query(default=None, description="Only products carrying this metadata key"),
    metadata_value: str | None = Query(default=None, description="…and this value, compared as text"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db_session),
) -> dict:
    records = DerivedProductRepository(db).list(
        node_id=node_id,
        archive_date=archive_date,
        period=period,
        kind=kind,
        category=category,
        processor=processor,
        state=state,
        metadata_key=metadata_key,
        metadata_value=metadata_value,
        limit=limit,
    )

    return {"products": [product_to_dict(record) for record in records]}


@router.get("/products/{product_id:path}/detail", include_in_schema=False)
async def product_detail(product_id: str, db: Session = Depends(get_db_session)) -> dict:
    record = DerivedProductRepository(db).get(product_id)

    if record is None:
        raise HTTPException(status_code=404, detail="No such product")

    return product_to_dict(record)


@router.get("/products/dates")
async def product_dates(node_id: str | None = None, db: Session = Depends(get_db_session)) -> dict:
    return {"dates": DerivedProductRepository(db).dates(node_id)}


@router.get("/sessions")
async def list_sessions(
    node_id: str | None = None,
    limit: int = Query(default=60, ge=1, le=500),
    db: Session = Depends(get_db_session),
) -> dict:
    records = ProcessingSessionRepository(db).list_recent(node_id=node_id, limit=limit)

    return {"sessions": [session_to_dict(record) for record in records]}


@router.post("/sessions/close")
async def close_session(payload: SessionCloseRequest) -> dict:
    """Finalise a session now instead of waiting for sunrise.

    Useful for testing a configuration change without waiting twelve hours, and
    for recovering a session whose period change was missed while the server was
    down.
    """
    result = await pipeline.close_session(payload.node_id, payload.archive_date, payload.period)

    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="No such session")

    return result


@router.post("/sessions/open")
async def open_session(payload: SessionOpenRequest) -> dict:
    """Start a session by hand.

    For everything the sun does not schedule: a focus test, a meteor shower, a
    run against a node that only captures on demand. The period watcher leaves a
    manual session alone, so it stays open until it is closed the same way.
    """
    return await pipeline.open_manual_session(
        payload.node_id, payload.archive_date, payload.period, payload.label
    )


@router.get("/retention")
async def get_retention(node_id: str | None = None) -> dict:
    """The rule that would apply to each category, for a node or globally."""
    return {
        "scope": node_id or GLOBAL_SCOPE,
        "categories": list(CATEGORIES),
        "policies": describe_policies(node_id),
    }


@router.put("/retention/{category}")
async def update_retention(
    category: str,
    payload: RetentionUpdate,
    db: Session = Depends(get_db_session),
) -> dict:
    if category not in CATEGORIES:
        raise HTTPException(status_code=404, detail="No such category")

    scope = payload.node_id or GLOBAL_SCOPE
    record = RetentionRepository(db).upsert(scope, category, payload.model_dump(exclude_none=True))

    logger.info(
        "processing.retention_updated",
        scope=scope,
        category=category,
        keep_days=record.keep_days,
        max_gb=record.max_gb,
    )

    return {
        "scope": record.scope,
        "category": record.category,
        "keep_days": record.keep_days,
        "max_gb": record.max_gb,
        "keep_versions": record.keep_versions,
    }


@router.post("/retention/apply")
async def run_retention(dry_run: bool = Query(default=True)) -> dict:
    """Sweep now. Defaults to a dry run, so the first call always reports rather than deletes."""
    return await run_in_threadpool(apply_retention, dry_run)


@router.get("/products/{node_id}/{archive_date}/{period}/{filename}", include_in_schema=False)
async def product_file(
    node_id: str,
    archive_date: str,
    period: str,
    filename: str,
    request: Request,
) -> FileResponse:
    settings = get_settings()
    relative = Path(safe_part(node_id)) / safe_part(archive_date) / safe_part(period) / safe_part(filename)
    path = (settings.derived_dir / relative).resolve()

    # Belt and braces: the components are already validated, but resolving and
    # re-checking means a symlink inside the tree cannot point outside it either.
    if not path.is_relative_to(settings.derived_dir.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="No such product")

    stat_result = path.stat()
    # Live products are rewritten every capture, so the browser must revalidate
    # rather than serve the copy it has. The ETag makes that one cheap 304.
    etag = f'W/"{path.name}-{stat_result.st_mtime_ns}-{stat_result.st_size}"'

    if request.headers.get("if-none-match") == etag:
        from fastapi.responses import Response

        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    return FileResponse(
        path,
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )
