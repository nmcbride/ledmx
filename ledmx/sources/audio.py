"""Audio spectrum visualiser, fed from PipeWire.

Two sources, chosen explicitly:

``output``  the monitor of the default sink - reacts to what is playing.
``mic``     the default input - reacts to the room.

Both are legitimate; what matters is that you get the one you asked for.
``--target <sink>`` alone does **not** select a monitor: PipeWire ignores it
for a capture stream and falls back to the default input. The property
``stream.capture.sink=true`` is what actually picks the sink's monitor ports.

That mistake is near-undetectable by watching the display, because a microphone
hears the speakers - audio playing still moves the bars, so a visualiser wired
to the mic looks like it is working correctly. `verify_source` inspects the
resulting graph links after start and refuses to run if the stream landed
somewhere other than what was requested. Recording a microphone is a reasonable
thing to do on purpose and an unreasonable thing to do by accident, especially
in something meant to run as a background service.

Decoding is a single `pw-record` process writing raw s16 mono to a pipe, read
by a background thread into a ring buffer.

**Analysis runs in that thread, not at display rate.** The FFT window is 46 ms
but frames render as slowly as every 167 ms, so a renderer that analyses only
when it draws sees about a quarter of the audio and misses the rest. A 25 ms
click falls into the gap roughly three times in four - which looks like an
unreliable visualiser rather than like a sampling bug. Running the analysis at
~50 Hz means consecutive windows overlap and nothing is missed, and the
envelope the renderer reads is already current.

Smoothing lives in the analysis thread too, with time constants in seconds, so
attack and release behave identically whether the display runs at 6 fps or 59.

This is the content the panels are best at. Bars are bold, legible at 9 px,
inherently on/off rather than tonal, and a spectrum has no fine detail to lose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

import numpy as np

from ..protocol import HEIGHT, WIDTH

Size = tuple[int, int]

RATE = 44100
#: Power-of-two window. 2048 samples at 44.1 kHz is ~46 ms - long enough to
#: resolve bass, short enough to feel responsive.
FFT_SIZE = 2048

#: Spectrum limits. Below ~40 Hz is mostly rumble, and the top octave carries
#: little that reads on nine bars.
FREQ_MIN = 40.0
FREQ_MAX = 16000.0

#: dB range mapped onto bar height, relative to full scale.
#:
#: Magnitudes are normalised by FFT_SIZE/2 first, so a full-scale sine reads
#: 0 dB. Without that normalisation an rfft over 2048 samples returns
#: magnitudes in the hundreds - about +54 dB for a full-scale tone - and any
#: audible signal pins every band to the top, which looks like a working
#: visualiser until you notice the bars never differ from each other.
DB_FLOOR = -60.0
DB_CEIL = -6.0

#: Analysis resolution and rate. More bands than any panel needs, so the
#: renderer can map them onto whatever width it has; fast enough that
#: successive 46 ms windows overlap and no transient falls between them.
ANALYSIS_BANDS = 64
ANALYSIS_INTERVAL = 0.02

#: Envelope time constants, in seconds. Fast attack catches transients, slower
#: release lets the eye follow the fall.
ATTACK = 0.02
RELEASE = 0.20


def _first_node(media_class: str) -> str | None:
    if shutil.which("pw-dump") is None:
        return None
    try:
        out = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=10
        )
        nodes = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None

    for node in nodes:
        props = (node.get("info") or {}).get("props") or {}
        if props.get("media.class") == media_class:
            name = props.get("node.name")
            if name:
                return name
    return None


def default_sink() -> str | None:
    """Node name of the default audio sink, whose monitor we can capture."""
    return _first_node("Audio/Sink")


def default_source() -> str | None:
    """Node name of the default audio input."""
    return _first_node("Audio/Source")


def verify_source(expect: str) -> str | None:
    """Check what our capture stream actually got linked to.

    Returns None if the links match `expect` ("monitor" or "input"), otherwise
    a description of what happened. Belt and braces: the property that selects
    a monitor is honoured by current PipeWire, but the consequence of it
    silently not being honoured is recording a microphone, so the graph gets
    checked rather than trusted.
    """
    if shutil.which("pw-link") is None:
        return None  # cannot verify; do not block on it
    try:
        out = subprocess.run(
            ["pw-link", "-l"], capture_output=True, text=True, timeout=10
        )
    except subprocess.SubprocessError:
        return None

    linked: list[str] = []
    in_block = False
    for line in out.stdout.splitlines():
        if line.startswith("pw-record"):
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if stripped.startswith("|<-") or stripped.startswith("|->"):
                linked.append(stripped[3:].strip())
            else:
                in_block = False

    if not linked:
        return None  # nothing to judge yet

    on_monitor = any(":monitor_" in link for link in linked)
    on_input = any("alsa_input" in link or ":capture_" in link for link in linked)

    if expect == "monitor" and not on_monitor:
        return (
            "requested system audio but the capture linked to "
            f"{', '.join(linked)} - refusing to run rather than record the "
            "microphone unintentionally"
        )
    if expect == "input" and not on_input:
        return (
            f"requested microphone but the capture linked to {', '.join(linked)}"
        )
    return None


class AudioCapture:
    """Reads the sink monitor into a ring buffer and computes band levels.

    One capture is shared by every panel showing a spectrum - running a
    `pw-record` per panel would double the work and let the two drift out of
    step with each other.
    """

    def __init__(
        self,
        source: str = "output",
        target: str | None = None,
        *,
        rate: int = RATE,
    ):
        if source not in ("output", "mic"):
            raise ValueError(f"source must be 'output' or 'mic', got {source!r}")
        self.source = source
        self.rate = rate
        self.target = target or (
            default_sink() if source == "output" else default_source()
        )
        self._buffer = np.zeros(FFT_SIZE, dtype=np.float32)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        self.error: str | None = None

        # Envelope maintained by the analysis thread, read by renderers.
        self._bands = np.zeros(ANALYSIS_BANDS, dtype=np.float32)
        self._analysis_edges = self._log_edges(ANALYSIS_BANDS)
        self._last_analysis = 0.0

    def _log_edges(self, bands: int) -> list[tuple[int, int]]:
        """FFT bin ranges for logarithmically spaced bands.

        Log spacing matters: linear bands would put most of them above 5 kHz,
        where music has little energy, and cram everything audible into the
        first few.
        """
        freqs = np.logspace(np.log10(FREQ_MIN), np.log10(FREQ_MAX), bands + 1)
        bin_hz = self.rate / FFT_SIZE
        edges = []
        for i in range(bands):
            lo = int(freqs[i] / bin_hz)
            hi = max(lo + 1, int(freqs[i + 1] / bin_hz))
            edges.append((lo, min(hi, FFT_SIZE // 2)))
        return edges

    def _analyse(self, now: float) -> None:
        """Update the envelope from the current window. Called at ~50 Hz."""
        dt = now - self._last_analysis if self._last_analysis else ANALYSIS_INTERVAL
        self._last_analysis = now

        with self._lock:
            samples = self._buffer.copy()

        spectrum = np.abs(np.fft.rfft(samples * self._window)) / (FFT_SIZE / 2)
        raw = np.zeros(ANALYSIS_BANDS, dtype=np.float32)
        for i, (lo, hi) in enumerate(self._analysis_edges):
            if hi > lo:
                # Peak rather than mean within the band: high bands span many
                # more bins than low ones, and averaging flattens a bright
                # cymbal into the surrounding silence.
                raw[i] = float(spectrum[lo:hi].max())

        with np.errstate(divide="ignore"):
            db = 20.0 * np.log10(np.maximum(raw, 1e-9))
        target = np.clip((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR), 0.0, 1.0)

        tau = np.where(target > self._bands, ATTACK, RELEASE)
        alpha = 1.0 - np.exp(-min(dt, 0.5) / np.maximum(tau, 1e-4))
        with self._lock:
            self._bands += (target - self._bands) * alpha

    def bands(self) -> np.ndarray:
        with self._lock:
            return self._bands.copy()

    def start(self) -> None:
        if shutil.which("pw-record") is None:
            self.error = "pw-record not found on PATH"
            return
        if self.target is None:
            kind = "sink" if self.source == "output" else "input"
            self.error = f"no audio {kind} found to capture"
            return

        cmd = ["pw-record"]
        if self.source == "output":
            # Selects the sink's monitor ports. Without this the stream links
            # to the default *input* instead - see the module docstring.
            cmd += ["-P", "{ stream.capture.sink=true }"]
        cmd += [
            "--target", self.target,
            "--rate", str(self.rate),
            "--channels", "1",
            "--format", "s16",
            "--latency", "20ms",
            "--container", "raw",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            self.error = f"could not start pw-record: {exc}"
            return

        # Give the graph a moment to settle before inspecting it.
        time.sleep(0.4)
        problem = verify_source("monitor" if self.source == "output" else "input")
        if problem is not None:
            self.error = problem
            self.stop()
            return

        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Drain the pipe to the newest audio, discarding any backlog.

        Processing every sample in order is wrong for a visualiser. pw-record
        writes into a 64 KB pipe, which at 88 KB/s holds up to three quarters
        of a second; a reader that never skips stays permanently that far
        behind, and the display lags the music by a fixed offset that no amount
        of frame rate fixes.

        Reading non-blocking until the pipe is empty and keeping only the last
        window means the display is always showing the most recent audio, and
        a hiccup costs a dropped instant rather than lasting latency.
        """
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        os.set_blocking(fd, False)

        window_bytes = FFT_SIZE * 2
        while not self._stop.is_set():
            pending = b""
            while True:
                try:
                    chunk = os.read(fd, 1 << 16)
                except BlockingIOError:
                    break
                except OSError:
                    return
                if not chunk:
                    if not pending:
                        return
                    break
                pending += chunk

            if pending:
                # Only the tail matters; everything older is already stale.
                tail = pending[-window_bytes:]
                if len(tail) % 2:
                    tail = tail[:-1]
                samples = (
                    np.frombuffer(tail, dtype=np.int16).astype(np.float32) / 32768.0
                )
                with self._lock:
                    if len(samples) >= FFT_SIZE:
                        self._buffer = samples[-FFT_SIZE:]
                    else:
                        self._buffer = np.concatenate(
                            [self._buffer[len(samples):], samples]
                        )

            now = time.perf_counter()
            if now - self._last_analysis >= ANALYSIS_INTERVAL:
                self._analyse(now)

            # Roughly one capture period; short enough to stay current, long
            # enough not to spin.
            time.sleep(0.005)

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def levels(self, columns: int) -> np.ndarray:
        """Current envelope resampled to `columns` bars, 0.0-1.0.

        The envelope is already smoothed and current - the renderer does no
        analysis of its own, it just maps the bands it has onto the columns it
        needs, taking the peak within each group so a narrow transient survives
        the reduction.
        """
        bands = self.bands()
        if columns >= ANALYSIS_BANDS:
            return np.interp(
                np.linspace(0, ANALYSIS_BANDS - 1, columns),
                np.arange(ANALYSIS_BANDS),
                bands,
            ).astype(np.float32)

        edges = np.linspace(0, ANALYSIS_BANDS, columns + 1).astype(int)
        return np.array(
            [
                bands[edges[i]:edges[i + 1]].max()
                if edges[i + 1] > edges[i] else 0.0
                for i in range(columns)
            ],
            dtype=np.float32,
        )


