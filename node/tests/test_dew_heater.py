"""Checks for the dew heater automation logic.

Run with:  python node/tests/test_dew_heater.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.heater.auto import AutoHeaterConfig, DewHeaterLogic, spread_celsius  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
        return

    failures.append(f"{label}{f' - {detail}' if detail else ''}")
    print(f"  FAIL  {label} {detail}")


config = AutoHeaterConfig(on_margin_c=3.0, off_margin_c=5.0, min_switch_seconds=120, stale_after_seconds=900)

print("spread")
check("saturated air has no spread", abs(spread_celsius(10.0, 100.0)) < 0.01)
check("dry air has a wide spread", spread_celsius(20.0, 30.0) > 10)

print("switching")
logic = DewHeaterLogic(started_at=0)
check("dry air keeps the heater off", logic.decide(config, 0, False, 20.0, 40.0) is False)
check("near-saturated air turns it on", logic.decide(config, 10, False, 10.0, 95.0) is True)

print("hysteresis and minimum time")
check("recovering air does not switch it off before the minimum time", logic.decide(config, 20, True, 10.0, 50.0) is True)
check("it switches off once the minimum time has passed", logic.decide(config, 200, True, 10.0, 50.0) is False)

logic = DewHeaterLogic(started_at=0)
logic.decide(config, 0, False, 10.0, 95.0)
check("a spread between the thresholds keeps it on", logic.decide(config, 500, True, 10.0, 80.0) is True)
check("a spread between the thresholds keeps it off", DewHeaterLogic(0).decide(config, 500, False, 10.0, 80.0) is False)

print("staleness")
logic = DewHeaterLogic(started_at=0)
check("fresh readings are not stale", not logic.is_stale(config, 100))
check("no reading for the limit is stale", logic.is_stale(config, 901))
logic.decide(config, 900, False, 20.0, 40.0)
check("a reading resets staleness", not logic.is_stale(config, 1000))

print("config")
bad = AutoHeaterConfig.from_dict({"on_margin_c": 6, "off_margin_c": 2, "min_switch_seconds": "x"})
check("off margin is forced above the on margin", bad.off_margin_c > bad.on_margin_c)
check("garbage falls back to the default", bad.min_switch_seconds == 120.0)
check("missing config uses defaults", AutoHeaterConfig.from_dict(None) == AutoHeaterConfig())

if failures:
    print(f"\n{len(failures)} check(s) failed:")

    for failure in failures:
        print(f"  - {failure}")

    sys.exit(1)

print("\nall checks passed")
