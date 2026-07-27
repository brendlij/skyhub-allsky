"""The processor contract and the registry that finds them.

A processor is a class that says what it is called, what it can be configured
with, and what to do when a frame arrives or a session ends. It never touches the
database, never talks to the API, and never knows about any other processor. That
is the whole extensibility story: adding meteor detection later means adding one
module and importing it, and nothing in the capture path changes.

Three hooks, all optional:

    on_session_start   a night began - allocate whatever you accumulate into
    on_frame           a capture arrived - update incrementally, cheaply
    on_session_end     the night ended - do the expensive encoding here

`on_frame` runs in a worker thread on every capture, so it has a budget measured
against the capture interval. `on_session_end` runs once and may take minutes.

Both return `ProductDraft` objects describing what they wrote. The pipeline turns
those into database rows and dashboard events; a processor never does that itself,
so products from a processor written next year are listed and served for free.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class FrameEvent:
    """One captured image, as handed to every processor."""

    node_id: str
    archive_date: str
    period: str
    captured_at: datetime
    # The rendered frame - overlays and hue correction applied, mask burned in.
    # This is what the UI shows, so products built from it match what the operator
    # sees. `original_path` is the untouched copy, for processors that need it.
    rendered_path: Path
    original_path: Path | None = None
    thumbnail_path: Path | None = None
    sequence_id: str | None = None
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return session_key_for(self.node_id, self.archive_date, self.period)


def session_key_for(node_id: str, archive_date: str, period: str) -> str:
    return f"{node_id}/{archive_date}/{period}"


# Session kinds. The first two are what the sun produces and are opened
# automatically; the rest exist so a session can be something else entirely -
# a manual run, a test, a meteor shower - without the pipeline needing to know
# what any of them mean.
SESSION_DAY = "day"
SESSION_NIGHT = "night"
SESSION_MANUAL = "manual"
SESSION_TEST = "test"

# Sessions the sun opens and closes on its own. Anything else is opened by an
# explicit request and closed the same way.
SOLAR_SESSIONS = frozenset({SESSION_DAY, SESSION_NIGHT})

# A processor that declares this runs whatever the session is.
ANY_SESSION = "*"


# Progress stages, in the order a session moves through them. Reported to the API
# so the UI can show what a processor is doing without knowing what it is.
STAGE_IDLE = "idle"
STAGE_RUNNING = "running"
STAGE_ENCODING = "encoding"
STAGE_FINALISING = "finalising"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"


@dataclass
class SessionContext:
    """Everything a processor needs about the run it is accumulating into.

    `state` is the processor's own scratch space, kept in memory for the life of
    the session - the startrail's stack lives here. It is deliberately per
    processor and per session, so two nodes stacking at once cannot collide.

    `shared` is the opposite: the one place processors can see each other. A
    processor publishes to it under its own name and reads other processors' keys
    through `consume`, which is what lets the build-video processor use the stack
    the startrail processor already computed instead of recomputing it. The
    coupling stays loose because a missing key is a `None`, not an error - a
    processor whose dependency is disabled degrades rather than crashes.
    """

    node_id: str
    archive_date: str
    # The session's kind: "day", "night", "manual", "test", or anything a caller
    # invents. Named `period` still because that is what it is for the two the
    # sun produces, and renaming it would churn every processor for nothing.
    period: str
    config: dict[str, Any]
    # Working directory for this processor's intermediate files. Created lazily;
    # safe to fill with as much as needed, and wiped when the session closes.
    work_dir: Path
    # Where finished products go. Survives the session.
    output_dir: Path
    state: dict[str, Any] = field(default_factory=dict)
    frame_count: int = 0
    started_at: datetime | None = None

    # Set by the pipeline when it builds the context.
    processor_name: str = ""
    shared: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Injected by the pipeline. Default is a no-op so a context built by hand -
    # in a test, say - still works without wiring a reporter.
    progress: Callable[..., None] = lambda *args, **kwargs: None

    @property
    def session_key(self) -> str:
        return session_key_for(self.node_id, self.archive_date, self.period)

    def ensure_dirs(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ---- sharing between processors ----

    def share(self, key: str, value: Any) -> None:
        """Publish something for dependent processors to consume."""
        self.shared.setdefault(self.processor_name, {})[key] = value

    def consume(self, processor: str, key: str, default: Any = None) -> Any:
        """Read a value another processor published, or `default` if it did not.

        Deliberately forgiving. A dependency that is disabled, failed, or simply
        has not run yet gives `default`, so the consumer decides whether that is
        fatal - the pipeline does not decide for it.
        """
        return self.shared.get(processor, {}).get(key, default)

    def report(self, stage: str, percent: float | None = None, detail: str = "") -> None:
        """Say what this processor is doing. Cheap; safe to call often."""
        self.progress(stage=stage, percent=percent, detail=detail)


# Retention and grouping buckets. A processor picks the one its output belongs to
# rather than inventing its own, so a retention rule written for "timelapse"
# covers a timelapse a processor added next year without being updated.
CATEGORY_STARTRAIL = "startrail"
CATEGORY_KEOGRAM = "keogram"
CATEGORY_TIMELAPSE = "timelapse"
CATEGORY_ANALYSIS = "analysis"
CATEGORY_REPORT = "report"
CATEGORY_TEMPORARY = "temporary"

CATEGORIES = (
    CATEGORY_STARTRAIL,
    CATEGORY_KEOGRAM,
    CATEGORY_TIMELAPSE,
    CATEGORY_ANALYSIS,
    CATEGORY_REPORT,
    CATEGORY_TEMPORARY,
)

# Variant widths. "web" is what a browser is served in a gallery; "preview" is a
# thumbnail. The original is never modified or replaced - it is the archival copy
# and the only one guaranteed to be exactly what the processor produced.
VARIANT_WEB = "web"
VARIANT_PREVIEW = "preview"

VARIANT_WIDTHS = {VARIANT_WEB: 1600, VARIANT_PREVIEW: 480}


@dataclass
class ProductDraft:
    """A file a processor produced, described well enough for the API to serve it.

    A processor's whole responsibility ends here: write a file, describe it,
    return it. Storage layout, variants, thumbnails, versioning, metadata,
    database rows, retention and API exposure are the Derived Product Manager's
    job, so every product behaves the same regardless of which processor made it.
    """

    kind: str
    path: Path
    media_type: str
    state: str = "live"
    frame_count: int = 0
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Which retention and grouping bucket this belongs to. Defaults are inferred
    # from the processor when a draft does not say, so existing processors keep
    # working unchanged.
    category: str = ""

    # Variants the manager should derive. Empty by default for live products -
    # they are rewritten every capture and already small, so deriving two more
    # files per frame would be pure waste. Final products get the full set.
    variants: tuple[str, ...] | None = None

    # Set to False for products that are rewritten constantly. Version counting a
    # live keogram would reach several hundred by dawn and mean nothing.
    versioned: bool = True

    @property
    def product_type(self) -> str:
        """The generic name for `kind`, matching the product model's vocabulary."""
        return self.kind

    def variants_to_build(self) -> tuple[str, ...]:
        if self.variants is not None:
            return self.variants

        return () if self.state == "live" else (VARIANT_WEB, VARIANT_PREVIEW)


