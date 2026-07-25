from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, ImageDraw

from app.camera.base import CameraInfo, CaptureResult


class MockCamera:
    def get_info(self) -> CameraInfo:
        return CameraInfo(
            camera_id="mock",
            name="Mock Camera",
            driver="mock",
            supports_exposure=True,
            supports_gain=True,
            supported_formats=["jpg"],
        )

    async def capture(self, settings: dict[str, Any], output_dir: Path) -> CaptureResult:
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        filename = f"mock_{timestamp}_{uuid4().hex[:8]}.jpg"
        output_path = output_dir / filename

        # A null size means "use the sensor's full resolution"; the mock has no
        # sensor, so it stands in with a 4:3 frame the same shape a Pi camera gives.
        width = int(settings.get("width") or 2028)
        height = int(settings.get("height") or 1520)
        exposure_ms = settings.get("exposure_ms")
        gain = settings.get("gain")
        auto_exposure = bool(settings.get("auto_exposure", False))
        auto_gain = bool(settings.get("auto_gain", False))
        image_format = str(settings.get("format") or "jpg").lower()

        if image_format not in ["jpg", "jpeg"]:
            raise ValueError(f"MockCamera only supports jpg for now, got: {image_format}")

        image = Image.new("RGB", (width, height), color=(12, 16, 24))
        draw = ImageDraw.Draw(image)

        lines = [
            "SkyHub Mock Camera",
            f"Captured UTC: {now.isoformat()}",
            f"Exposure: {'auto' if auto_exposure else f'{exposure_ms} ms'}",
            f"Gain: {'auto' if auto_gain else gain}",
            f"Format: {image_format}",
            f"Size: {width}x{height}",
        ]

        y = 40
        for line in lines:
            draw.text((40, y), line, fill=(230, 230, 230))
            y += 32

        for i in range(180):
            x = (i * 73) % width
            y = (i * 137) % height
            r = 1 if i % 5 else 2
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(240, 240, 220))

        image.save(output_path, quality=92)

        return CaptureResult(
            file_path=output_path,
            format="jpg",
            width=width,
            height=height,
            metadata={
                "captured_at": now.isoformat(),
                "camera": "mock",
                "auto_exposure": auto_exposure,
                "exposure_ms": exposure_ms,
                "auto_gain": auto_gain,
                "gain": gain,
            },
        )
