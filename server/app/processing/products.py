"""The Derived Product Manager: the single owner of every generated asset.

A processor's responsibility ends at "here is a file and what it is". Everything
after that happens here, once, for every product:

    storage layout      where it lives, under a predictable path
    variants            web and preview copies, so a browser is never sent a
                        4056px original to display in a gallery tile
    metadata            what the processor attached, plus the ambient conditions
                        the frame was taken in
    versioning          how many times this product has been rebuilt
    status              live, final, or failed - and why
    database rows       one per product, replaced in place
    retention           what is allowed to accumulate, and for how long

The point is uniformity. A product from a processor written next year is stored,
previewed, listed, served and expired exactly like a startrail, because none of
that is the processor's code.

Cloud sync and external storage providers are not implemented, but this is where
they go: `register` is the one place that knows a product now exists, and
`ProductStore` below is the seam a remote backend would implement.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
import structlog

from app.config import get_settings
from app.db.database import SessionLocal
from app.processing.base import (
    CATEGORY_ANALYSIS,
    VARIANT_PREVIEW,
    VARIANT_WEB,
    VARIANT_WIDTHS,
    ProductDraft,
)
from app.processing.images import open_rgb, save_jpeg, scaled_to_width
from app.repositories.processing_repository import DerivedProductRepository

logger = structlog.get_logger()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProductStore:
    """Where product files live.

    Local disk today. A cloud backend would subclass this - `place` returns the
    canonical location, `url_for` the address to serve - and nothing above would
    change, because no processor and no route builds a product path itself.
    """

    def root(self) -> Path:
        return get_settings().derived_dir

    def place(self, node_id: str, archive_date: str, period: str, filename: str) -> Path:
        return self.root() / node_id / archive_date / period / filename

    def relative(self, path: Path) -> str | None:
        try:
            return str(path.relative_to(self.root())).replace("\\", "/")
        except ValueError:
            return None

    def url_for(self, relative_path: str) -> str:
        return f"/api/processing/products/{relative_path}"


store = ProductStore()


def variant_path(source: Path, variant: str) -> Path:
    """`startrail.jpg` + "web" -> `startrail.web.jpg`.

    Beside the original rather than in a subdirectory, so a product and all its
    variants move, sync and expire as one unit.
    """
    return source.with_name(f"{source.stem}.{variant}{source.suffix}")


class ProductManager:
    """Registers products and everything that has to be true about them."""

    def __init__(self, product_store: ProductStore | None = None):
        self.store = product_store or store

    # ---- registration ----

    def register(
        self,
        draft: ProductDraft,
        *,
        node_id: str,
        archive_date: str,
        period: str,
        processor: str,
        session_key: str,
        ambient_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Take a processor's output and make it a first-class product.

        Returns the stored record as a dictionary, or None when the draft cannot
        be accepted - a file that was never written, or one written outside the
        derived directory. Neither is fatal to the session: a broken product is a
        missing product, not a failed night.
        """
        relative_path = self.store.relative(draft.path)

        if relative_path is None:
            logger.warning(
                "products.outside_store", path=str(draft.path), processor=processor
            )
            return None

        exists = draft.path.is_file()

        if not exists and draft.state != "failed":
            logger.warning("products.missing_file", path=str(draft.path), processor=processor)
            return None

        width, height = self._dimensions(draft)
        variants = self._build_variants(draft) if exists else {}
        category = draft.category or CATEGORY_ANALYSIS

        with SessionLocal() as db:
            repository = DerivedProductRepository(db)
            previous = repository.get(
                repository.product_id(node_id, archive_date, period, draft.kind)
            )

            # Versioning counts rebuilds of a finished product, not the hundreds
            # of rewrites a live one goes through before dawn.
            version = (previous.version or 1) if previous else 1

            if previous and draft.versioned and draft.state != "live":
                version = (previous.version or 1) + (1 if previous.state != "live" else 0)

            record = repository.upsert(
                {
                    "node_id": node_id,
                    "archive_date": archive_date,
                    "period": period,
                    "session_key": session_key,
                    "processor": processor,
                    "kind": draft.kind,
                    "category": category,
                    "relative_path": relative_path,
                    "preview_path": variants.get(VARIANT_PREVIEW),
                    "web_path": variants.get(VARIANT_WEB),
                    "media_type": draft.media_type,
                    "state": draft.state,
                    "version": version,
                    "frame_count": draft.frame_count,
                    "size_bytes": draft.path.stat().st_size if exists else 0,
                    "width": width,
                    "height": height,
                    "duration_seconds": draft.duration_seconds,
                    "product_metadata": self._metadata(draft, ambient_metadata),
                }
            )

            return product_to_dict(record)

    # ---- variants ----

    def _build_variants(self, draft: ProductDraft) -> dict[str, str]:
        """Derive the web and preview copies a browser should be served instead.

        Only for images: a video's variants would mean re-encoding it, which is
        the expensive thing the pipeline is careful about. Videos get a poster
        frame instead, produced by the processor that made them if it wants one.

        A source already smaller than the variant target is not copied - a
        480px-wide product does not need a 480px "preview" beside it.
        """
        wanted = draft.variants_to_build()

        if not wanted or not draft.media_type.startswith("image/"):
            return {}

        built: dict[str, str] = {}

        try:
            source = open_rgb(draft.path)

        except (OSError, ValueError) as error:
            logger.warning("products.variant_source_unreadable", path=str(draft.path), error=str(error))
            return {}

        for variant in wanted:
            target_width = VARIANT_WIDTHS.get(variant)

            if not target_width or source.width <= target_width:
                continue

            path = variant_path(draft.path, variant)

            try:
                save_jpeg(scaled_to_width(source, target_width), path, 82)

            except (OSError, ValueError) as error:
                logger.warning("products.variant_failed", variant=variant, error=str(error))
                continue

            relative = self.store.relative(path)

            if relative:
                built[variant] = relative

        return built

    def _dimensions(self, draft: ProductDraft) -> tuple[int | None, int | None]:
        """Trust the processor if it said, otherwise read the file."""
        if draft.width and draft.height:
            return draft.width, draft.height

        if not draft.media_type.startswith("image/") or not draft.path.is_file():
            return draft.width, draft.height

        try:
            with Image.open(draft.path) as image:
                return image.width, image.height

        except (OSError, ValueError):
            return None, None

    def _metadata(
        self, draft: ProductDraft, ambient: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Merge what the processor attached over the conditions of the session.

        The processor wins on conflict: it knows what it measured, the ambient
        block is only the context it measured it in.
        """
        merged = {**(ambient or {}), **(draft.metadata or {})}

        return merged or None

    # ---- session close ----

    def settle_session(self, session_key: str) -> dict[str, int]:
        """Retire the live products of a session that has closed.

        Nothing is "live" once the session ends, and leaving the flag set has two
        real consequences: retention skips live products by design, so they would
        never expire; and the UI shows live products under "building now", so an
        archive date from last March would claim to be in progress.

        A live product whose processor also produced a final one is redundant -
        the final is the same picture - so it is deleted. One with no final
        equivalent is the processor's only output and is kept, demoted to final.
        """
        promoted = 0
        removed = 0

        with SessionLocal() as db:
            repository = DerivedProductRepository(db)
            products = repository.list_for_session(session_key)
            finals = {
                product.processor for product in products if product.state == "final"
            }

            for product in products:
                if product.state != "live":
                    continue

                if product.processor in finals:
                    self.delete(product)
                    db.delete(product)
                    removed += 1
                else:
                    product.state = "final"
                    promoted += 1

            if promoted or removed:
                db.commit()

        if promoted or removed:
            logger.info(
                "products.session_settled",
                session=session_key,
                promoted=promoted,
                removed=removed,
            )

        return {"promoted": promoted, "removed": removed}

    # ---- deletion ----

    def delete(self, record) -> int:
        """Remove a product and every variant of it. Returns bytes reclaimed."""
        freed = 0
        root = self.store.root()

        for relative in (record.relative_path, record.preview_path, record.web_path):
            if not relative:
                continue

            path = root / relative

            if not path.is_file():
                continue

            try:
                freed += path.stat().st_size
                path.unlink()

            except OSError as error:
                logger.warning("products.delete_failed", path=str(path), error=str(error))

        return freed


def product_to_dict(record) -> dict[str, Any]:
    """The API shape of a product. One definition, used by every route."""
    return {
        "product_id": record.product_id,
        "session_key": record.session_key,
        "node_id": record.node_id,
        "archive_date": record.archive_date,
        "period": record.period,
        "processor": record.processor,
        # Both names: `kind` is what the code has always called it, `product_type`
        # is the generic vocabulary.
        "kind": record.kind,
        "product_type": record.kind,
        "category": record.category,
        "state": record.state,
        "status": record.state,
        "version": record.version,
        "media_type": record.media_type,
        "mime_type": record.media_type,
        "url": store.url_for(record.relative_path),
        # A client should prefer these and fall back to `url`. Null means the
        # original is already small enough to serve directly.
        "preview_url": store.url_for(record.preview_path) if record.preview_path else None,
        "web_url": store.url_for(record.web_path) if record.web_path else None,
        "frame_count": record.frame_count,
        "size_bytes": record.size_bytes,
        "file_size": record.size_bytes,
        "width": record.width,
        "height": record.height,
        "duration_seconds": record.duration_seconds,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "metadata": record.product_metadata or {},
    }


manager = ProductManager()
