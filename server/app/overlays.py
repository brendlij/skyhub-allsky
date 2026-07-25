from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont


VARIABLE_PATTERN = re.compile(r"\$[A-Za-z][A-Za-z0-9_.]*")


@dataclass(frozen=True)
class VariableSpec:
    token: str
    label: str
    group: str
    sample: str
    # Unit is appended when the editor inserts the variable, so nobody has to
    # remember that illumination is a percentage and sensor temperature is Celsius.
    unit: str = ""
    suggested_label: str = ""

    def snippet(self) -> str:
        """Ready-to-use template fragment with label and unit already attached."""
        parts = []

        if self.suggested_label:
            parts.append(f"{self.suggested_label}:")

        parts.append(self.token)

        if self.unit:
            parts.append(self.unit)

        return " ".join(parts)


# Single source of truth for the overlay variables. The editor fetches this rather
# than keeping its own copy, so a variable added here shows up in the UI with a
# preview value and cannot silently drift out of sync with what actually renders.
VARIABLE_CATALOG: tuple[VariableSpec, ...] = (
    VariableSpec("$capture.datetime", "Date time", "Capture", "2026-06-21 23:42:10"),
    VariableSpec("$capture.date", "Date", "Capture", "2026-06-21"),
    VariableSpec("$capture.time", "Time", "Capture", "23:42:10"),
    VariableSpec("$capture.period", "Period", "Capture", "NIGHT"),
    VariableSpec("$capture.timezone", "Timezone", "Capture", "Europe/Berlin"),
    VariableSpec("$capture.filename", "Filename", "Capture", "cap_1a2b_picamera2.jpg"),
    VariableSpec("$capture.sequence", "Sequence", "Capture", "seq_4f21"),
    VariableSpec("$node.id", "Node", "Capture", "pi5-hqcam"),
    VariableSpec("$picamera2.state", "Camera state", "Capture", "capturing"),

    VariableSpec("$exposure.time", "Exposure", "Exposure", "10s", suggested_label="Exp"),
    VariableSpec("$exposure.shutter", "Shutter", "Exposure", "10.0s", suggested_label="Shutter"),
    VariableSpec("$exposure.ms", "Exposure ms", "Exposure", "10000", unit="ms", suggested_label="Exp"),
    VariableSpec("$exposure.gain", "Gain", "Exposure", "8", suggested_label="Gain"),
    VariableSpec("$exposure.analogue_gain", "Analogue gain", "Exposure", "8", suggested_label="Gain"),
    VariableSpec("$exposure.digital_gain", "Digital gain", "Exposure", "1.02", suggested_label="Dgain"),
    VariableSpec("$exposure.total_gain", "Total gain", "Exposure", "8.16", suggested_label="Gain"),
    VariableSpec("$exposure.iso", "ISO", "Exposure", "800", suggested_label="ISO"),
    VariableSpec("$exposure.mean", "Mean", "Exposure", "0.196", suggested_label="Mean"),
    VariableSpec("$exposure.target_mean", "Target mean", "Exposure", "0.2", suggested_label="Target"),
    VariableSpec("$exposure.auto", "Auto exposure", "Exposure", "off", suggested_label="AE"),
    VariableSpec("$exposure.auto_gain", "Auto gain", "Exposure", "off", suggested_label="AG"),

    VariableSpec("$sensor.temperature", "Sensor temp", "Sensor", "31.5", unit="°C", suggested_label="Sensor"),
    VariableSpec("$sensor.lux", "Lux", "Sensor", "0.4", unit="lux"),
    VariableSpec("$sensor.size", "Sensor size", "Sensor", "4056x3040"),
    VariableSpec("$sensor.mode", "Sensor mode", "Sensor", "2028x1520"),
    VariableSpec("$sensor.bit_depth", "Bit depth", "Sensor", "12", unit="bit"),
    VariableSpec("$sensor.colour_gains", "Colour gains", "Sensor", "2.60 / 2.10", suggested_label="WB"),
    VariableSpec("$sensor.wb_red", "WB red", "Sensor", "2.6", suggested_label="R"),
    VariableSpec("$sensor.wb_blue", "WB blue", "Sensor", "2.1", suggested_label="B"),

    VariableSpec("$image.width", "Width", "Image", "2028", unit="px"),
    VariableSpec("$image.height", "Height", "Image", "1520", unit="px"),
    VariableSpec("$image.size", "Dimensions", "Image", "2028x1520"),
    VariableSpec("$image.megapixels", "Megapixels", "Image", "3.1", unit="MP"),
    VariableSpec("$image.filesize", "File size", "Image", "2.4 MB"),
    VariableSpec("$image.format", "Format", "Image", "jpg"),

    VariableSpec("$settings.interval", "Interval", "Settings", "60", unit="s", suggested_label="Every"),
    VariableSpec("$settings.saturation", "Saturation", "Settings", "1.15", suggested_label="Sat"),
    VariableSpec("$settings.hue", "Hue", "Settings", "-12", unit="°", suggested_label="Hue"),

    VariableSpec("$bme280.temperature", "Temp", "Environment", "12.4", unit="°C", suggested_label="Temp"),
    VariableSpec("$bme280.humidity", "Humidity", "Environment", "78", unit="%", suggested_label="Humidity"),
    VariableSpec("$bme280.pressure", "Pressure", "Environment", "1008", unit="hPa"),
    VariableSpec("$bme280.dew_point", "Dew point", "Environment", "8.7", unit="°C", suggested_label="Dew"),

    VariableSpec("$heater.state", "Heater", "Heater", "off", suggested_label="Heater"),
    VariableSpec("$heater.desired", "Heater desired", "Heater", "off", suggested_label="Heater set"),
    VariableSpec("$heater.gpio", "Heater GPIO", "Heater", "23", suggested_label="GPIO"),
    VariableSpec("$heater.driver", "Heater driver", "Heater", "gpiozero"),

    VariableSpec("$sun.elevation", "Sun elevation", "Sun & Moon", "-18.4", unit="°", suggested_label="Sun"),
    VariableSpec("$sun.azimuth", "Sun azimuth", "Sun & Moon", "12.7", unit="°", suggested_label="Sun az"),
    VariableSpec("$sun.sunrise", "Sunrise", "Sun & Moon", "05:12", suggested_label="Sunrise"),
    VariableSpec("$sun.sunset", "Sunset", "Sun & Moon", "21:38", suggested_label="Sunset"),
    VariableSpec("$moon.phase", "Moon phase", "Sun & Moon", "14.2"),
    VariableSpec("$moon.phase_name", "Moon phase name", "Sun & Moon", "Full", suggested_label="Moon"),
    VariableSpec("$moon.illumination", "Moon illumination", "Sun & Moon", "99", unit="%", suggested_label="Moon"),
    VariableSpec("$moon.elevation", "Moon elevation", "Sun & Moon", "34.1", unit="°", suggested_label="Moon"),
    VariableSpec("$moon.azimuth", "Moon azimuth", "Sun & Moon", "168.3", unit="°", suggested_label="Moon az"),
)


