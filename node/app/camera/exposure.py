"""Mean-target auto exposure/gain for allsky cameras.

libcamera's built-in AEC/AGC is tuned for handheld daylight photography: it caps
exposure at the frame duration, re-converges over several frames, and has no
concept of a night sky. This module is a port of the "mode mean" controller from
the AllskyTeam/allsky project (src/mode_mean.cpp), which drives the image toward
a target mean brightness instead.

The core idea is that exposure time and gain are collapsed into a single
logarithmic "exposure level" measured in stops:

    level = log2(gain * exposure_seconds) * shutter_steps ** 2

Working in that domain means a correction is a multiplicative change in captured
light, which is what actually matters, and it lets the controller trade exposure
against gain afterwards under a single policy: stretch exposure first, only add
gain (and therefore noise) once exposure is maxed out.
"""

from dataclasses import dataclass, field
import math

# Tuned defaults from allsky's src/include/allsky_common.h.repo.
DEFAULT_DAY_MEAN = 0.5
DEFAULT_NIGHT_MEAN = 0.2

# allsky ships 0.1 here, but the threshold is an absolute deadband: against a
# night target of 0.2 that accepts anything from 0.1 to 0.3, and the loop settles
# around 0.115 - a 43% error. 0.02 settles within 3% and is still wide enough to
# stop the exposure from twitching frame to frame.
DEFAULT_MEAN_THRESHOLD = 0.02
DEFAULT_P0 = 5.0
DEFAULT_P1 = 20.0
DEFAULT_P2 = 45.0

# One level unit is 2**(1/36) ~= 1.9% more light; the 50-unit clamp caps a single
# correction at roughly 2.6x so the loop cannot oscillate wildly.
SHUTTER_STEPS = 6.0
HISTORY_SIZE = 3
MAX_EXPOSURE_CHANGE = 50

MODE_OFF = "off"
MODE_AUTO = "auto"
MODE_EXPOSURE_ONLY = "exposure"
MODE_GAIN_ONLY = "gain"


@dataclass
class ExposureLimits:
    min_exposure_us: int = 100
    max_exposure_us: int = 60_000_000
    min_gain: float = 1.0
    max_gain: float = 16.0

    def __post_init__(self):
        self.min_exposure_us = max(1, int(self.min_exposure_us))
        self.max_exposure_us = max(self.min_exposure_us, int(self.max_exposure_us))
        self.min_gain = max(0.1, float(self.min_gain))
        self.max_gain = max(self.min_gain, float(self.max_gain))


@dataclass
class ExposureState:
    exposure_us: int
    gain: float
    level: int
    mean: float | None = None
    target_mean: float = DEFAULT_NIGHT_MEAN
    within_threshold: bool = False
    at_limit: bool = False