class ConfigField:
    """One tunable, declared so the UI can render it without knowing the processor."""

    def __init__(
        self,
        key: str,
        label: str,
        kind: str,
        default: Any,
        *,
        help_text: str = "",
        minimum: float | None = None,
        maximum: float | None = None,
        choices: list[str] | None = None,
    ):
        self.key = key
        self.label = label
        # "int" | "float" | "bool" | "text" | "choice"
        self.kind = kind
        self.default = default
        self.help_text = help_text
        self.minimum = minimum
        self.maximum = maximum
        self.choices = choices

    def coerce(self, value: Any) -> Any:
        """Force a submitted value into range and type, or fall back to the default.

        Config arrives from the API as JSON, so it is user input. Clamping rather
        than rejecting keeps a typo from disabling a processor for a whole night -
        the frame that is being captured right now matters more than the setting
        being exactly what was asked for.
        """
        try:
            if self.kind == "bool":
                return bool(value)

            if self.kind == "int":
                coerced = int(value)
            elif self.kind == "float":
                coerced = float(value)
            elif self.kind == "choice":
                text = str(value)
                return text if (not self.choices or text in self.choices) else self.default
            else:
                # Bounded so a pathological string cannot end up in a filename or
                # an ffmpeg argument.
                return str(value)[:200]

        except (TypeError, ValueError):
            return self.default

        if self.minimum is not None:
            coerced = max(self.minimum, coerced)
        if self.maximum is not None:
            coerced = min(self.maximum, coerced)

        return int(coerced) if self.kind == "int" else coerced

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "help_text": self.help_text,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": self.choices,
        }