# One-click starting layouts. Templates use the same tokens the editor inserts, so
# a preset is just a normal overlay the user can pick apart afterwards.
OVERLAY_PRESETS: tuple[dict, ...] = (
    {
        "id": "allsky_standard",
        "name": "Standard allsky",
        "description": "Timestamp, exposure, conditions and moon in the four corners.",
        "entities": [
            {"anchor": "top-left", "x": 0.02, "y": 0.02, "font_size": 34,
             "text": "$capture.datetime"},
            {"anchor": "top-right", "x": 0.98, "y": 0.02, "font_size": 30,
             "text": "Exp $exposure.time  ISO$exposure.iso"},
            {"anchor": "bottom-left", "x": 0.02, "y": 0.98, "font_size": 28,
             "text": "Temp $bme280.temperature °C  Humidity $bme280.humidity %  Dew $bme280.dew_point °C"},
            {"anchor": "bottom-right", "x": 0.98, "y": 0.98, "font_size": 28,
             "text": "Moon $moon.phase_name $moon.illumination %  Sun $sun.elevation °"},
        ],
    },
    {
        "id": "minimal",
        "name": "Minimal",
        "description": "Just the timestamp and node name.",
        "entities": [
            {"anchor": "bottom-left", "x": 0.02, "y": 0.98, "font_size": 30,
             "text": "$capture.datetime  $node.id"},
        ],
    },
    {
        "id": "diagnostic",
        "name": "Diagnostic",
        "description": "Everything useful for tuning exposure and colour.",
        "entities": [
            {"anchor": "top-left", "x": 0.02, "y": 0.02, "font_size": 28,
             "text": "$capture.datetime  $capture.period  $node.id"},
            {"anchor": "top-right", "x": 0.98, "y": 0.02, "font_size": 26,
             "text": "Exp $exposure.time  Gain $exposure.gain  ISO$exposure.iso"},
            {"anchor": "bottom-right", "x": 0.98, "y": 0.98, "font_size": 26,
             "text": "Mean $exposure.mean / Target $exposure.target_mean  WB $sensor.colour_gains"},
            {"anchor": "bottom-left", "x": 0.02, "y": 0.98, "font_size": 26,
             "text": "Sensor $sensor.temperature °C  $sensor.lux lux  $image.size  $image.filesize"},
        ],
    },
)


