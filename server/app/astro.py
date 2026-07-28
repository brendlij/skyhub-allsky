"""Where the site is, and where the sun is over it.

Both answers have to be the same everywhere. The overlay prints the sun's
elevation on every frame, the archive splits day from night by sunrise and
sunset, and the startrail now stacks only between astronomical dusk and dawn -
three consumers that would be quietly inconsistent if each built its own
observer from its own source.

`main` cannot be that single source: it imports the processing package, so a
processor importing it back would be a cycle. This module depends on nothing but
the database and astral, and everyone reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo

from astral import Depression, LocationInfo, Observer
from astral import sun as astral_sun
import structlog

logger = structlog.get_logger()

# Astral's own name for the sun 18 degrees down: the end of astronomical
# twilight, when the sun stops contributing any light at all to the sky. Taken
# from the library rather than written as 18 so there is one definition of it.
ASTRONOMICAL = float(Depression.ASTRONOMICAL.value)

# How coarsely a night is scanned when locating dusk and dawn. The sun moves at
# most a quarter of a degree in ten minutes, so no crossing can hide between two
# samples; the exact time comes from bisecting the interval that brackets it.
SAMPLE_MINUTES = 10


@dataclass(frozen=True)
class Site:
    """The configured location, detached from the database session it came from."""

    label: str
    latitude: float
    longitude: float
    elevation_m: float
    timezone: str

    @property
    def observer(self) -> Observer:
        return Observer(self.latitude, self.longitude, self.elevation_m)

    @property
    def location(self) -> LocationInfo:
        return LocationInfo(
            name=self.label or "SkyHub",
            region="",
            timezone=self.timezone,
            latitude=self.latitude,
            longitude=self.longitude,
        )

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)

        except (ValueError, KeyError):
            logger.warning("astro.bad_timezone", timezone=self.timezone)
            return ZoneInfo("UTC")


_cached: Site | None = None
_lock = Lock()

# Bumped on every invalidation. A read that began before the site was saved
# carries the generation it started under, and is refused if it no longer
# matches - see `site` for why that matters.
_generation = 0


def site() -> Site:
    """The configured site, read once and held.

    Every capture asks for this several times, and the processing worker asks
    again per frame from another thread, so it is cached rather than fetched.
    `invalidate` is called when the setting is saved, which is the only thing
    that can change it.

    The generation counter closes a lost-update race that is easy to hit and
    very hard to see. A capture thread calls this, misses the cache, and starts
    reading the row; while it is reading, the operator saves a new location and
    `invalidate` clears the cache. If the reader then simply stored what it
    found, it would put the *old* location back into an already-invalidated
    cache - and because nothing invalidates again until the next save, every sun
    calculation in the process would keep using the old location until a
    restart. Refusing to install a value read under a superseded generation
    makes the stale reader drop its result instead.
    """
    global _cached

    with _lock:
        if _cached is not None:
            return _cached

        read_at = _generation

    # Imported here rather than at module scope: the repository pulls in the
    # database, and this module is imported by processors that must stay
    # importable without one.
    from app.db.database import SessionLocal
    from app.repositories.site_settings_repository import SiteSettingsRepository

    with SessionLocal() as db:
        record = SiteSettingsRepository(db).get_or_create()
        resolved = Site(
            label=record.label or "",
            latitude=float(record.latitude),
            longitude=float(record.longitude),
            elevation_m=float(record.elevation_m or 0.0),
            timezone=record.timezone,
        )

    with _lock:
        if read_at == _generation:
            _cached = resolved

    # Returned either way: it is what the database held when this call started,
    # which is the best answer this call can honestly give. Only the cache is
    # protected, because only the cache outlives the call.
    return resolved


def invalidate() -> None:
    """Forget the cached site. Called when it is saved."""
    global _cached, _generation

    with _lock:
        _cached = None
        _generation += 1


def observer() -> Observer:
    return site().observer


def location() -> LocationInfo:
    return site().location


def timezone_name() -> str:
    return site().timezone


def local_zone() -> ZoneInfo:
    return site().zone


def sun_elevation(moment: datetime) -> float | None:
    """Degrees above the horizon, negative below. None if astral could not say.

    Wrapped rather than left to raise, for the same reason the overlay wraps it:
    astral has edge cases at extreme latitudes, and a frame whose sun position is
    unknown should fall through rather than fail the capture that carried it.
    """
    try:
        return float(astral_sun.elevation(observer(), moment))

    except Exception as error:
        logger.warning("astro.elevation_failed", moment=moment.isoformat(), error=str(error))
        return None


def archive_period(moment: datetime) -> tuple[str, str]:
    """Which archive a capture belongs in: its date, and "day" or "night".

    Sunrise and sunset at the configured site, nothing else. A capture after
    sunset belongs to the night that started that evening; one before sunrise
    belongs to the night that started the evening before, which is why the date
    is returned alongside rather than left to the caller to guess.

    Lives here rather than in `main` because the pipeline needs it too, and
    `main` imports the pipeline.
    """
    zone = local_zone()
    local_time = moment.astimezone(zone)
    today = astral_sun.sun(observer(), date=local_time.date(), tzinfo=zone)

    if today["sunrise"] <= local_time < today["sunset"]:
        return local_time.date().isoformat(), "day"

    if local_time >= today["sunset"]:
        return local_time.date().isoformat(), "night"

    return (local_time.date() - timedelta(days=1)).isoformat(), "night"


def current_period() -> str:
    """"day" or "night", right now."""
    return archive_period(datetime.now(timezone.utc))[1]


def current_night(moment: datetime | None = None) -> date:
    """The date of the night in progress, which is not always today's date.

    Nights are named for the date they began, so at 01:00 the night in progress
    began yesterday. Taking `date.today()` instead would describe the night that
    has not started yet - so a settings page opened at 01:00, standing in the
    dark, would report tomorrow's dusk and dawn, and a startrail asked about
    "tonight" would answer about the wrong session. Local midday is the split,
    the same convention `dark_window` and the archive use.
    """
    local = (moment or datetime.now(local_zone())).astimezone(local_zone())

    return local.date() if local.hour >= 12 else local.date() - timedelta(days=1)


def is_dark(moment: datetime, depression: float = ASTRONOMICAL) -> bool:
    """Whether the sun is at least `depression` degrees below the horizon.

    This is the definition of astronomical night, asked of one instant. It is
    what decides whether a frame belongs in a startrail, in preference to
    checking the frame's timestamp against a pair of dusk and dawn times: the
    definition has no calendar in it, so there is no night for it to attribute a
    frame to and no midnight for it to be caught out by.
    """
    elevation = sun_elevation(moment)

    return elevation is not None and elevation <= -abs(depression)


def sky_position(moment: datetime) -> dict[str, float]:
    """Where the sun and moon are, for stamping onto a capture.

    A camera node knows its exposure and its sensor temperature; it does not know
    where on Earth it is standing. The server does, so it is the server that can
    answer this - and the pipeline's ambient collector has been looking for
    `sun_altitude` on every frame since it was written, finding nothing, because
    nothing was putting it there.

    Silent about anything astral will not answer, so a value that cannot be
    computed is absent from the metadata rather than present and wrong.
    """
    from astral import moon as astral_moon

    where = observer()
    values: dict[str, float] = {}

    try:
        values["sun_altitude"] = round(astral_sun.elevation(where, moment), 2)
        values["sun_azimuth"] = round(astral_sun.azimuth(where, moment), 2)

    except Exception:
        pass

    try:
        values["moon_altitude"] = round(astral_moon.elevation(where, moment), 2)

    except Exception:
        pass

    return values


def dark_window(
    night_date: date, depression: float = ASTRONOMICAL
) -> tuple[datetime, datetime] | None:
    """When the sun is that far down on a given night: dusk, then dawn.

    Nights are named for the date they began, matching the archive, so the search
    runs from local midday to local midday. At the default depression the two
    times are astronomical dusk and astronomical dawn.

    None when there is no such window. North of roughly 48.5 degrees the sun
    never gets 18 degrees down for some weeks around midsummer, and there is no
    honest answer other than "the sky does not get that dark tonight" - this does
    not invent a substitute, it leaves that decision to the caller.

    Found by scanning elevation rather than by asking astral for the dusk on a
    date. Astral answers that question per calendar day, and in a short summer
    night dusk falls after midnight - so two dusks share one date, none falls on
    another, and the answer for a given night can be the wrong night's event by a
    whole day. Elevation has no such ambiguity.

    One deliberate difference from `astral.sun.dusk`: astral applies a horizon
    dip correction for the observer's height, so at 240 m its "18 degrees" dusk
    is really the sun at 18.5 degrees down and lands six minutes late. That
    correction belongs to sunrise and sunset, where standing higher genuinely
    lets you see further over the horizon; astronomical twilight is a statement
    about how much light is left in the sky, and 18 degrees means 18 degrees at
    any altitude. Scanning elevation gives the undipped definition, which is
    also what Clear Outside and timeanddate report.
    """
    threshold = -abs(depression)
    zone = local_zone()
    where = observer()
    start = datetime.combine(night_date, time(12, 0), tzinfo=zone)

    steps = (24 * 60) // SAMPLE_MINUTES
    step = timedelta(minutes=SAMPLE_MINUTES)

    def elevation_at(moment: datetime) -> float:
        return float(astral_sun.elevation(where, moment))

    try:
        samples = [elevation_at(start + step * index) for index in range(steps + 1)]

    except Exception as error:
        logger.warning("astro.dark_window_failed", night=night_date.isoformat(), error=str(error))
        return None

    below = [index for index, elevation in enumerate(samples) if elevation <= threshold]

    if not below:
        logger.info(
            "astro.never_that_dark",
            night=night_date.isoformat(),
            depression=depression,
            darkest=round(min(samples), 2),
        )
        return None

    first, last = below[0], below[-1]

    # The scan brackets each crossing to within one sample; bisection turns that
    # into a time worth printing. Only the two boundaries are refined, so this is
    # a few dozen more evaluations rather than a finer scan of the whole night.
    dusk = (
        _crossing(elevation_at, start + step * (first - 1), start + step * first, threshold)
        if first
        else start
    )
    dawn = (
        _crossing(elevation_at, start + step * last, start + step * (last + 1), threshold)
        if last < steps
        else start + step * steps
    )

    return dusk, dawn


def _crossing(elevation_at, before: datetime, after: datetime, threshold: float) -> datetime:
    """Bisect an interval known to contain one threshold crossing, to the second."""
    while (after - before) > timedelta(seconds=1):
        middle = before + (after - before) / 2

        if (elevation_at(middle) <= threshold) == (elevation_at(after) <= threshold):
            after = middle
        else:
            before = middle

    return after
