"""Dew heater automation: switch on when the air is close to its dew point.

Condensation forms on the dome when its surface drops to the dew point, and the
dome is always a little colder than the air. So the quantity to watch is the spread
between air temperature and dew point: the smaller it gets, the closer the dome is
to fogging. Heating early (a few degrees of spread) keeps it clear; heating only at
a spread of zero means wiping a wet dome.

Two thresholds rather than one, so the heater does not chatter around the limit,
and a minimum time between switches so a sensor wobbling around a threshold cannot
cycle the relay.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.environment.calculations import dew_point_celsius


@dataclass(frozen=True)
class AutoHeaterConfig:
    # Heater turns on when the spread falls to this many degrees...
    on_margin_c: float = 3.0
    # ...and stays on until the spread has recovered to this.
    off_margin_c: float = 5.0
    min_switch_seconds: float = 120.0
    # No valid reading for this long and the heater is switched off: a heater left
    # on by a dead sensor is a worse failure than a dome that fogs.
    stale_after_seconds: float = 900.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "AutoHeaterConfig":
        data = data or {}
        defaults = cls()

        def number(key: str, default: float, minimum: float) -> float:
            try:
                return max(minimum, float(data.get(key, default)))
            except (TypeError, ValueError):
                return default

        on_margin = number("on_margin_c", defaults.on_margin_c, 0.0)
        # Off below on would mean the heater turns itself off the moment it turns on.
        off_margin = max(number("off_margin_c", defaults.off_margin_c, 0.0), on_margin + 0.5)

        return cls(
            on_margin_c=on_margin,
            off_margin_c=off_margin,
            min_switch_seconds=number("min_switch_seconds", defaults.min_switch_seconds, 0.0),
            stale_after_seconds=number("stale_after_seconds", defaults.stale_after_seconds, 60.0),
        )


def spread_celsius(temperature_c: float, humidity_percent: float) -> float:
    return temperature_c - dew_point_celsius(temperature_c, humidity_percent)


class DewHeaterLogic:
    """Holds the timing state; the decision itself is a pure function of its inputs."""

    def __init__(self, started_at: float):
        self.last_switch_at: float | None = None
        self.last_reading_at: float = started_at

    def reset(self, now: float) -> None:
        """Forget timing, e.g. after the mode or thresholds changed."""
        self.last_switch_at = None
        self.last_reading_at = now

    def decide(
        self,
        config: AutoHeaterConfig,
        now: float,
        enabled: bool,
        temperature_c: float,
        humidity_percent: float,
    ) -> bool:
        self.last_reading_at = now
        spread = spread_celsius(temperature_c, humidity_percent)
        wanted = spread < config.off_margin_c if enabled else spread <= config.on_margin_c

        if wanted == enabled:
            return enabled

        if self.last_switch_at is not None and now - self.last_switch_at < config.min_switch_seconds:
            return enabled

        self.last_switch_at = now

        return wanted

    def is_stale(self, config: AutoHeaterConfig, now: float) -> bool:
        return now - self.last_reading_at > config.stale_after_seconds