def overlay_presets() -> list[dict]:
    return [
        {
            "id": preset["id"],
            "name": preset["name"],
            "description": preset["description"],
            "entities": [dict(entity) for entity in preset["entities"]],
        }
        for preset in OVERLAY_PRESETS
    ]


def known_tokens() -> set[str]:
    return {spec.token for spec in VARIABLE_CATALOG}


def unknown_tokens(template: str) -> list[str]:
    """Tokens that will silently render as empty text."""
    known = known_tokens()
    # Legacy aliases still resolve even though they are not offered in the picker.
    known |= {"$node.node_id", "$bme280.temperature_c", "$bme280.humidity_percent",
              "$bme280.pressure_hpa", "$bme280.dew_point_c", "$heater.actual"}

    return sorted({
        match.group(0)
        for match in VARIABLE_PATTERN.finditer(template or "")
        if match.group(0) not in known
    })


def variable_catalog(live_values: dict[str, object] | None = None) -> list[dict]:
    """Catalog for the editor.

    `value` is what this variable resolves to right now for the selected node, and
    falls back to the illustrative sample when there is nothing real to show yet.
    """
    live_values = live_values or {}
    entries = []

    for spec in VARIABLE_CATALOG:
        resolved = format_value(live_values.get(spec.token)) if spec.token in live_values else ""

        entries.append({
            "token": spec.token,
            "label": spec.label,
            "group": spec.group,
            "sample": spec.sample,
            "unit": spec.unit,
            "suggested_label": spec.suggested_label,
            "snippet": spec.snippet(),
            "value": resolved or spec.sample,
            "live": bool(resolved),
        })

    return entries

# Pillow defaults to quality 75 with 4:2:0 chroma subsampling, which visibly softens
# stars and colour edges every time an overlay is burned in.
RENDER_JPEG_QUALITY = 95
RENDER_JPEG_SUBSAMPLING = 0


def hex_to_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if not isinstance(value, str):
        return fallback

    value = value.strip().lstrip("#")

    if len(value) != 6:
        return fallback

    try:
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return fallback


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(font_name, size=max(8, int(size)))
        except OSError:
            continue

    return ImageFont.load_default()


# The editor preview computes its label boxes with exactly these numbers. Keep the
# two in step: a label lands where it was dragged only if both sides agree on how
# big its box is. Deriving the height from the font size rather than from the
# glyphs also keeps two labels of the same size the same height, where measuring
# the drawn text made "DAY" shorter than "38.9 C" purely because it has no
# descenders.
OVERLAY_LINE_HEIGHT = 1.2
OVERLAY_PADDING_RATIO = 0.22
OVERLAY_MIN_PADDING = 5


def overlay_font_size(entity: dict) -> int:
    return max(8, int(entity.get("font_size") or 28))


def overlay_padding(font_size: int) -> int:
    return max(OVERLAY_MIN_PADDING, int(font_size * OVERLAY_PADDING_RATIO))


def overlay_line_height(font_size: int) -> int:
    return int(round(font_size * OVERLAY_LINE_HEIGHT))


