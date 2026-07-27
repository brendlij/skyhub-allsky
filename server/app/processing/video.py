"""ffmpeg, wrapped so its absence is a reported condition rather than a crash.

ffmpeg is not a Python dependency and cannot be pip-installed, so a SkyHub that
has never had `apt install ffmpeg` run on it is a completely normal state. Every
video product therefore degrades to "unavailable, here is why" instead of failing
once per night in a log nobody reads.

Frames are fed through the concat demuxer from a manifest the session has been
appending to all night. That is what keeps encoding off the archive: ffmpeg reads
a list the pipeline already had, not a directory it has to scan and sort.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Callable

import structlog

from app.config import get_settings

logger = structlog.get_logger()

# Encoding a night is minutes of work on a Pi; this only catches a hang.
ENCODE_TIMEOUT_SECONDS = 3600

CODECS = {
    # x264 is the only one that plays everywhere, and is the default for that
    # reason alone. `yuv420p` is what makes it play in browsers and on phones.
    "h264": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium"],
    # Smaller at the same quality, appreciably slower to encode - reasonable
    # overnight on a Pi 5, painful on anything older.
    "h265": ["-c:v", "libx265", "-pix_fmt", "yuv420p", "-preset", "medium", "-tag:v", "hvc1"],
    # No patent questions, good compression, slowest of the three.
    "vp9": ["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-b:v", "0"],
}

CONTAINER_FOR_CODEC = {"h264": "mp4", "h265": "mp4", "vp9": "webm"}


@dataclass
class EncodeResult:
    ok: bool
    path: Path | None = None
    duration_seconds: float | None = None
    frame_count: int = 0
    error: str | None = None


def ffmpeg_binary() -> str | None:
    """Locate ffmpeg: the configured path first, then PATH."""
    configured = get_settings().ffmpeg_path

    if configured:
        return configured if Path(configured).is_file() else None

    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return ffmpeg_binary() is not None


def ffmpeg_version() -> str | None:
    binary = ffmpeg_binary()

    if binary is None:
        return None

    try:
        completed = subprocess.run(
            [binary, "-version"], capture_output=True, text=True, timeout=15, check=False
        )
        first_line = (completed.stdout or "").splitlines()[:1]

        return first_line[0] if first_line else None

    except (OSError, subprocess.SubprocessError):
        return None


def write_concat_manifest(frames: list[Path], manifest_path: Path, frame_duration: float) -> int:
    """Write an ffmpeg concat list, skipping frames that have since been deleted.

    Retention can delete captures while a night is still running, so the manifest
    the session appended to is a list of what *was* there. Filtering here means a
    pruned frame costs one frame of the timelapse rather than the whole encode.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with manifest_path.open("w", encoding="utf-8") as handle:
        handle.write("ffconcat version 1.0\n")

        for frame in frames:
            if not frame.is_file():
                continue

            # Single quotes are the concat format's escape; a path containing one
            # would otherwise break out of the argument.
            escaped = str(frame.resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
            handle.write(f"duration {frame_duration:.6f}\n")
            written += 1

    if written:
        # The concat demuxer ignores the final entry's duration, so the last frame
        # is repeated - without this it flashes past in a single frame time.
        with manifest_path.open("a", encoding="utf-8") as handle:
            escaped = str(frames[-1].resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")

    return written


def encode_timelapse(
    frames: list[Path],
    output_path: Path,
    *,
    fps: int = 30,
    codec: str = "h264",
    quality: int = 23,
    width: int = 1920,
    manifest_path: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> EncodeResult:
    """Encode an ordered list of stills into a video.

    Returns a result rather than raising: a failed encode at sunrise must not stop
    the other processors from finalising, and the reason belongs on the product so
    the UI can show it.

    `on_progress` is called with a real percentage, read from ffmpeg's own
    `-progress` stream. A four-minute encode on a Pi is indistinguishable from a
    hang without it.
    """
    binary = ffmpeg_binary()

    if binary is None:
        return EncodeResult(ok=False, error="ffmpeg is not installed on this server")

    if not frames:
        return EncodeResult(ok=False, error="No frames to encode")

    manifest = manifest_path or output_path.with_suffix(".concat")
    frame_count = write_concat_manifest(frames, manifest, 1.0 / max(1, fps))

    if frame_count == 0:
        return EncodeResult(ok=False, error="Every frame had been deleted before encoding")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    codec_args = CODECS.get(codec, CODECS["h264"])

    # Scale to an even width and height whatever the source: H.264 4:2:0 cannot
    # represent an odd dimension, and a 4056x3040 frame scaled to 1920 lands on
    # 1439.4 vertically.
    scale = f"scale={width}:-2:flags=lanczos" if width > 0 else "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    command = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "concat",
        # The manifest holds absolute paths, which the demuxer refuses without this.
        "-safe", "0",
        "-i", str(manifest),
        "-vf", scale,
        "-r", str(fps),
        *codec_args,
        "-crf", str(quality),
        # Lets a browser start playing before the whole file has downloaded.
        "-movflags", "+faststart",
    ]

    if on_progress is not None:
        # Machine-readable key=value progress on stdout, leaving stderr for the
        # diagnostics we want if it fails.
        command += ["-progress", "pipe:1", "-nostats"]

    command.append(str(output_path))

    try:
        if on_progress is None:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=ENCODE_TIMEOUT_SECONDS, check=False
            )
            return_code, stderr = completed.returncode, completed.stderr
        else:
            return_code, stderr = _run_with_progress(command, frame_count, on_progress)

    except subprocess.TimeoutExpired:
        return EncodeResult(ok=False, error="ffmpeg timed out")

    except OSError as error:
        return EncodeResult(ok=False, error=f"Could not run ffmpeg: {error}")

    finally:
        manifest.unlink(missing_ok=True)

    if return_code != 0 or not output_path.is_file():
        # ffmpeg's diagnostics are long and the useful part is at the end.
        detail = (stderr or "").strip().splitlines()[-3:]
        message = " ".join(detail) or f"ffmpeg exited {return_code}"

        logger.warning("processing.encode_failed", output=str(output_path), error=message)

        return EncodeResult(ok=False, error=message[:500])

    return EncodeResult(
        ok=True,
        path=output_path,
        duration_seconds=frame_count / max(1, fps),
        frame_count=frame_count,
    )


def _run_with_progress(
    command: list[str], total_frames: int, on_progress: Callable[[float], None]
) -> tuple[int, str]:
    """Run ffmpeg, translating its progress stream into a percentage.

    ffmpeg emits `frame=N` lines to stdout under `-progress`. Against the frame
    count we already know from the manifest, that is an exact percentage rather
    than an estimate.

    stderr is drained on a thread. Without that, a long diagnostic fills the pipe
    buffer and ffmpeg blocks writing to it - a deadlock that looks exactly like
    the hang this function exists to make visible.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        if process.stderr is None:
            return

        for line in process.stderr:
            stderr_chunks.append(line)

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()

    try:
        if process.stdout is not None:
            for line in process.stdout:
                key, separator, value = line.strip().partition("=")

                if separator and key == "frame" and total_frames > 0:
                    try:
                        on_progress(min(100.0, int(value) * 100.0 / total_frames))
                    except (TypeError, ValueError):
                        continue

        process.wait(timeout=ENCODE_TIMEOUT_SECONDS)

    except subprocess.TimeoutExpired:
        process.kill()
        raise

    finally:
        reader.join(timeout=5)

    return process.returncode, "".join(stderr_chunks)


def container_for(codec: str) -> str:
    return CONTAINER_FOR_CODEC.get(codec, "mp4")
