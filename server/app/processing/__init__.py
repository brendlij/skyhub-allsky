"""Post-capture processing: derived products built incrementally from captures.

The capture path publishes a frame and forgets about it. Everything downstream -
startrails, keograms, timelapses, and whatever gets added later - lives here,
behind a queue, and can fail without the camera noticing.

    app.processing.base        the Processor contract and its registry
    app.processing.pipeline    the queue, the worker, session lifecycle
    app.processing.images      Pillow primitives shared by processors
    app.processing.video       ffmpeg, wrapped so its absence is reportable
    app.processing.processors  the processors themselves

Import order matters exactly once: `processors` must be imported for the registry
to be populated, which is why it is imported here rather than by whoever asks.
"""

from app.processing import processors  # noqa: F401
from app.processing.base import (  # noqa: F401
    ConfigField,
    FrameEvent,
    ProductDraft,
    Processor,
    SessionContext,
    get_processor,
    register_processor,
    registered_processors,
    session_key_for,
)
from app.processing.pipeline import pipeline  # noqa: F401

__all__ = [
    "ConfigField",
    "FrameEvent",
    "ProductDraft",
    "Processor",
    "SessionContext",
    "get_processor",
    "pipeline",
    "register_processor",
    "registered_processors",
    "session_key_for",
]
