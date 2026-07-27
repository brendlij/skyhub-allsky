"""Image primitives shared by the processors.

Pillow only, deliberately. `ImageChops.lighter` is exactly the per-pixel maximum a
startrail stack needs and is implemented in C, so the obvious reason to reach for
numpy - "the Python loop would be too slow" - does not apply. Keeping the
dependency list where it is matters more on a Raspberry Pi than anywhere else.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops
import structlog

logger = structlog.get_logger()

# Pillow refuses very large files as a decompression-bomb guard. An allsky frame
# at 4056x3040 is 12MP and well inside the default, but a future 60MP sensor
# would trip it, so the ceiling is raised deliberately rather than disabled.
Image.MAX_IMAGE_PIXELS = 200_000_000


def open_rgb(path: Path) -> Image.Image:
    """Load an image as RGB, fully decoded and detached from the file handle."""
    with Image.open(path) as handle:
        return handle.convert("RGB")


def lighten(base: Image.Image, incoming: Image.Image) -> Image.Image:
    """Per-pixel maximum - the startrail blend.

    stack = max(stack, frame), channel by channel. Stars move between frames and
    are brighter than the sky behind them, so the maximum keeps every position a
    star has occupied and discards the sky that was there the rest of the time.

    A size mismatch is resized rather than refused: a mid-night resolution change
    should cost sharpness, not the whole night's stack.
    """
    if base.size != incoming.size:
        incoming = incoming.resize(base.size, Image.LANCZOS)

    return ImageChops.lighter(base, incoming)


def scaled_to_width(image: Image.Image, target_width: int) -> Image.Image:
    """Downscale to a width, preserving aspect. Never upscales."""
    if target_width <= 0 or image.width <= target_width:
        return image

    height = max(1, round(image.height * target_width / image.width))

    return image.resize((target_width, height), Image.LANCZOS)


def even_dimensions(image: Image.Image) -> Image.Image:
    """Crop to even width and height.

    H.264 with 4:2:0 chroma cannot encode an odd dimension. Cropping a single row
    is invisible; letting ffmpeg fail at the end of a night is not.
    """
    width = image.width - (image.width % 2)
    height = image.height - (image.height % 2)

    if (width, height) == image.size:
        return image

    return image.crop((0, 0, width, height))


def extract_strip(image: Image.Image, orientation: str, position: float) -> Image.Image:
    """Pull one column or row out of a frame, as a keogram contribution.

    `position` is a fraction of the width (vertical strip) or height (horizontal),
    so 0.5 is the centre and the setting survives a resolution change. A vertical
    strip through the centre of an allsky frame is the sky from horizon to
    horizon through the zenith, which is what makes a keogram readable.
    """
    position = min(1.0, max(0.0, position))

    if orientation == "horizontal":
        row = min(image.height - 1, int(image.height * position))
        return image.crop((0, row, image.width, row + 1))

    column = min(image.width - 1, int(image.width * position))

    return image.crop((column, 0, column + 1, image.height))


def save_jpeg(image: Image.Image, path: Path, quality: int = 88) -> None:
    """Write a JPEG atomically.

    Live products are read by the web UI at the same moment the pipeline rewrites
    them. Writing in place gives the browser a truncated file; writing beside and
    renaming means a reader sees either the old frame or the new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")

    image.convert("RGB").save(temporary, format="JPEG", quality=quality, optimize=True)
    temporary.replace(path)


def save_png(image: Image.Image, path: Path) -> None:
    """Write a PNG atomically. Used for working state that must not degrade.

    The stack is reloaded and re-saved for every frame of the night, so a lossy
    format would compound its own artefacts hundreds of times over.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")

    # compress_level 1: this file is rewritten constantly and read by nobody but
    # us, so encoder time matters far more than the bytes on disk.
    image.save(temporary, format="PNG", compress_level=1)
    temporary.replace(path)


def load_if_exists(path: Path) -> Image.Image | None:
    """Reload working state after a restart, tolerating a half-written file."""
    if not path.is_file():
        return None

    try:
        return open_rgb(path)

    except (OSError, ValueError) as error:
        logger.warning("processing.state_unreadable", path=str(path), error=str(error))
        return None
