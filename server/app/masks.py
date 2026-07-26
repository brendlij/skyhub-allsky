"""Per-node lens masks.

A mask is a PNG the size of the frame marking what to black out - a roof, a
street lamp, the dead corners outside a fisheye circle.

Two ways to draw one, because both are what image editors hand you:

  * with transparency - transparent is kept, opaque is painted over the capture
  * flat black and white - white is kept, black is painted over

Which one applies is decided per file: a PNG carrying any transparency is read
by its alpha, a fully opaque one is read by its brightness. Either way it is
normalised to an alpha mask on upload, so everything downstream sees one format.

The mask is applied to the frame *before* the original is filed away, so both
the rendered capture and the raw original carry it. That is deliberate - and
destructive: what the mask covers is gone from every copy on the server.
"""
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops

from app.config import get_settings
from app.overlays import RENDER_JPEG_QUALITY, RENDER_JPEG_SUBSAMPLING

settings = get_settings()

# Big enough for a 100MP sensor's mask, small enough that a mis-picked file
# cannot fill the disk.
MAX_MASK_BYTES = 32 * 1024 * 1024


def mask_path(node_id: str) -> Path:
    return settings.masks_dir / f"{node_id}.png"


def mask_info(node_id: str) -> dict:
    path = mask_path(node_id)

    if not path.exists():
        return {"node_id": node_id, "exists": False}

    stat = path.stat()

    try:
        with Image.open(path) as mask:
            width, height = mask.size

    except OSError:
        width = height = None

    return {
        "node_id": node_id,
        "exists": True,
        "width": width,
        "height": height,
        "size_bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def normalize_mask(image: Image.Image) -> tuple[Image.Image, str]:
    """An uploaded mask as RGBA where alpha is "cover this", plus how it was read."""
    rgba = image.convert("RGBA")

    if rgba.getchannel("A").getextrema()[0] < 255:
        return rgba, "transparency"

    # Nothing transparent anywhere, so the file cannot mean "alpha is the mask" -
    # it is a black-and-white drawing. White is what you keep, black is what goes,
    # and grey in between lands as partial cover rather than a hard edge.
    alpha = ImageChops.invert(rgba.convert("L"))
    covered = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    covered.putalpha(alpha)

    return covered, "black-and-white"


def save_mask(node_id: str, data: bytes) -> dict:
    """Validate and store an uploaded mask. Raises ValueError on a bad file."""
    if not data:
        raise ValueError("The mask file is empty")

    if len(data) > MAX_MASK_BYTES:
        raise ValueError("The mask is larger than 32 MB")

    try:
        with Image.open(BytesIO(data)) as mask:
            mask.verify()

    except Exception as error:  # Pillow raises a grab bag of types here.
        raise ValueError("That file is not a readable image") from error

    with Image.open(BytesIO(data)) as mask:
        if (mask.format or "").upper() != "PNG":
            raise ValueError("The mask has to be a PNG")

        rgba, mode = normalize_mask(mask)
        lowest, highest = rgba.getchannel("A").getextrema()

        # Both of these are a drawing that says nothing, and silently accepting
        # one leaves the user staring at an unchanged frame wondering why.
        if lowest == 255:
            raise ValueError(
                "Every pixel in this mask covers the frame. Leave the areas you "
                "want to keep transparent, or paint them white."
            )

        if highest == 0:
            raise ValueError(
                "This mask covers nothing. Paint the areas you want blacked out "
                "in black, or make them opaque."
            )

        settings.masks_dir.mkdir(parents=True, exist_ok=True)
        rgba.save(mask_path(node_id), format="PNG", optimize=True)

    return {**mask_info(node_id), "mode": mode}


def delete_mask(node_id: str) -> bool:
    path = mask_path(node_id)

    if not path.exists():
        return False

    path.unlink()
    return True


def load_mask(node_id: str, size: tuple[int, int]) -> Image.Image | None:
    """The node's mask as RGBA at `size`, or None when there is no usable mask."""
    path = mask_path(node_id)

    if not path.exists():
        return None

    try:
        with Image.open(path) as mask:
            rgba = mask.convert("RGBA")

    except OSError:
        return None

    if rgba.size != size:
        # A mask drawn against a full-resolution frame still has to work when the
        # camera is pinned to a smaller readout, and vice versa.
        rgba = rgba.resize(size, Image.LANCZOS)

    return rgba


def apply_mask_to_file(node_id: str, image_path: Path) -> bool:
    """Burn the node's mask into an image file. Returns True when it was rewritten."""
    if not mask_path(node_id).exists():
        return False

    with Image.open(image_path) as image:
        source_format = (image.format or "").upper()
        exif = image.info.get("exif")
        icc_profile = image.info.get("icc_profile")
        mask = load_mask(node_id, image.size)

        if mask is None:
            return False

        masked = Image.alpha_composite(image.convert("RGBA"), mask)
        save_options = {}

        if exif:
            save_options["exif"] = exif

        if icc_profile:
            save_options["icc_profile"] = icc_profile

        if source_format == "PNG":
            masked.save(image_path, format="PNG", **save_options)

        else:
            # JPEG cannot hold the alpha channel, and does not need to: the mask is
            # already composited down to flat pixels.
            masked.convert("RGB").save(
                image_path,
                format=source_format or "JPEG",
                quality=RENDER_JPEG_QUALITY,
                subsampling=RENDER_JPEG_SUBSAMPLING,
                optimize=True,
                **save_options,
            )

    return True