def text_box_size(draw: ImageDraw.ImageDraw, text: str, font, font_size: int) -> tuple[int, int]:
    """Outer box of a label, padding included."""
    lines = text.split("\n")
    padding = overlay_padding(font_size)
    width = max(int(round(draw.textlength(line, font=font))) for line in lines)

    return width + padding * 2, overlay_line_height(font_size) * len(lines) + padding * 2


def draw_label_text(draw: ImageDraw.ImageDraw, position: tuple[int, int], text: str, font, font_size: int, fill) -> None:
    """Draw each line centred in its own fixed-height line box.

    Anchoring on the baseline rather than on the glyph bounding box is what makes
    the vertical position independent of which characters happen to be in the text.
    """
    left, top = position
    line_height = overlay_line_height(font_size)

    try:
        ascent, descent = font.getmetrics()
    except AttributeError:
        # Bitmap fallback font: no metrics, no baseline anchors.
        draw.text((left, top), text, font=font, fill=fill)
        return

    baseline = top + (line_height - (ascent + descent)) / 2 + ascent

    for index, line in enumerate(text.split("\n")):
        draw.text((left, baseline + index * line_height), line, font=font, fill=fill, anchor="ls")


def format_value(value) -> str:
    if value is None:
        return ""

    if isinstance(value, bool):
        return "on" if value else "off"

    if isinstance(value, float):
        return f"{value:.1f}".rstrip("0").rstrip(".")

    return str(value)


