"""Video playback via ffmpeg.

ffmpeg does the decode, scale, crop, greyscale conversion and frame rate
conversion in a single pass and hands back raw 8-bit luma - which is already
exactly the frame format the panels want, so there is no per-frame conversion
work left to do in Python.

Decoding runs on a background thread feeding a small queue. At 9x34 ffmpeg is
far faster than real time, but keeping it off the display loop means a decode
hiccup can't stall frame pacing.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading

import numpy as np

from ..protocol import HEIGHT, WIDTH


def _filter_chain(width: int, height: int, fps: float, fit: str) -> str:
    if fit == "stretch":
        scale = f"scale={width}:{height}"
    elif fit == "contain":
        scale = (
            f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:  # cover
        scale = (
            f"scale=w={width}:h={height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    return f"fps={fps},{scale},format=gray"


class VideoSource:
    """Streams frames from a video file, optionally looping.

    Call it with a timestamp to get the current frame; it advances at the rate
    ffmpeg was asked to produce, independently of how often it's polled.
    """

    def __init__(
        self,
        path: str,
        *,
        fps: float = 30.0,
        fit: str = "cover",
        loop: bool = True,
        width: int = WIDTH,
        height: int = HEIGHT,
        queue_size: int = 8,
    ):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "ffmpeg not found on PATH - enter the dev shell, or use the "
                "packaged ledmx which wraps it in"
            )
        self.path = path
        self.fps = fps
        self.loop = loop
        self.width = width
        self.height = height
        self._frame_bytes = width * height

        self._queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._current = np.zeros((height, width), dtype=np.uint8)
        self._index = -1
        self.exhausted = False

        self._cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", path,
            "-vf", _filter_chain(width, height, fps, fit),
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "-",
        ]
        self._thread = threading.Thread(target=self._decode, daemon=True)
        self._thread.start()

    def _decode(self) -> None:
        while not self._stop.is_set():
            proc = subprocess.Popen(
                self._cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            assert proc.stdout is not None
            produced = 0
            try:
                while not self._stop.is_set():
                    raw = proc.stdout.read(self._frame_bytes)
                    if len(raw) < self._frame_bytes:
                        break
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                        self.height, self.width
                    )
                    produced += 1
                    while not self._stop.is_set():
                        try:
                            self._queue.put(frame, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            finally:
                proc.kill()
                proc.wait()

            if self._stop.is_set() or not self.loop or produced == 0:
                break

        self._queue.put(None)

    def __call__(self, t: float) -> np.ndarray:
        """Return the frame for time ``t``."""
        wanted = int(t * self.fps)
        # Pull forward to the requested frame, tolerating an empty queue by
        # holding the last frame rather than stalling the display loop.
        while self._index < wanted:
            try:
                frame = self._queue.get_nowait()
            except queue.Empty:
                break
            if frame is None:
                self.exhausted = True
                break
            self._current = frame
            self._index += 1
        return self._current

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def __enter__(self) -> "VideoSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def probe_duration(path: str) -> float | None:
    """Duration in seconds via ffprobe, or None if unavailable."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None
