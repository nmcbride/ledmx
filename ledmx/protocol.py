"""Wire protocol for Framework input modules.

Every command is ``MAGIC + [command_id] + parameters``, sent over the module's
USB CDC-ACM serial port. The baud rate is nominally 115200 but is ignored by
the firmware - measured throughput exceeds what 115200 would allow, so the real
limit is USB transaction and firmware timing, not line rate.

**One command per write(), always.** The firmware's main loop does a single
``serial.read(&mut buf)`` of at most 64 bytes per iteration and parses exactly
one command from the front of it; anything following that command in the same
buffer is silently discarded. Concatenating a whole frame into one write is
therefore not an optimisation, it is corruption - the USB stack splits the
buffer at 64-byte boundaries and the firmware misparses each fragment.

That puts a hard ceiling on frame rate, measured at roughly 59 commands per
second:

===================  ========  =========================================
Frame type           Commands  Ceiling
===================  ========  =========================================
Black & white        1         ~59 fps
Full greyscale       10        ~6 fps   (9 columns + one flush)
Greyscale, N cols     N+1      ~59/(N+1) fps
===================  ========  =========================================

Hence `greyscale_commands` returns a *list*, and `changed_columns` exists so
callers can send only the columns that actually differ from what's on screen.
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np

MAGIC = b"\x32\xac"

VENDOR_ID = 0x32AC
LED_MATRIX_PRODUCT_ID = 0x0020

#: Panel geometry. The matrix is portrait: 9 columns wide, 34 rows tall.
WIDTH = 9
HEIGHT = 34


class Command(IntEnum):
    BRIGHTNESS = 0x00
    PATTERN = 0x01
    BOOTLOADER_RESET = 0x02
    SLEEP = 0x03
    ANIMATE = 0x04
    PANIC = 0x05
    DRAW_BW = 0x06
    STAGE_GREY_COL = 0x07
    DRAW_GREY_COL_BUFFER = 0x08
    SET_TEXT = 0x09
    START_GAME = 0x10
    GAME_CONTROL = 0x11
    GAME_STATUS = 0x12
    SET_COLOR = 0x13
    DISPLAY_ON = 0x14
    INVERT_SCREEN = 0x15
    SET_PIXEL_COLUMN = 0x16
    FLUSH_FRAMEBUFFER = 0x17
    CLEAR_RAM = 0x18
    SCREEN_SAVER = 0x19
    SET_FPS = 0x1A
    SET_POWER_MODE = 0x1B
    PWM_FREQ = 0x1E
    DEBUG_MODE = 0x1F
    VERSION = 0x20


def command(cmd: Command, *params: int) -> bytes:
    """Frame a single command with its parameters."""
    return MAGIC + bytes([int(cmd), *params])


def _as_frame(pixels: np.ndarray) -> np.ndarray:
    if pixels.shape != (HEIGHT, WIDTH):
        raise ValueError(
            f"expected {(HEIGHT, WIDTH)} frame, got {pixels.shape}"
        )
    if pixels.dtype != np.uint8:
        pixels = np.clip(pixels, 0, 255).astype(np.uint8)
    return pixels


def stage_column(index: int, values: np.ndarray) -> bytes:
    """One STAGE_GREY_COL command: 38 bytes, comfortably under the 64 limit."""
    return MAGIC + bytes([int(Command.STAGE_GREY_COL), index]) + values.tobytes()


def flush_columns() -> bytes:
    """Commit staged columns to the display atomically."""
    return command(Command.DRAW_GREY_COL_BUFFER, 0x00)


def greyscale_commands(pixels: np.ndarray) -> list[bytes]:
    """Commands for a full greyscale frame - one per column, plus a flush.

    `pixels` is (HEIGHT, WIDTH) uint8 in standard image row-major order, so it
    drops straight out of Pillow, ffmpeg or numpy. The wire format wants
    column-major, so the transpose happens here against a contiguous array.

    Each element of the returned list must be sent as its own write().
    """
    pixels = _as_frame(pixels)
    columns = np.ascontiguousarray(pixels.T)
    out = [stage_column(x, columns[x]) for x in range(WIDTH)]
    out.append(flush_columns())
    return out


def changed_columns(pixels: np.ndarray, previous: np.ndarray | None) -> list[bytes]:
    """Commands updating only the columns that differ from `previous`.

    Since cost scales with command count, not bytes, skipping unchanged columns
    is the main lever available for greyscale frame rate. Content with a static
    background or limited horizontal motion can run several times faster than
    the ~6 fps a full-frame update allows.

    The flush is still required, so a frame touching N columns costs N+1
    commands. Returns an empty list when nothing changed at all.
    """
    pixels = _as_frame(pixels)
    if previous is None:
        return greyscale_commands(pixels)

    columns = np.ascontiguousarray(pixels.T)
    prev_columns = np.ascontiguousarray(_as_frame(previous).T)

    out = [
        stage_column(x, columns[x])
        for x in range(WIDTH)
        if not np.array_equal(columns[x], prev_columns[x])
    ]
    if not out:
        return []
    out.append(flush_columns())
    return out


def bw_frame(pixels: np.ndarray) -> bytes:
    """Serialise a 1-bit frame as a single DRAW_BW command.

    One command instead of ten, so roughly ten times the frame rate of
    greyscale. `pixels` is (HEIGHT, WIDTH); anything non-zero lights the LED.

    Bit order is **row-major** - the firmware computes ``index = x + WIDTH * y``
    and reads bit ``index % 8`` of byte ``index // 8``. That is exactly the
    natural flattening of a (HEIGHT, WIDTH) array, so no transpose is involved.
    Transposing here scatters a coherent image into isolated dots, which is
    subtle enough to look like a dithering artefact rather than a packing bug.
    """
    if pixels.shape != (HEIGHT, WIDTH):
        raise ValueError(
            f"expected {(HEIGHT, WIDTH)} frame, got {pixels.shape}"
        )

    flat = (pixels.reshape(-1) != 0)
    packed = np.packbits(flat, bitorder="little")
    return MAGIC + bytes([int(Command.DRAW_BW)]) + packed.tobytes()


def brightness(percent: int) -> bytes:
    """Set the panel's maximum brightness (0-100)."""
    return command(Command.BRIGHTNESS, max(0, min(100, int(percent))))


def sleeping(state: bool) -> bytes:
    return command(Command.SLEEP, 1 if state else 0)


def animate(state: bool) -> bytes:
    return command(Command.ANIMATE, 1 if state else 0)


def blank() -> list[bytes]:
    """An all-off greyscale frame."""
    return greyscale_commands(np.zeros((HEIGHT, WIDTH), dtype=np.uint8))