class Processor(ABC):
    """Base class. Subclass, set the class attributes, override what you need."""

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""

    # Which session kinds this runs for. "day" and "night" are what the sun
    # produces; a session can also be "manual", "test", or anything a caller
    # invents. A processor that should run whatever the session is can say
    # `session_kinds = ANY_SESSION`.
    session_kinds: ClassVar[frozenset[str]] = frozenset({"day", "night"})

    # What this processor's output is, for retention and grouping. A draft can
    # override it per product.
    category: ClassVar[str] = CATEGORY_ANALYSIS

    # Processors whose output this one consumes. Purely an ordering hint plus a
    # documented expectation - the dependency is read through `context.consume`,
    # which returns None when it is missing, so a disabled dependency degrades
    # this processor rather than breaking it.
    depends_on: ClassVar[tuple[str, ...]] = ()

    # Whether it needs ffmpeg. The pipeline reports a processor as unavailable
    # rather than letting it fail once per frame.
    requires_ffmpeg: ClassVar[bool] = False

    fields: ClassVar[tuple[ConfigField, ...]] = ()

    default_enabled: ClassVar[bool] = True
    default_priority: ClassVar[int] = 100

    # ---- lifecycle, all optional ----

    def on_session_start(self, session: SessionContext) -> None:
        """Called once before the first frame of a session reaches this processor."""

    def on_resume(self, session: SessionContext) -> None:
        """Called instead of nothing when a restart picks a session back up.

        `on_session_start` still runs first and is where state is reloaded; this
        is the hook for anything that should happen *only* on a resume - warning
        about a gap in the frames, re-deriving something the crash left half
        written. Most processors do not need it.
        """

    def on_frame(self, session: SessionContext, frame: FrameEvent) -> Iterable[ProductDraft]:
        """Called for every capture. Keep it inside the capture interval."""
        return ()

    def on_session_end(self, session: SessionContext) -> Iterable[ProductDraft]:
        """Called once when the session closes. The expensive work belongs here."""
        return ()

    def on_shutdown(self, session: SessionContext) -> None:
        """Called for each open session when the server is stopping.

        The session is *not* being finalised - a restart resumes it. This is for
        flushing in-memory state to disk so that resume picks up where the process
        left off rather than from the last checkpoint.
        """

    def on_error(self, session: SessionContext, hook: str, error: Exception) -> None:
        """Called when one of this processor's own hooks raised.

        Runs after the pipeline has already logged and disabled the processor for
        this session, so it cannot rescue itself - it is for releasing whatever
        the failure stranded. Anything raised here is swallowed.
        """

    # ---- helpers ----

    @classmethod
    def runs_for(cls, session_kind: str) -> bool:
        """Whether this processor takes part in a session of this kind.

        `periods` is the older name for the same idea and is still honoured, so a
        processor written before sessions became generic needs no edit. A
        processor that declares neither runs for day and night.
        """
        declared = getattr(cls, "periods", None) or cls.session_kinds

        return ANY_SESSION in declared or session_kind in declared

    @classmethod
    def kinds(cls) -> frozenset[str]:
        return frozenset(getattr(cls, "periods", None) or cls.session_kinds)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {field_spec.key: field_spec.default for field_spec in cls.fields}

    @classmethod
    def coerce_config(cls, raw: dict[str, Any] | None) -> dict[str, Any]:
        """Merge stored overrides onto the declared defaults, validating as it goes."""
        config = cls.default_config()

        for field_spec in cls.fields:
            if raw and field_spec.key in raw:
                config[field_spec.key] = field_spec.coerce(raw[field_spec.key])

        return config

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "label": cls.label,
            "description": cls.description,
            # Both names: `periods` for anything already reading it, `session_kinds`
            # for what it actually means now.
            "periods": sorted(cls.kinds()),
            "session_kinds": sorted(cls.kinds()),
            "category": cls.category,
            "depends_on": list(cls.depends_on),
            "requires_ffmpeg": cls.requires_ffmpeg,
            "default_enabled": cls.default_enabled,
            "default_priority": cls.default_priority,
            "fields": [field_spec.describe() for field_spec in cls.fields],
        }


_registry: dict[str, type[Processor]] = {}


def register_processor(processor_class: type[Processor]) -> type[Processor]:
    """Class decorator. Import the module and the processor exists - that is all."""
    if not processor_class.name:
        raise ValueError(f"{processor_class.__name__} needs a name")

    if processor_class.name in _registry:
        raise ValueError(f"Duplicate processor name: {processor_class.name}")

    _registry[processor_class.name] = processor_class
    logger.debug("processing.registered", processor=processor_class.name)

    return processor_class


def registered_processors() -> dict[str, type[Processor]]:
    return dict(_registry)


def get_processor(name: str) -> type[Processor] | None:
    return _registry.get(name)


def resolve_order(names: Iterable[str], priorities: dict[str, int] | None = None) -> list[str]:
    """Order processors so a dependency always runs before its consumer.

    A topological sort, with the stored priority as the tie-break so the operator
    still controls the order among processors that do not depend on each other.

    A cycle - or a dependency on a processor that is disabled or does not exist -
    is not an error. The cycle is broken at an arbitrary point and the missing
    dependency is ignored, because the consumer reads through `context.consume`
    and handles absence itself. Refusing to run anything because two processors
    reference each other would be a worse outcome than running them in a
    defensible order.
    """
    priorities = priorities or {}
    remaining = {name for name in names if name in _registry}
    ordered: list[str] = []
    placed: set[str] = set()

    while remaining:
        ready = [
            name for name in remaining
            # Only dependencies that are actually taking part count: one that is
            # disabled or unregistered can never be satisfied, so waiting for it
            # would strand the consumer forever.
            if all(dep in placed or dep not in remaining for dep in _registry[name].depends_on)
        ]

        if not ready:
            # A cycle. Break it at the highest-priority member and carry on.
            ready = [min(remaining, key=lambda name: (priorities.get(name, 100), name))]
            logger.warning("processing.dependency_cycle", processors=sorted(remaining), breaking_at=ready[0])

        for name in sorted(ready, key=lambda item: (priorities.get(item, 100), item)):
            ordered.append(name)
            placed.add(name)
            remaining.discard(name)

    return ordered
