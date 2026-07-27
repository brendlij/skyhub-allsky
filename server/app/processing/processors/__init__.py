"""Importing this package is what makes processors exist.

Each module registers itself with the registry on import, so adding a processor is
a new module plus one line here - nothing in the capture path, the API or the UI
changes. Order is the import order only; actual run order comes from the priority
stored per processor.
"""

from app.processing.processors import (  # noqa: F401
    keogram,
    startrail,
    startrail_build,
    timelapse,
)

__all__ = ["keogram", "startrail", "startrail_build", "timelapse"]
