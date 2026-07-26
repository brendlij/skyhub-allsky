import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from app.camera.base import CameraInfo, CaptureResult
from app.config import NODE_DIR
from app.camera.exposure import (
    DEFAULT_DAY_MEAN,
    DEFAULT_MEAN_THRESHOLD,
    DEFAULT_NIGHT_MEAN,
    ExposureLimits,
    MODE_AUTO,
    MODE_EXPOSURE_ONLY,
    MODE_GAIN_ONLY,
    MODE_OFF,
    MeanTargetController,
)


logger = structlog.get_logger()

DEFAULT_JPEG_QUALITY = 95
DEFAULT_SETTLE_FRAMES = 3
ASPECT_TOLERANCE = 0.01
DEFAULT_DAY_START_EXPOSURE_US = 5_000
FRAME_DURATION_MARGIN_US = 1_000

# Wall-clock a capture may spend waiting for a control change to reach the sensor
# before it gives up and keeps the next frame regardless. Long night exposures buy
# fewer settle frames than short daylight ones - see _settle_frame_budget.
SETTLE_TIME_BUDGET_US = 30_000_000
SETTLE_EXPOSURE_TOLERANCE = 0.005

# Cheap luma source for the mean-target controller; decoding the full-resolution
# main stream every frame just to average it would cost far more.
METERING_SIZE = (320, 240)

# The Bayer pattern has twice as many green photosites as red or blue, so a frame
# developed with unity colour gains comes out visibly green. These are a workable
# starting point for a Pi HQ camera under a typical light-polluted sky; they are
# meant to be tuned per site from the UI.
DEFAULT_NIGHT_COLOUR_GAINS = (2.2, 1.8)
DAY_SEED_PATH = NODE_DIR / "data" / "picamera2_day_seed.json"


