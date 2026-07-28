"""The site is the single source of truth for every sun calculation.

Two properties are worth a test of their own, because both failed silently and
both produced a system that looked configured and computed for somewhere else:

  * saving a location must reach the next calculation, even if a capture thread
    was reading the old one at the time
  * "tonight" must mean the night in progress, not today's calendar date

Run with:  python server/tests/test_astro_site.py
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

_TEMP_DIR = tempfile.mkdtemp(prefix="skyhub-astro-test-")
os.environ["SKYHUB_SERVER_DATA_DIR"] = _TEMP_DIR

from datetime import date, datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from astral import Observer  # noqa: E402
from astral.sun import elevation  # noqa: E402

from app import astro  # noqa: E402
from app.db.database import SessionLocal, create_db_tables  # noqa: E402
from app.repositories import site_settings_repository  # noqa: E402
from app.repositories.site_settings_repository import SiteSettingsRepository  # noqa: E402

# A real site, well south of where astronomical night stops happening in summer.
HOME = {"latitude": 47.65514, "longitude": 7.80044, "elevation_m": 240.0, "timezone": "Europe/Berlin"}
ELSEWHERE = {"latitude": 52.52, "longitude": 13.405, "elevation_m": 0.0, "timezone": "Europe/Berlin"}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {detail}")


def save(values: dict) -> None:
    with SessionLocal() as db:
        SiteSettingsRepository(db).update(values)

    astro.invalidate()


def main() -> int:
    create_db_tables()

    print("\nthe configured site reaches the calculation")
    save(HOME)
    check("the site is what was saved", astro.site().latitude == HOME["latitude"], str(astro.site()))
    check(
        "the observer carries the elevation",
        astro.observer().elevation == HOME["elevation_m"],
        str(astro.observer()),
    )

    print("\ndark_window agrees with astral for the same observer")
    night = date(2026, 7, 28)
    window = astro.dark_window(night)

    check("there is a window", window is not None)

    if window:
        dusk, dawn = window
        where = Observer(HOME["latitude"], HOME["longitude"], HOME["elevation_m"])

        # The definition, checked against astral directly rather than against a
        # remembered number: at both ends the sun is at -18, and a minute outside
        # each end it is not.
        check(
            "the sun is 18 degrees down across the window",
            round(elevation(where, dusk), 1) == -18.0 and round(elevation(where, dawn), 1) == -18.0,
            f"{elevation(where, dusk):.2f} / {elevation(where, dawn):.2f}",
        )
        check(
            "it is not, on either side of it",
            elevation(where, dusk - timedelta(minutes=2)) > -18.0
            and elevation(where, dawn + timedelta(minutes=2)) > -18.0,
        )
        check("dusk precedes dawn", dusk < dawn, f"{dusk} -> {dawn}")
        check(
            "the window is one night long, not one day",
            (dawn - dusk).total_seconds() / 3600 < 14,
            f"{(dawn - dusk).total_seconds() / 3600:.2f} h",
        )

    print("\na night that never gets dark says so")
    save({**ELSEWHERE, "latitude": 60.0})
    check("no window above the arctic-ish limit in midsummer", astro.dark_window(date(2026, 6, 21)) is None)

    print("\ntonight is the night in progress, not today's date")
    zone = ZoneInfo("Europe/Berlin")
    save(HOME)

    check(
        "an evening belongs to its own date",
        astro.current_night(datetime(2026, 7, 28, 23, 30, tzinfo=zone)) == date(2026, 7, 28),
    )
    check(
        "after midnight still belongs to the night that began yesterday",
        astro.current_night(datetime(2026, 7, 29, 1, 30, tzinfo=zone)) == date(2026, 7, 28),
        str(astro.current_night(datetime(2026, 7, 29, 1, 30, tzinfo=zone))),
    )
    check(
        "the morning after still does",
        astro.current_night(datetime(2026, 7, 29, 5, 0, tzinfo=zone)) == date(2026, 7, 28),
    )

    print("\nsaving a location is not lost to a concurrent read")
    # The failure this reproduces: a capture thread reads the old row, and while
    # it is still reading, the operator saves. If the slow reader is allowed to
    # store what it found, it puts the old location back into the cache that the
    # save just cleared - and nothing invalidates again until the next save, so
    # every sun calculation uses the old site until the process restarts.
    save(ELSEWHERE)
    astro.invalidate()

    original = site_settings_repository.SiteSettingsRepository.get_or_create

    def slow_read(self):
        record = original(self)
        time.sleep(0.4)
        return record

    site_settings_repository.SiteSettingsRepository.get_or_create = slow_read

    reader = threading.Thread(target=astro.site)
    reader.start()
    time.sleep(0.1)

    site_settings_repository.SiteSettingsRepository.get_or_create = original
    save(HOME)
    reader.join()

    check(
        "the saved location survives the slow reader",
        astro.site().latitude == HOME["latitude"],
        f"cache holds {astro.site().latitude}, expected {HOME['latitude']}",
    )

    print()

    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("all checks passed")
    return 0


def test_astro_site():
    """pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