def trim_number(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def format_exposure(exposure_ms) -> str | None:
    """Human-readable exposure: 10s, 250ms, 900us."""
    if exposure_ms is None:
        return None

    exposure_ms = float(exposure_ms)

    if exposure_ms >= 1000:
        return f"{trim_number(exposure_ms / 1000)}s"

    if exposure_ms >= 1:
        return f"{trim_number(exposure_ms, 1)}ms"

    return f"{round(exposure_ms * 1000)}us"


def format_shutter(exposure_ms) -> str | None:
    """Photographic notation: long night exposures in seconds, day as 1/n."""
    if not exposure_ms:
        return None

    seconds = float(exposure_ms) / 1000

    if seconds >= 1:
        return f"{trim_number(seconds, 1)}s"

    return f"1/{round(1 / seconds)}"


def format_filesize(size_bytes) -> str | None:
    if not size_bytes:
        return None

    size = float(size_bytes)

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{trim_number(size, 1)} {unit}"

        size /= 1024

    return None


def moon_phase_name(phase: float) -> str:
    # astral returns 0-27.99: 0 new, 7 first quarter, 14 full, 21 last quarter.
    boundaries = (
        (1.84, "New"),
        (5.53, "Waxing crescent"),
        (9.22, "First quarter"),
        (12.91, "Waxing gibbous"),
        (16.61, "Full"),
        (20.30, "Waning gibbous"),
        (23.99, "Last quarter"),
        (27.68, "Waning crescent"),
    )

    for limit, name in boundaries:
        if phase < limit:
            return name

    return "New"


def moon_illumination(phase: float) -> float:
    return (1 - math.cos(2 * math.pi * phase / 28)) / 2 * 100


def sun_moon_values(observer, local_captured_at: datetime) -> dict[str, object]:
    """Sun and moon position for the capture instant.

    Wrapped defensively: astral raises for a sun that never rises or sets at high
    latitudes, and a single bad overlay variable must not fail the whole upload.
    """
    if observer is None:
        return {}

    from astral import moon as astral_moon
    from astral import sun as astral_sun

    values: dict[str, object] = {}

    try:
        values["$sun.elevation"] = round(astral_sun.elevation(observer, local_captured_at), 1)
        values["$sun.azimuth"] = round(astral_sun.azimuth(observer, local_captured_at), 1)
    except Exception:
        pass

    try:
        times = astral_sun.sun(observer, date=local_captured_at.date(), tzinfo=local_captured_at.tzinfo)
        values["$sun.sunrise"] = times["sunrise"].strftime("%H:%M")
        values["$sun.sunset"] = times["sunset"].strftime("%H:%M")
    except Exception:
        pass

    try:
        phase = astral_moon.phase(local_captured_at.date())
        values["$moon.phase"] = round(phase, 1)
        values["$moon.phase_name"] = moon_phase_name(phase)
        values["$moon.illumination"] = round(moon_illumination(phase))
    except Exception:
        pass

    try:
        values["$moon.elevation"] = round(astral_moon.elevation(observer, local_captured_at), 1)
        values["$moon.azimuth"] = round(astral_moon.azimuth(observer, local_captured_at), 1)
    except Exception:
        pass

    return values


def exposure_values(metadata: dict, period: str, camera_settings) -> dict[str, object]:
    """Prefer what the sensor reported over what was requested.

    The node sends both: the requested exposure/gain and the values the frame was
    actually taken with. Those diverge whenever libcamera clamps a request or the
    mean-target controller is still converging, and the overlay should describe the
    frame in front of you.
    """
    exposure_ms = metadata.get("actual_exposure_ms")

    if exposure_ms is None:
        exposure_ms = metadata.get("exposure_ms")

    analogue_gain = metadata.get("actual_analogue_gain")

    if analogue_gain is None:
        analogue_gain = metadata.get("gain")

    digital_gain = metadata.get("actual_digital_gain")
    total_gain = None

    if analogue_gain is not None:
        total_gain = float(analogue_gain) * float(digital_gain or 1.0)

    prefix = "day" if period == "day" else "night"

    return {
        "$exposure.time": format_exposure(exposure_ms),
        "$exposure.shutter": format_shutter(exposure_ms),
        "$exposure.ms": trim_number(float(exposure_ms), 1) if exposure_ms is not None else None,
        "$exposure.gain": trim_number(float(analogue_gain)) if analogue_gain is not None else None,
        "$exposure.analogue_gain": trim_number(float(analogue_gain)) if analogue_gain is not None else None,
        "$exposure.digital_gain": trim_number(float(digital_gain)) if digital_gain is not None else None,
        "$exposure.total_gain": trim_number(total_gain) if total_gain is not None else None,
        # Gain x100 is the conventional ISO equivalence for these sensors.
        "$exposure.iso": round(float(analogue_gain) * 100) if analogue_gain is not None else None,
        "$exposure.mean": trim_number(float(metadata["mean"]), 3) if metadata.get("mean") is not None else None,
        "$exposure.target_mean": trim_number(float(metadata["target_mean"]), 3)
        if metadata.get("target_mean") is not None
        else None,
        "$exposure.auto": bool(getattr(camera_settings, f"{prefix}_auto_exposure", False)),
        "$exposure.auto_gain": bool(getattr(camera_settings, f"{prefix}_auto_gain", False)),
    }


def sensor_values(metadata: dict) -> dict[str, object]:
    colour_gains = metadata.get("colour_gains")
    sensor_size = metadata.get("sensor_size")
    sensor_mode = metadata.get("sensor_mode")

    values: dict[str, object] = {
        "$sensor.temperature": metadata.get("sensor_temperature_c"),
        "$sensor.lux": trim_number(float(metadata["lux"]), 2) if metadata.get("lux") is not None else None,
        "$sensor.bit_depth": metadata.get("bit_depth"),
    }

    if sensor_size and len(sensor_size) == 2:
        values["$sensor.size"] = f"{sensor_size[0]}x{sensor_size[1]}"

    if sensor_mode and len(sensor_mode) == 2:
        values["$sensor.mode"] = f"{sensor_mode[0]}x{sensor_mode[1]}"

    if colour_gains and len(colour_gains) == 2:
        values["$sensor.wb_red"] = trim_number(float(colour_gains[0]))
        values["$sensor.wb_blue"] = trim_number(float(colour_gains[1]))
        values["$sensor.colour_gains"] = (
            f"{float(colour_gains[0]):.2f} / {float(colour_gains[1]):.2f}"
        )

    return values


def variable_values(context: dict) -> dict[str, object]:
    captured_at = context["captured_at"]
    environment = context.get("environment")
    heater = context.get("heater")
    camera_settings = context.get("camera_settings")
    metadata = context.get("metadata") or {}
    period = str(context.get("period") or "night")
    prefix = "day" if period == "day" else "night"
    width = context.get("width")
    height = context.get("height")

    values = {
        "$capture.datetime": captured_at.strftime("%Y-%m-%d %H:%M:%S"),
        "$capture.date": captured_at.strftime("%Y-%m-%d"),
        "$capture.time": captured_at.strftime("%H:%M:%S"),
        "$capture.period": period.upper(),
        "$capture.timezone": context.get("timezone_name", ""),
        "$capture.filename": context.get("filename", ""),
        "$capture.sequence": context.get("sequence_id", ""),
        "$node.id": context.get("node_id", ""),
        "$node.node_id": context.get("node_id", ""),
        "$picamera2.state": "capturing" if getattr(camera_settings, "capture_enabled", False) else "idle",
        "$image.width": width,
        "$image.height": height,
        "$image.size": f"{width}x{height}" if width and height else None,
        "$image.megapixels": trim_number(width * height / 1_000_000, 1) if width and height else None,
        "$image.filesize": format_filesize(context.get("size_bytes")),
        "$image.format": context.get("format", ""),
        "$settings.interval": getattr(camera_settings, "interval_seconds", None),
        # Pre-formatted: the generic float formatter rounds to 1 decimal, which
        # would turn a saturation of 1.15 into 1.1.
        "$settings.saturation": trim_number(float(saturation), 2)
        if (saturation := getattr(camera_settings, f"{prefix}_saturation", None)) is not None
        else None,
        "$settings.hue": trim_number(float(hue), 1)
        if (hue := getattr(camera_settings, f"{prefix}_hue", None)) is not None
        else None,
    }

    values.update(exposure_values(metadata, period, camera_settings))
    values.update(sensor_values(metadata))
    values.update(sun_moon_values(context.get("observer"), captured_at))

    if environment is not None:
        values.update(
            {
                "$bme280.temperature": environment.temperature_c,
                "$bme280.temperature_c": environment.temperature_c,
                "$bme280.humidity": environment.humidity_percent,
                "$bme280.humidity_percent": environment.humidity_percent,
                "$bme280.pressure": environment.pressure_hpa,
                "$bme280.pressure_hpa": environment.pressure_hpa,
                "$bme280.dew_point": environment.dew_point_c,
                "$bme280.dew_point_c": environment.dew_point_c,
            }
        )

    if heater is not None:
        values.update(
            {
                "$heater.desired": heater.desired_enabled,
                "$heater.actual": heater.actual_enabled,
                "$heater.state": heater.actual_enabled,
                "$heater.gpio": heater.gpio_pin,
                "$heater.driver": heater.driver,
            }
        )

    return values


def render_template(template: str, context: dict) -> str:
    # Resolved once per image, not once per entity: the sun and moon positions cost
    # real work and every entity on the frame shares the same capture instant.
    values = context.get("_resolved_values")

    if values is None:
        values = variable_values(context)
        context["_resolved_values"] = values

    def replace(match: re.Match) -> str:
        return format_value(values.get(match.group(0)))

    return VARIABLE_PATTERN.sub(replace, template)


def entity_text(entity: dict, context: dict) -> str:
    if entity.get("text"):
        return render_template(str(entity.get("text") or ""), context)

    entity_type = entity.get("type")

    if entity_type == "datetime":
        return render_template("$capture.datetime", context)

    if entity_type == "date":
        return render_template("$capture.date", context)

    if entity_type == "time":
        return render_template("$capture.time", context)

    if entity_type == "period":
        return render_template("$capture.period", context)

    if entity_type == "node_id":
        return render_template("$node.id", context)

    if entity_type == "text":
        return str(entity.get("label") or "")

    return ""


def anchored_position(
    anchor: str,
    x: float,
    y: float,
    text_width: int,
    text_height: int,
    image_width: int,
    image_height: int,
) -> tuple[int, int]:
    left = int(x * image_width)
    top = int(y * image_height)

    if "right" in anchor:
        left -= text_width
    elif "center" in anchor:
        left -= text_width // 2

    if "bottom" in anchor:
        top -= text_height
    elif anchor == "center":
        top -= text_height // 2

    return left, top


def apply_hue_shift(image: Image.Image, degrees: float) -> Image.Image:
    """Rotate hue around the colour wheel.

    libcamera has no hue control, so unlike white balance this cannot be done on
    the sensor - it is a post-process on the developed image. Prefer the white
    balance tint for fixing a green or magenta cast: that corrects the channel
    gains before highlights clip, whereas rotating hue here shifts stars and
    airglow along with the cast and cannot recover a channel that already blew out.
    """
    shift = int(round((degrees % 360) / 360 * 256)) % 256

    if shift == 0:
        return image

    hue, saturation, value = image.convert("HSV").split()
    hue = hue.point(lambda level: (level + shift) % 256)

    return Image.merge("HSV", (hue, saturation, value)).convert("RGB")


def render_capture_image(
    image_path: Path,
    overlay_settings,
    *,
    node_id: str,
    captured_at: datetime,
    period: str,
    timezone_name: str,
    environment=None,
    heater=None,
    camera_settings=None,
    hue_shift: float = 0.0,
    metadata: dict | None = None,
    observer=None,
    sequence_id: str | None = None,
    size_bytes: int | None = None,
    image_format: str | None = None,
) -> bool:
    """Apply hue correction and overlays in one decode/encode pass.

    Returns True when the file was rewritten. Doing both in a single pass matters:
    each JPEG round trip is lossy, so hue and overlays as separate steps would
    cost two re-encodes instead of one.
    """
    entities = []

    if overlay_settings and overlay_settings.enabled:
        entities = overlay_settings.entities or []

    hue_shift = float(hue_shift or 0.0) % 360

    if not entities and hue_shift == 0:
        return False

    local_captured_at = captured_at.astimezone(ZoneInfo(timezone_name))

    with Image.open(image_path) as image:
        source_format = (image.format or "").upper()
        exif = image.info.get("exif")
        icc_profile = image.info.get("icc_profile")

        if hue_shift:
            # Before the overlay, so burned-in text keeps the colours it was given.
            image = apply_hue_shift(image.convert("RGB"), hue_shift)

        image = image.convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        context = {
            "node_id": node_id,
            "captured_at": local_captured_at,
            "period": period,
            "timezone_name": timezone_name,
            "environment": environment,
            "heater": heater,
            "camera_settings": camera_settings,
            "metadata": metadata or {},
            "observer": observer,
            "sequence_id": sequence_id,
            "filename": image_path.name,
            "size_bytes": size_bytes,
            "format": image_format,
            "width": image.width,
            "height": image.height,
        }

        for entity in entities:
            if not entity.get("enabled", True):
                continue

            text = entity_text(entity, context)

            if not text:
                continue

            font_size = overlay_font_size(entity)
            font = load_font(font_size)
            padding = overlay_padding(font_size)
            box_width, box_height = text_box_size(draw, text, font, font_size)
            left, top = anchored_position(
                entity.get("anchor", "top-left"),
                min(1, max(0, float(entity.get("x", 0)))),
                min(1, max(0, float(entity.get("y", 0)))),
                box_width,
                box_height,
                image.width,
                image.height,
            )

            left = max(0, min(image.width - box_width, left))
            top = max(0, min(image.height - box_height, top))
            opacity = min(1, max(0, float(entity.get("background_opacity", 0.35))))
            background = (*hex_to_rgb(entity.get("background", "#000000"), (0, 0, 0)), int(255 * opacity))
            color = (*hex_to_rgb(entity.get("color", "#ffffff"), (255, 255, 255)), 255)

            if opacity > 0:
                draw.rounded_rectangle(
                    (left, top, left + box_width, top + box_height),
                    radius=max(4, padding),
                    fill=background,
                )

            draw_label_text(draw, (left + padding, top + padding), text, font, font_size, color)

        image = Image.alpha_composite(image, overlay).convert("RGB")
        save_options = {}

        if exif:
            save_options["exif"] = exif

        if icc_profile:
            save_options["icc_profile"] = icc_profile

        if source_format in {"JPEG", "JPEG2000", ""}:
            save_options.update(
                format="JPEG",
                quality=RENDER_JPEG_QUALITY,
                subsampling=RENDER_JPEG_SUBSAMPLING,
                optimize=True,
            )
        else:
            save_options["format"] = source_format

        image.save(image_path, **save_options)

    return True