class PiCamera2Camera:
    def __init__(self):
        try:
            from picamera2 import Picamera2
        except ImportError as error:
            raise RuntimeError(
                "The picamera2 camera driver needs the Raspberry Pi picamera2 package. "
                "On Raspberry Pi OS, install it with: sudo apt install -y python3-picamera2"
            ) from error

        self._picamera2 = Picamera2()
        self._configured_key: tuple | None = None
        self._applied_controls: dict[str, Any] | None = None
        self._actual_size: tuple[int, int] | None = None
        self._metering_size: tuple[int, int] | None = None
        self._metering_mask = None
        self._last_frame_metadata: dict[str, Any] | None = None
        self._last_requested_controls: dict[str, Any] | None = None
        self._last_capture_period: str | None = None
        self._day_seed: dict[str, float] | None = self._load_day_seed()
        self._capture_index = 0
        self._needs_settle = True
        self._started = False
        self._controller: MeanTargetController | None = None
        self._controller_key: tuple | None = None
        self._sensor_mode = self._select_sensor_mode()
        self._sensor_size = self._resolve_sensor_size()

        logger.info(
            "picamera2.detected",
            sensor_size=self._sensor_size,
            sensor_mode=self._sensor_mode["size"] if self._sensor_mode else None,
            bit_depth=self._sensor_mode.get("bit_depth") if self._sensor_mode else None,
        )

    def get_info(self) -> CameraInfo:
        return CameraInfo(
            camera_id="picamera2",
            name="Raspberry Pi Camera",
            driver="picamera2",
            supports_exposure=True,
            supports_gain=True,
            supported_formats=["jpg", "png", "dng"],
        )

    async def capture(self, settings: dict[str, Any], output_dir: Path) -> CaptureResult:
        return await asyncio.to_thread(self._capture_sync, settings, output_dir)

    def _capture_sync(self, settings: dict[str, Any], output_dir: Path) -> CaptureResult:
        self._capture_index += 1
        frame_index = self._capture_index
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        stem = f"picamera2_{timestamp}_{uuid4().hex[:8]}"

        image_format = str(settings.get("format") or "jpg").lower()

        if image_format == "jpeg":
            image_format = "jpg"

        if image_format not in {"jpg", "png"}:
            raise ValueError(
                f"PiCamera2Camera writes jpg or png for the main image, got: {image_format}. "
                "Use the raw setting to also store a DNG."
            )

        save_raw = bool(settings.get("raw", False))
        output_path = output_dir / f"{stem}.{image_format}"
        raw_path = output_dir / f"{stem}.dng" if save_raw else None
        frame_metadata: dict[str, Any] = {}
        measured_mean: float | None = None

        size = self._resolve_output_size(settings)
        self._ensure_configured(size=size, save_raw=save_raw, settings=settings)
        controller = self._sync_controller(settings)
        self._apply_controls(settings, controller)
        requested_controls = self._last_requested_controls or {}

        # Only wait out the sensor when something actually changed. Every settle
        # frame costs a full exposure, so on a fixed 60s night exposure blindly
        # settling doubled the cost of each capture and quietly stretched the
        # interval; with the controls unchanged the sensor is already there.
        if self._needs_settle:
            self._settle_after_controls(
                requested_controls,
                int(settings.get("settle_frames", DEFAULT_SETTLE_FRAMES)),
            )
            self._needs_settle = False

        request = self._picamera2.capture_request()

        try:
            request.save("main", str(output_path))

            if raw_path is not None:
                request.save_dng(str(raw_path))

            frame_metadata = dict(request.get_metadata() or {})
            measured_mean = self._measure_mean(request, settings)
        finally:
            request.release()

        self._last_frame_metadata = frame_metadata
        self._last_capture_period = str(settings.get("period") or "night")

        exposure_state = None

        actual_exposure_us = frame_metadata.get("ExposureTime")
        actual_gain = frame_metadata.get("AnalogueGain")

        if controller is not None and measured_mean is not None:
            exposure_state = controller.update(
                measured_mean,
                actual_exposure_us=actual_exposure_us,
                actual_gain=actual_gain,
            )
            logger.info(
                "picamera2.exposure.metered",
                mean=round(measured_mean, 4),
                target_mean=round(exposure_state.target_mean, 4),
                within_threshold=exposure_state.within_threshold,
                at_limit=exposure_state.at_limit,
                next_exposure_ms=round(exposure_state.exposure_us / 1000, 1),
                next_gain=exposure_state.gain,
            )

        period = str(settings.get("period") or "night")

        if (
            period == "day"
            and controller is not None
            and exposure_state is not None
            and exposure_state.within_threshold
            and actual_exposure_us is not None
            and actual_gain is not None
        ):
            self._save_day_seed(int(actual_exposure_us), float(actual_gain))

        if exposure_state is not None:
            logger.info(
                "picamera2.convergence",
                frame_index=frame_index,
                mean=round(measured_mean, 4) if measured_mean is not None else None,
                actual_exposure_ms=self._microseconds_to_ms(actual_exposure_us),
                actual_gain=actual_gain,
                actual_digital_gain=frame_metadata.get("DigitalGain"),
                next_exposure_ms=self._microseconds_to_ms(exposure_state.exposure_us),
                next_gain=exposure_state.gain,
                target_mean=round(exposure_state.target_mean, 4),
                within_threshold=exposure_state.within_threshold,
            )

        logger.info(
            "picamera2.frame",
            frame_index=frame_index,
            requested_exposure_us=requested_controls.get("ExposureTime"),
            requested_gain=requested_controls.get("AnalogueGain"),
            requested_frame_duration_limits=requested_controls.get("FrameDurationLimits"),
            requested_ae_enable=requested_controls.get("AeEnable"),
            requested_colour_gains=requested_controls.get("ColourGains"),
            actual_exposure_us=actual_exposure_us,
            actual_gain=actual_gain,
            actual_digital_gain=frame_metadata.get("DigitalGain"),
            frame_duration=frame_metadata.get("FrameDuration"),
            lux=frame_metadata.get("Lux"),
            ae_enable=frame_metadata.get("AeEnable"),
            ColourGains=list(frame_metadata["ColourGains"])
            if frame_metadata.get("ColourGains")
            else None,
            mean=round(measured_mean, 4) if measured_mean is not None else None,
            controller_target=round(exposure_state.target_mean, 4) if exposure_state else None,
            controller_next_exposure=exposure_state.exposure_us if exposure_state else None,
            controller_next_gain=exposure_state.gain if exposure_state else None,
        )

        width, height = self._actual_size or size

        return CaptureResult(
            file_path=output_path,
            format=image_format,
            width=width,
            height=height,
            metadata={
                "captured_at": now.isoformat(),
                "camera": "picamera2",
                "frame_index": frame_index,
                "auto_exposure": bool(settings.get("auto_exposure", False)),
                "exposure_ms": settings.get("exposure_ms"),
                "auto_gain": bool(settings.get("auto_gain", False)),
                "gain": settings.get("gain"),
                "raw_path": str(raw_path) if raw_path else None,
                "sensor_size": list(self._sensor_size) if self._sensor_size else None,
                "sensor_mode": list(self._sensor_mode["size"]) if self._sensor_mode else None,
                "bit_depth": self._sensor_mode.get("bit_depth") if self._sensor_mode else None,
                "actual_exposure_ms": self._microseconds_to_ms(frame_metadata.get("ExposureTime")),
                "actual_analogue_gain": frame_metadata.get("AnalogueGain"),
                "actual_digital_gain": frame_metadata.get("DigitalGain"),
                "colour_gains": list(frame_metadata["ColourGains"])
                if frame_metadata.get("ColourGains")
                else None,
                "sensor_temperature_c": frame_metadata.get("SensorTemperature"),
                "lux": frame_metadata.get("Lux"),
                "mean": round(measured_mean, 4) if measured_mean is not None else None,
                "target_mean": round(exposure_state.target_mean, 4) if exposure_state else None,
                "mean_within_threshold": exposure_state.within_threshold if exposure_state else None,
                "next_exposure_ms": self._microseconds_to_ms(exposure_state.exposure_us)
                if exposure_state
                else None,
                "next_gain": exposure_state.gain if exposure_state else None,
            },
        )

    def _select_sensor_mode(self) -> dict[str, Any] | None:
        modes = list(getattr(self._picamera2, "sensor_modes", None) or [])

        if not modes:
            return None

        pixel_array = self._picamera2.camera_properties.get("PixelArraySize")

        def covers_full_sensor(mode: dict[str, Any]) -> bool:
            crop_limits = mode.get("crop_limits")

            if not crop_limits or not pixel_array:
                return True

            crop_width, crop_height = crop_limits[2], crop_limits[3]
            return (
                crop_width >= pixel_array[0] * 0.99
                and crop_height >= pixel_array[1] * 0.99
            )

        # A mode with a narrower crop sees less sky, no matter how many pixels it has.
        candidates = [mode for mode in modes if covers_full_sensor(mode)] or modes

        return max(
            candidates,
            key=lambda mode: (mode["size"][0] * mode["size"][1], mode.get("bit_depth", 0)),
        )

    def _resolve_sensor_size(self) -> tuple[int, int] | None:
        if self._sensor_mode:
            width, height = self._sensor_mode["size"]
            return int(width), int(height)

        pixel_array = self._picamera2.camera_properties.get("PixelArraySize")

        if pixel_array:
            return int(pixel_array[0]), int(pixel_array[1])

        return None

    def _full_frame_crop(self) -> tuple[int, int, int, int] | None:
        scaler_crop = (getattr(self._picamera2, "camera_controls", None) or {}).get("ScalerCrop")

        # camera_controls entries are (min, max, default); the max is the whole sensor.
        if scaler_crop and len(scaler_crop) >= 2 and scaler_crop[1]:
            return tuple(int(value) for value in scaler_crop[1])

        pixel_array = self._picamera2.camera_properties.get("PixelArraySize")

        if pixel_array:
            return 0, 0, int(pixel_array[0]), int(pixel_array[1])

        return None

    def _resolve_output_size(self, settings: dict[str, Any]) -> tuple[int, int]:
        requested_width = settings.get("width")
        requested_height = settings.get("height")
        allow_crop = bool(settings.get("allow_crop", False))

        if not self._sensor_size:
            return int(requested_width or 1920), int(requested_height or 1080)

        sensor_width, sensor_height = self._sensor_size
        sensor_aspect = sensor_width / sensor_height

        if not requested_width and not requested_height:
            return sensor_width, sensor_height

        if requested_width and requested_height:
            width, height = int(requested_width), int(requested_height)
            requested_aspect = width / height

            if allow_crop or abs(requested_aspect - sensor_aspect) <= sensor_aspect * ASPECT_TOLERANCE:
                return width, height

            # Honouring a mismatched aspect ratio makes libcamera centre-crop the
            # sensor, which silently cuts the top and bottom off the sky.
            corrected_height = self._even(round(width / sensor_aspect))
            logger.warning(
                "picamera2.aspect_corrected",
                requested=[width, height],
                corrected=[width, corrected_height],
                sensor_size=[sensor_width, sensor_height],
                reason="requested aspect ratio would crop the sensor",
            )
            return width, corrected_height

        if requested_width:
            width = int(requested_width)
            return width, self._even(round(width / sensor_aspect))

        height = int(requested_height)
        return self._even(round(height * sensor_aspect)), height

    def _ensure_configured(
        self,
        size: tuple[int, int],
        save_raw: bool,
        settings: dict[str, Any],
    ) -> None:
        configured_key = (size, save_raw)

        if self._started and self._configured_key == configured_key:
            return

        if self._started:
            self._picamera2.stop()
            self._started = False

        self._picamera2.options["quality"] = int(
            settings.get("jpeg_quality", DEFAULT_JPEG_QUALITY)
        )

        metering_size = (
            min(METERING_SIZE[0], size[0]),
            min(METERING_SIZE[1], size[1]),
        )
        config_kwargs: dict[str, Any] = {
            "main": {"size": size},
            "lores": {"size": metering_size, "format": "YUV420"},
            "buffer_count": 2,
        }

        # The raw stream pins the sensor to its full-resolution readout and is what
        # save_dng() writes out. Both matter for image quality.
        if self._sensor_mode:
            config_kwargs["raw"] = {
                "size": self._sensor_mode["size"],
                "format": str(self._sensor_mode["format"]),
            }
        elif save_raw:
            config_kwargs["raw"] = {}

        config = self._picamera2.create_still_configuration(**config_kwargs)
        self._picamera2.configure(config)

        camera_configuration = self._picamera2.camera_configuration()
        self._actual_size = tuple(camera_configuration["main"]["size"])
        lores_configuration = camera_configuration.get("lores")
        self._metering_size = tuple(lores_configuration["size"]) if lores_configuration else None
        self._metering_mask = None
        self._picamera2.start()
        self._started = True
        self._configured_key = configured_key
        self._applied_controls = None
        self._needs_settle = True

        full_crop = self._full_frame_crop()

        if full_crop:
            # Without this libcamera keeps whatever crop the previous mode left behind.
            self._picamera2.set_controls({"ScalerCrop": full_crop})

        logger.info(
            "picamera2.configured",
            requested_size=list(size),
            actual_size=list(self._actual_size),
            raw=save_raw,
            scaler_crop=list(full_crop) if full_crop else None,
        )

        if self._actual_size != size:
            logger.warning(
                "picamera2.size_adjusted",
                requested=list(size),
                actual=list(self._actual_size),
                reason="libcamera aligned the stream size",
            )

    def _controller_mode(self, settings: dict[str, Any]) -> str:
        auto_exposure = bool(settings.get("auto_exposure", False))
        auto_gain = bool(settings.get("auto_gain", False))

        if auto_exposure and auto_gain:
            return MODE_AUTO

        if auto_exposure:
            return MODE_EXPOSURE_ONLY

        if auto_gain:
            return MODE_GAIN_ONLY

        return MODE_OFF

    def _exposure_limits(self, settings: dict[str, Any]) -> ExposureLimits:
        camera_limits = getattr(self._picamera2, "camera_controls", None) or {}
        exposure_range = camera_limits.get("ExposureTime")
        gain_range = camera_limits.get("AnalogueGain")

        sensor_min_exposure = int(exposure_range[0]) if exposure_range else 100
        sensor_max_exposure = int(exposure_range[1]) if exposure_range else 60_000_000
        sensor_min_gain = float(gain_range[0]) if gain_range else 1.0
        sensor_max_gain = float(gain_range[1]) if gain_range else 16.0

        max_exposure_ms = settings.get("max_exposure_ms")
        max_gain = settings.get("max_gain")

        return ExposureLimits(
            min_exposure_us=sensor_min_exposure,
            max_exposure_us=min(
                sensor_max_exposure,
                int(float(max_exposure_ms) * 1000) if max_exposure_ms else sensor_max_exposure,
            ),
            min_gain=sensor_min_gain,
            max_gain=min(sensor_max_gain, float(max_gain) if max_gain else sensor_max_gain),
        )

    def _sync_controller(self, settings: dict[str, Any]) -> MeanTargetController | None:
        mode = self._controller_mode(settings)

        if mode == MODE_OFF:
            self._controller = None
            self._controller_key = None
            return None

        period = str(settings.get("period") or "night")
        default_target = DEFAULT_DAY_MEAN if period == "day" else DEFAULT_NIGHT_MEAN
        target_mean = float(settings.get("target_mean") or default_target)
        threshold = float(settings.get("mean_threshold") or DEFAULT_MEAN_THRESHOLD)
        limits = self._exposure_limits(settings)
        key = (mode, period, target_mean, threshold, limits)
        seed_exposure_us, seed_gain = self._controller_seed(settings, period)

        if self._controller is None or self._controller_key != key:
            self._controller = MeanTargetController(
                target_mean=target_mean,
                threshold=threshold,
                limits=limits,
                mode=mode,
                exposure_us=seed_exposure_us,
                gain=seed_gain,
            )
            self._controller_key = key
            logger.info(
                "picamera2.exposure.controller_reset",
                mode=mode,
                period=period,
                target_mean=target_mean,
                threshold=threshold,
                seed_exposure_us=seed_exposure_us,
                seed_gain=seed_gain,
                max_exposure_ms=limits.max_exposure_us / 1000,
                max_gain=limits.max_gain,
            )

        return self._controller

    def _controller_seed(self, settings: dict[str, Any], period: str) -> tuple[int, float]:
        last_metadata = self._last_frame_metadata if self._last_capture_period == period else None
        seed_exposure_us: int | None = None
        seed_gain: float | None = None

        if last_metadata is not None:
            actual_exposure_us = last_metadata.get("ExposureTime")
            actual_gain = last_metadata.get("AnalogueGain")

            if actual_exposure_us is not None:
                seed_exposure_us = int(actual_exposure_us)

            if actual_gain is not None:
                seed_gain = float(actual_gain)

        if period == "day":
            if self._day_seed is not None:
                seed_exposure_us = int(self._day_seed.get("exposure_us") or 0) or None
                seed_gain = float(self._day_seed.get("gain") or 0) or None

            return seed_exposure_us or DEFAULT_DAY_START_EXPOSURE_US, seed_gain or 1.0

        exposure_ms = settings.get("exposure_ms")
        gain = settings.get("gain")

        return (
            seed_exposure_us
            or (int(float(exposure_ms) * 1000) if exposure_ms is not None else 1_000_000),
            seed_gain or (float(gain) if gain is not None else 1.0),
        )

    def _load_day_seed(self) -> dict[str, float] | None:
        try:
            raw_text = DAY_SEED_PATH.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except FileNotFoundError:
            return None
        except Exception as error:
            logger.warning("picamera2.day_seed.load_failed", error=str(error), path=str(DAY_SEED_PATH))
            return None

        exposure_us = data.get("exposure_us")
        gain = data.get("gain")

        if exposure_us is None or gain is None:
            return None

        return {"exposure_us": float(exposure_us), "gain": float(gain)}

    def _save_day_seed(self, exposure_us: int, gain: float) -> None:
        payload = {"exposure_us": int(exposure_us), "gain": float(gain)}

        try:
            DAY_SEED_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = DAY_SEED_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temp_path.replace(DAY_SEED_PATH)
        except Exception as error:
            logger.warning("picamera2.day_seed.save_failed", error=str(error), path=str(DAY_SEED_PATH))
            return

        self._day_seed = {"exposure_us": float(exposure_us), "gain": float(gain)}

    def _apply_controls(
        self,
        settings: dict[str, Any],
        controller: MeanTargetController | None,
    ) -> None:
        if controller is not None:
            exposure_us = controller.exposure_us
            gain = controller.gain
        else:
            exposure_ms = settings.get("exposure_ms")
            exposure_us = int(float(exposure_ms) * 1000) if exposure_ms is not None else None
            gain = float(settings["gain"]) if settings.get("gain") is not None else None

        # libcamera's AEC/AGC is always off: either the mean-target controller owns
        # exposure and gain, or the user set them explicitly. AeEnable cannot split
        # exposure and gain independently anyway, so letting it run would silently
        # override whichever half was meant to stay manual.
        controls: dict[str, Any] = {"AeEnable": False}

        if exposure_us is not None:
            controls["ExposureTime"] = int(exposure_us)
            # Frame duration caps exposure, so it has to track the requested
            # shutter - in both directions. Leaving it untouched on the day/auto
            # path meant the night limit stayed pinned after sunrise: every
            # daytime frame still took a whole night exposure however short the
            # shutter was, and since a capture needs settle frames too, a 2ms
            # daylight frame cost four 50s frames. libcamera clamps the request up
            # to the sensor's fastest readout, so asking for less is harmless.
            frame_duration_us = int(exposure_us) + FRAME_DURATION_MARGIN_US
            controls["FrameDurationLimits"] = (frame_duration_us, frame_duration_us)

        if gain is not None:
            controls["AnalogueGain"] = float(gain)

        controls.update(self._white_balance_controls(settings))

        noise_reduction = self._noise_reduction_mode(settings.get("noise_reduction"))

        if noise_reduction is not None:
            controls["NoiseReductionMode"] = noise_reduction

        if settings.get("sharpness") is not None:
            controls["Sharpness"] = float(settings["sharpness"])

        if settings.get("saturation") is not None:
            controls["Saturation"] = float(settings["saturation"])

        if controls == self._applied_controls:
            self._last_requested_controls = dict(controls)
            return

        self._picamera2.set_controls(controls)
        self._applied_controls = controls
        self._last_requested_controls = dict(controls)
        # Takes a frame or two to land on the sensor, so the next capture has to
        # wait for it. A capture that changes nothing does not.
        self._needs_settle = True

        logger.info("picamera2.controls.applied", controls=self._loggable(controls))

    def _white_balance_controls(self, settings: dict[str, Any]) -> dict[str, Any]:
        # The auto flag wins: gains are always sent alongside it so the manual
        # values survive round-tripping through the UI, but they only take effect
        # once auto is switched off.
        if bool(settings.get("auto_white_balance", False)):
            return {"AwbEnable": True}

        colour_gains = settings.get("colour_gains")

        if colour_gains and len(colour_gains) == 2 and all(g is not None for g in colour_gains):
            return {
                "AwbEnable": False,
                "ColourGains": (float(colour_gains[0]), float(colour_gains[1])),
            }

        # Grey-world AWB has no valid assumption to work from under a night sky, so
        # it swings between green and magenta from frame to frame. Fixed gains are
        # what keep a timelapse colour-stable.
        if str(settings.get("period") or "night") == "night":
            return {
                "AwbEnable": False,
                "ColourGains": DEFAULT_NIGHT_COLOUR_GAINS,
            }

        return {"AwbEnable": True}

    def _measure_mean(self, request, settings: dict[str, Any]) -> float | None:
        if self._metering_size is None:
            return None

        try:
            import numpy

            frame = request.make_array("lores")
        except Exception as error:
            logger.warning("picamera2.metering.failed", error=str(error))
            return None

        width, height = self._metering_size
        # YUV420 packs the full-resolution luma plane first; that is exactly the
        # brightness we want, with no colour conversion needed.
        luma = frame[:height, :width]
        mask = self._metering_mask_for(numpy, width, height, settings)

        if mask is not None:
            values = luma[mask]
        else:
            values = luma

        if values.size == 0:
            return None

        return float(values.mean()) / 255.0

    def _metering_mask_for(self, numpy, width: int, height: int, settings: dict[str, Any]):
        if not bool(settings.get("meter_centre_only", True)):
            return None

        if self._metering_mask is not None:
            return self._metering_mask

        from app.camera.exposure import circular_mask_bounds

        radius_fraction = float(settings.get("meter_radius_fraction", 1 / 3))
        centre_x, centre_y, radius = circular_mask_bounds(width, height, radius_fraction)
        grid_y, grid_x = numpy.ogrid[:height, :width]
        # A fisheye frame is a lit circle on black; averaging the dead corners in
        # would drag the mean down and make the controller over-expose the sky.
        self._metering_mask = (
            (grid_x - centre_x) ** 2 + (grid_y - centre_y) ** 2
        ) <= radius ** 2

        return self._metering_mask

    def _settle_frame_budget(self, frames: int, exposure_us: Any) -> int:
        """How many frames settling may spend.

        Every settle frame costs a full exposure, so a fixed count of three is
        cheap by day and brutal at night: confirming a 50s control had landed
        cost 150s before the keeper was even started. Past the budget it is
        better to keep a frame that might still be short - its metadata records
        what it really used - than to spend minutes proving the point.
        """
        budget = max(1, int(frames))

        if not exposure_us:
            return budget

        affordable = int(SETTLE_TIME_BUDGET_US // max(1, int(exposure_us)))

        return max(1, min(budget, affordable))

    def _settle_after_controls(self, requested_controls: dict[str, Any], frames: int) -> None:
        requested_exposure = requested_controls.get("ExposureTime")
        requested_gain = requested_controls.get("AnalogueGain")
        requested_frame_duration_limits = requested_controls.get("FrameDurationLimits")
        settle_frames = self._settle_frame_budget(frames, requested_exposure)

        for settle_index in range(1, settle_frames + 1):
            request = self._picamera2.capture_request()

            try:
                metadata = dict(request.get_metadata() or {})
            finally:
                request.release()

            actual_exposure = metadata.get("ExposureTime")
            actual_gain = metadata.get("AnalogueGain")

            logger.info(
                "picamera2.settle.frame",
                settle_index=settle_index,
                requested_exposure_us=requested_exposure,
                requested_gain=requested_gain,
                requested_frame_duration_limits=requested_frame_duration_limits,
                actual_exposure_us=actual_exposure,
                actual_gain=actual_gain,
                actual_digital_gain=metadata.get("DigitalGain"),
                frame_duration=metadata.get("FrameDuration"),
                ae_enable=metadata.get("AeEnable"),
            )

            # Relative, because the sensor quantises exposure to whole lines: a
            # flat 100us tolerance is 2ppm of a 50s request, so a control that had
            # landed perfectly well could still read as a mismatch and burn the
            # rest of the settle budget.
            exposure_matches = (
                requested_exposure is None
                or actual_exposure is None
                or abs(int(actual_exposure) - int(requested_exposure))
                <= max(100, int(requested_exposure) * SETTLE_EXPOSURE_TOLERANCE)
            )
            gain_matches = (
                requested_gain is None
                or actual_gain is None
                or abs(float(actual_gain) - float(requested_gain)) <= 0.1
            )

            if exposure_matches and gain_matches:
                return

    def _noise_reduction_mode(self, value: Any):
        if value is None:
            return None

        try:
            from libcamera import controls as libcamera_controls
        except ImportError:
            return None

        modes = {
            "off": libcamera_controls.draft.NoiseReductionModeEnum.Off,
            "fast": libcamera_controls.draft.NoiseReductionModeEnum.Fast,
            "high_quality": libcamera_controls.draft.NoiseReductionModeEnum.HighQuality,
            "minimal": libcamera_controls.draft.NoiseReductionModeEnum.Minimal,
        }

        return modes.get(str(value).lower())

    @staticmethod
    def _even(value: int) -> int:
        return value if value % 2 == 0 else value + 1

    @staticmethod
    def _microseconds_to_ms(value: Any) -> float | None:
        if value is None:
            return None

        return round(float(value) / 1000, 3)

    @staticmethod
    def _loggable(controls: dict[str, Any]) -> dict[str, Any]:
        return {key: str(value) for key, value in controls.items()}

    def close(self) -> None:
        if self._started:
            self._picamera2.stop()
            self._started = False

        self._picamera2.close()