@dataclass
class MeanTargetController:
    """Feedback controller driving image mean brightness toward a target."""

    target_mean: float = DEFAULT_NIGHT_MEAN
    threshold: float = DEFAULT_MEAN_THRESHOLD
    limits: ExposureLimits = field(default_factory=ExposureLimits)
    mode: str = MODE_AUTO
    exposure_us: int = 1_000_000
    gain: float = 1.0
    shutter_steps: float = SHUTTER_STEPS
    history_size: int = HISTORY_SIZE
    p0: float = DEFAULT_P0
    p1: float = DEFAULT_P1
    p2: float = DEFAULT_P2

    def __post_init__(self):
        self.reset(self.exposure_us, self.gain)

    def reset(self, exposure_us: int, gain: float) -> None:
        self.exposure_us = self._clamp_exposure(exposure_us)
        self.gain = self._clamp_gain(gain)
        self._level = self._exposure_level(self.exposure_us, self.gain)
        self._mean_history: list[float] = [self.target_mean] * self.history_size
        self._count = 0
        self._fast_forward = False
        self._last_mean: float | None = None
        self._level_min, self._level_max = self._level_bounds()

    def configure(
        self,
        target_mean: float | None = None,
        threshold: float | None = None,
        limits: ExposureLimits | None = None,
        mode: str | None = None,
    ) -> bool:
        """Apply new targets. Returns True when the change forced a reset."""
        changed = False

        if target_mean is not None and target_mean != self.target_mean:
            self.target_mean = float(target_mean)
            changed = True

        if threshold is not None and threshold != self.threshold:
            self.threshold = float(threshold)

        if limits is not None and limits != self.limits:
            self.limits = limits
            changed = True

        if mode is not None and mode != self.mode:
            self.mode = mode
            changed = True

        if changed:
            # A day/night switch moves the target far enough that the old history
            # would fight the new setpoint for several frames.
            self.reset(self.exposure_us, self.gain)

        return changed

    @property
    def state(self) -> ExposureState:
        return ExposureState(
            exposure_us=self.exposure_us,
            gain=self.gain,
            level=self._level,
            mean=self._last_mean,
            target_mean=self.target_mean,
            within_threshold=(
                self._last_mean is not None
                and abs(self._last_mean - self.target_mean) <= self.threshold
            ),
            at_limit=self._level in (self._level_min, self._level_max),
        )

    def update(
        self,
        measured_mean: float,
        actual_exposure_us: float | None = None,
        actual_gain: float | None = None,
    ) -> ExposureState:
        """Feed the mean of the frame just captured, get settings for the next.

        Pass the exposure and gain the sensor actually used for that frame when
        they are known. Controls take a frame or two to land and libcamera clamps
        silently, so reasoning from the commanded values instead integrates error
        that was never really applied.
        """
        if self.mode == MODE_OFF:
            self._last_mean = measured_mean
            return self.state

        if actual_exposure_us and actual_gain:
            self._level = self._exposure_level(actual_exposure_us, actual_gain)
            self._level = max(self._level_min, min(self._level_max, self._level))

        measured_mean = min(1.0, max(0.0, float(measured_mean)))
        self._last_mean = measured_mean
        self._mean_history[self._count % self.history_size] = measured_mean

        index = self._count % self.history_size
        previous_index = (self._count + self.history_size - 1) % self.history_size

        # Linear extrapolation of the trend, so a sky that is still darkening gets
        # ahead of the change instead of always lagging one frame behind.
        forecast = (2.0 * self._mean_history[index]) - self._mean_history[previous_index]
        forecast = min(1.0, max(0.0, forecast))

        # Weighted average of recent means, most recent weighted highest, with the
        # forecast carrying the same weight as the whole history.
        weights_total = sum(range(1, self.history_size + 1)) + self.history_size
        predicted_mean = 0.0

        for offset in range(1, self.history_size + 1):
            predicted_mean += self._mean_history[(self._count + offset) % self.history_size] * offset

        predicted_mean += forecast * self.history_size
        predicted_mean /= weights_total

        change = self._exposure_change(
            predicted_diff=abs(predicted_mean - self.target_mean),
            measured_diff=abs(measured_mean - self.target_mean),
        )

        if measured_mean < self.target_mean - self.threshold:
            if self.gain < self.limits.max_gain or self.exposure_us < self.limits.max_exposure_us:
                self._level += change
        elif measured_mean > self.target_mean + self.threshold:
            if self.gain > self.limits.min_gain or self.exposure_us > self.limits.min_exposure_us:
                self._level -= change

        self._level = max(self._level_min, min(self._level_max, self._level))
        self._update_fast_forward(index, previous_index)
        self._apply_level()
        self._count += 1

        return self.state

    def _exposure_change(self, predicted_diff: float, measured_diff: float) -> int:
        # Three gears: the further the mean is from target, the more aggressively
        # the polynomial ramps, so recovery is fast but settling stays gentle.
        if self._fast_forward or measured_diff > self.threshold * 1.75:
            change = self.p0 + self.p1 * predicted_diff + (self.p2 * predicted_diff) ** 2.0
        elif measured_diff > self.threshold * 1.25:
            change = self.p0 + self.p1 * predicted_diff + ((self.p2 * predicted_diff) ** 2.0) / 2.0
        elif measured_diff > self.threshold:
            change = self.p0 + self.p1 * predicted_diff
        else:
            return int(self.shutter_steps / 2)

        return int(min(MAX_EXPOSURE_CHANGE, max(1.0, change)))

    def _update_fast_forward(self, index: int, previous_index: int) -> None:
        if self._level in (self._level_min, self._level_max):
            self._fast_forward = True
            return

        settled = (
            abs(self._mean_history[index] - self.target_mean) < self.threshold
            and abs(self._mean_history[previous_index] - self.target_mean) < self.threshold
        )

        if self._fast_forward and settled:
            self._fast_forward = False

    def _apply_level(self) -> None:
        effective_us = self._effective_exposure_us(self._level)

        if self.mode == MODE_AUTO:
            # Push exposure to its maximum before touching gain: long exposures
            # catch meteors, gain only adds noise.
            gain = min(
                self.limits.max_gain,
                max(self.limits.min_gain, effective_us / self.limits.max_exposure_us),
            )
            exposure_us = min(
                self.limits.max_exposure_us,
                max(self.limits.min_exposure_us, effective_us / gain),
            )
        elif self.mode == MODE_GAIN_ONLY:
            gain = min(
                self.limits.max_gain,
                max(self.limits.min_gain, effective_us / self.exposure_us),
            )
            exposure_us = self.exposure_us
        elif self.mode == MODE_EXPOSURE_ONLY:
            gain = self.gain
            exposure_us = min(
                self.limits.max_exposure_us,
                max(self.limits.min_exposure_us, effective_us / gain),
            )
        else:
            return

        self.gain = round(self._clamp_gain(gain), 3)
        self.exposure_us = self._clamp_exposure(exposure_us)

    def _level_bounds(self) -> tuple[int, int]:
        if self.mode == MODE_GAIN_ONLY:
            low = self._exposure_level(self.exposure_us, self.limits.min_gain)
            high = self._exposure_level(self.exposure_us, self.limits.max_gain)
        elif self.mode == MODE_EXPOSURE_ONLY:
            low = self._exposure_level(self.limits.min_exposure_us, self.gain)
            high = self._exposure_level(self.limits.max_exposure_us, self.gain)
        else:
            low = self._exposure_level(self.limits.min_exposure_us, self.limits.min_gain)
            high = self._exposure_level(self.limits.max_exposure_us, self.limits.max_gain)

        return low - 1, high + 1

    def _exposure_level(self, exposure_us: float, gain: float) -> int:
        light = max(1e-9, gain * exposure_us / 1_000_000)
        return int(math.log2(light) * self.shutter_steps ** 2)

    def _effective_exposure_us(self, level: int) -> float:
        return (2.0 ** (level / self.shutter_steps ** 2)) * 1_000_000

    def _clamp_exposure(self, exposure_us: float) -> int:
        return int(min(self.limits.max_exposure_us, max(self.limits.min_exposure_us, exposure_us)))

    def _clamp_gain(self, gain: float) -> float:
        return float(min(self.limits.max_gain, max(self.limits.min_gain, gain)))


def circular_mask_bounds(width: int, height: int, radius_fraction: float = 1 / 3):
    """Centre/radius of the region the mean is measured over.

    An allsky frame is a bright circle on a black background; averaging the whole
    frame lets the dead corners drag the mean down and the controller over-exposes
    the sky to compensate.
    """
    radius = max(1, int(min(width, height) * radius_fraction))
    return width // 2, height // 2, radius
