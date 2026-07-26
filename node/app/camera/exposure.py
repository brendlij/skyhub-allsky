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

One deliberate departure from upstream: the size of a correction comes from the
ratio of target to measured mean rather than from allsky's polynomial in their
absolute difference. See _level_change - the polynomial oscillates instead of
settling once the loop is anywhere near a bright target.
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

# Floor for the measured mean when correcting. A frame that reads as pure black
# would otherwise ask for an infinite correction.
MIN_USABLE_MEAN = 1e-4

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

    def __post_init__(self):
        self.reset(self.exposure_us, self.gain)

    def reset(self, exposure_us: int, gain: float) -> None:
        self.exposure_us = self._clamp_exposure(exposure_us)
        self.gain = self._clamp_gain(gain)
        self._level = self._exposure_level(self.exposure_us, self.gain)
        self._mean_history: list[float] = [self.target_mean] * self.history_size
        self._count = 0
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

        if abs(measured_mean - self.target_mean) > self.threshold:
            self._level += self._level_change(predicted_mean)

        self._level = max(self._level_min, min(self._level_max, self._level))
        self._apply_level()
        self._count += 1

        return self.state

    def _level_change(self, predicted_mean: float) -> int:
        """How many level units to move to reach the target.

        Brightness scales with captured light, so the correction is the *ratio* of
        target to measured, which the level domain expresses directly as its
        logarithm. allsky's original polynomial ramps on the absolute difference of
        the two means instead, and in a multiplicative domain that is the wrong
        shape: it asks for the same correction whether the frame needs halving or
        quartering, so around a bright target the loop steps straight past it and
        settles into a permanent two-frame oscillation - one blown frame, one dark
        frame, forever - instead of converging.

        Being proportional to the log error, this shrinks as the target is
        approached, which is what makes it settle instead of ringing.
        """
        ratio = self.target_mean / max(MIN_USABLE_MEAN, predicted_mean)
        change = math.log2(ratio) * self.shutter_steps ** 2

        return int(max(-MAX_EXPOSURE_CHANGE, min(MAX_EXPOSURE_CHANGE, change)))

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
