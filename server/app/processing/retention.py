"""Retention for derived products.

Separate from the capture retention already in `main.py` on purpose: captures are
the raw record and are pruned by one global policy, while derived products differ
wildly in what they are worth per byte. A year of keograms is a few hundred
megabytes and is the most useful thing on the disk; a year of full-resolution
startrail build videos is not.

So the rules are per category, and per node where an operator wants them to be.
Nothing is deleted by default - an upgrade must never start removing an
operator's work because of a default they did not choose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.db.database import SessionLocal
from app.models.derived_product import DerivedProduct
from app.models.retention_policy import RetentionPolicy
from app.processing.base import CATEGORIES
from app.processing.products import manager

logger = structlog.get_logger()

GLOBAL_SCOPE = "global"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def policy_id(scope: str, category: str) -> str:
    return f"{scope}:{category}"


@dataclass
class Policy:
    scope: str
    category: str
    keep_days: int | None = None
    max_gb: float | None = None
    keep_versions: int = 1

    @property
    def unlimited(self) -> bool:
        return not self.keep_days and not self.max_gb

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "category": self.category,
            "keep_days": self.keep_days,
            "max_gb": self.max_gb,
            "keep_versions": self.keep_versions,
        }


class RetentionRepository:
    def __init__(self, db):
        self.db = db

    def get(self, scope: str, category: str) -> RetentionPolicy | None:
        return self.db.get(RetentionPolicy, policy_id(scope, category))

    def effective(self, node_id: str, category: str) -> Policy:
        """The node's rule if it has one, otherwise the global rule, otherwise none."""
        for scope in (node_id, GLOBAL_SCOPE):
            record = self.get(scope, category)

            if record is not None:
                return Policy(
                    scope=record.scope,
                    category=record.category,
                    keep_days=record.keep_days,
                    max_gb=record.max_gb,
                    keep_versions=record.keep_versions,
                )

        return Policy(scope=GLOBAL_SCOPE, category=category)

    def upsert(self, scope: str, category: str, values: dict[str, Any]) -> RetentionPolicy:
        record = self.get(scope, category)

        if record is None:
            record = RetentionPolicy(
                policy_id=policy_id(scope, category), scope=scope, category=category
            )
            self.db.add(record)

        for field_name in ("keep_days", "max_gb", "keep_versions"):
            if field_name not in values:
                continue

            value = values[field_name]

            if value in (None, "", 0) and field_name != "keep_versions":
                # Explicitly clearing a limit rather than setting it to zero:
                # "keep for 0 days" would mean "delete everything immediately",
                # which is never what a cleared field means.
                setattr(record, field_name, None)
                continue

            try:
                if field_name == "max_gb":
                    setattr(record, field_name, max(0.1, float(value)))
                else:
                    setattr(record, field_name, max(1, int(value)))
            except (TypeError, ValueError):
                continue

        record.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(record)

        return record

    def list_all(self) -> list[RetentionPolicy]:
        return self.db.query(RetentionPolicy).order_by(RetentionPolicy.policy_id).all()


def apply_retention(dry_run: bool = False) -> dict[str, Any]:
    """Sweep derived products against their policies.

    Age first, then size. Age is the cheaper test and the one an operator reasons
    about; the size cap is a backstop for a category that produces more than
    expected, and removes oldest-first until it fits.

    Live products are never touched. They belong to a session that is still
    running, and deleting one would leave the UI pointing at nothing for the sake
    of a few kilobytes.
    """
    removed = 0
    freed = 0
    by_category: dict[str, int] = {}

    with SessionLocal() as db:
        repository = RetentionRepository(db)
        products = (
            db.query(DerivedProduct)
            .filter(DerivedProduct.state != "live")
            .order_by(DerivedProduct.archive_date.asc())
            .all()
        )

        if not products:
            return {"removed": 0, "freed_bytes": 0, "dry_run": dry_run, "by_category": {}}

        # Group so each (node, category) is measured against its own rule.
        grouped: dict[tuple[str, str], list[DerivedProduct]] = {}

        for product in products:
            grouped.setdefault((product.node_id, product.category or "analysis"), []).append(product)

        for (node_id, category), items in grouped.items():
            policy = repository.effective(node_id, category)

            if policy.unlimited:
                continue

            doomed: list[DerivedProduct] = []
            survivors: list[DerivedProduct] = []

            if policy.keep_days:
                cutoff = (utc_now() - timedelta(days=policy.keep_days)).date().isoformat()

                for product in items:
                    # Compared as ISO date strings, which sort correctly and avoid
                    # parsing every row.
                    (doomed if product.archive_date < cutoff else survivors).append(product)
            else:
                survivors = list(items)

            if policy.max_gb:
                budget = int(policy.max_gb * 1024 ** 3)
                total = sum(product.size_bytes or 0 for product in survivors)

                # Oldest first: the newest products are the ones being looked at.
                for product in sorted(survivors, key=lambda item: item.archive_date):
                    if total <= budget:
                        break

                    doomed.append(product)
                    total -= product.size_bytes or 0

            for product in doomed:
                if not dry_run:
                    freed += manager.delete(product)
                    db.delete(product)
                else:
                    freed += product.size_bytes or 0

                removed += 1
                by_category[category] = by_category.get(category, 0) + 1

        if not dry_run and removed:
            db.commit()

    if removed:
        logger.info(
            "retention.swept",
            removed=removed,
            freed_bytes=freed,
            dry_run=dry_run,
            by_category=by_category,
        )

    return {"removed": removed, "freed_bytes": freed, "dry_run": dry_run, "by_category": by_category}


def describe_policies(node_id: str | None = None) -> list[dict[str, Any]]:
    """Every category with the rule that would currently apply to it."""
    with SessionLocal() as db:
        repository = RetentionRepository(db)

        return [
            {
                **repository.effective(node_id or GLOBAL_SCOPE, category).as_dict(),
                "category": category,
                "explicit": repository.get(node_id or GLOBAL_SCOPE, category) is not None,
            }
            for category in CATEGORIES
        ]