class Spectrum:
    """Frequency bars: one per column, with peak markers."""

    def __init__(
        self,
        capture: AudioCapture,
        size: Size = (HEIGHT, WIDTH),
        *,
        attack: float = 0.04,
        release: float = 0.25,
        peak_fall: float = 0.6,
        bar_level: int = 255,
        peak_level: int = 130,
    ):
        self.h, self.w = size
        self.capture = capture
        self.peak_fall = peak_fall
        self.bar_level = bar_level
        self.peak_level = peak_level

        self._peaks = np.zeros(self.w, dtype=np.float32)
        self._last_t = 0.0

    def __call__(self, t: float) -> np.ndarray:
        dt = min(0.5, max(0.0, t - self._last_t))
        self._last_t = t

        # No smoothing here: the envelope arrives already smoothed by the
        # analysis thread, which runs at ~50 Hz regardless of frame rate.
        # Smoothing again at display rate would only add latency.
        levels = self.capture.levels(self.w)

        self._peaks = np.maximum(
            np.maximum(levels, self._peaks - self.peak_fall * dt), 0.0
        )

        out = np.zeros((self.h, self.w), dtype=np.uint8)
        for x in range(self.w):
            filled = int(round(float(levels[x]) * self.h))
            if filled > 0:
                out[self.h - filled:, x] = self.bar_level
            peak_row = self.h - 1 - int(round(float(self._peaks[x]) * (self.h - 1)))
            if 0 <= peak_row < self.h - filled:
                out[peak_row, x] = self.peak_level
        return out
