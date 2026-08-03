"""Discovering and talking to LED matrix panels.

Addressing is deliberately never done by ``/dev/ttyACMn`` or by serial number:

* ``ttyACMn`` numbering is kernel enumeration order - a boot-time race between
  two identical devices, so the same panel can change number across reboots.
* Framework ships both modules with the *same* serial (FRAKDEBZ0100000000), so
  ``/dev/serial/by-id/`` contains a single symlink for two devices and silently
  resolves to whichever won the race.

The stable identifier is the USB topology path, e.g.
``pci-0000:c5:00.3-usb-0:4.2:1.0``, because each input deck bay is hardwired to
a fixed internal hub port.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import serial

from . import protocol
from .protocol import HEIGHT, WIDTH

BY_PATH = Path("/dev/serial/by-path")
SYS_USB = Path("/sys/bus/usb/devices")

#: Product ID of the FW16 keyboard module, used to infer which side is which.
KEYBOARD_PRODUCT_ID = 0x0012


@dataclass(frozen=True)
class PanelInfo:
    """A discovered panel and the stable ways to refer to it."""

    device: str  # /dev/ttyACM0 - current, may change across boots
    usb_path: str  # pci-0000:c5:00.3-usb-0:3.3:1.0 - stable
    hub: str  # 1-3 - the internal hub this bay hangs off
    port: str  # 1-3.3 - the USB device node

    @property
    def by_path(self) -> str:
        return str(BY_PATH / self.usb_path)


def _read_attr(directory: Path, name: str) -> str | None:
    try:
        return (directory / name).read_text().strip()
    except OSError:
        return None


def _usb_device_dir(tty: str) -> Path | None:
    """Map /dev/ttyACM0 to its USB device directory in sysfs."""
    name = os.path.basename(tty)
    iface = Path(f"/sys/class/tty/{name}/device")
    if not iface.exists():
        return None
    # The tty hangs off the CDC interface (1-3.3:1.0); its parent is the
    # USB device itself (1-3.3), which carries idVendor/idProduct.
    return iface.resolve().parent


def _is_led_matrix(usb_dir: Path) -> bool:
    vid = _read_attr(usb_dir, "idVendor")
    pid = _read_attr(usb_dir, "idProduct")
    return (
        vid is not None
        and int(vid, 16) == protocol.VENDOR_ID
        and pid is not None
        and int(pid, 16) == protocol.LED_MATRIX_PRODUCT_ID
    )


def _hub_has_keyboard(hub_dir: Path) -> bool:
    """Does this hub also carry the FW16 keyboard module?

    On the Framework 16 the left bay shares an internal hub with the keyboard
    and fingerprint reader, while the right bay sits on a hub of its own. That
    makes keyboard adjacency a reliable, self-configuring way to tell the two
    identical panels apart - no per-machine configuration required.
    """
    if not hub_dir.exists():
        return False
    for child in hub_dir.iterdir():
        if not child.is_dir():
            continue
        vid = _read_attr(child, "idVendor")
        pid = _read_attr(child, "idProduct")
        if vid is None or pid is None:
            continue
        if int(vid, 16) == protocol.VENDOR_ID and int(pid, 16) == KEYBOARD_PRODUCT_ID:
            return True
    return False


def discover() -> list[PanelInfo]:
    """Find every attached LED matrix, sorted by USB path."""
    found: dict[str, PanelInfo] = {}

    for link in sorted(glob.glob(str(BY_PATH / "*"))):
        usb_path = os.path.basename(link)
        # by-path exposes both "usb-" and "usbv2-" spellings of the same
        # device; keep one.
        if "-usbv2-" in usb_path:
            continue

        device = os.path.realpath(link)
        usb_dir = _usb_device_dir(device)
        if usb_dir is None or not _is_led_matrix(usb_dir):
            continue

        port = usb_dir.name  # 1-3.3
        hub = port.rsplit(".", 1)[0] if "." in port else port  # 1-3
        found[device] = PanelInfo(
            device=device, usb_path=usb_path, hub=hub, port=port
        )

    return sorted(found.values(), key=lambda p: p.usb_path)


def identify_sides() -> dict[str, PanelInfo]:
    """Return panels keyed by ``"left"`` / ``"right"`` where determinable.

    Uses keyboard-adjacency (see `_hub_has_keyboard`). If that's inconclusive -
    a different machine, a single panel, an unusual deck layout - falls back to
    USB path ordering and the caller can override.
    """
    panels = discover()
    if not panels:
        return {}
    if len(panels) == 1:
        return {"left": panels[0]}

    left = [p for p in panels if _hub_has_keyboard(SYS_USB / p.hub)]
    right = [p for p in panels if p not in left]

    if len(left) == 1 and len(right) == 1:
        return {"left": left[0], "right": right[0]}

    # Inconclusive: fall back to path order, lowest path first.
    return {"left": panels[0], "right": panels[1]}


class Panel:
    """A single LED matrix, held open.

    The serial connection stays open for the lifetime of the object. The
    reference implementation opens a fresh connection per command, which is
    fine for one-shot CLI use and ruinous for animation.
    """

    def __init__(self, info: PanelInfo | str, *, baudrate: int = 115200):
        if isinstance(info, str):
            info = PanelInfo(device=info, usb_path="", hub="", port="")
        self.info = info
        self._serial = serial.Serial(info.device, baudrate, timeout=1)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()

    def __enter__(self) -> "Panel":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Panel {self.info.device} @ {self.info.usb_path or '?'}>"

    # -- raw ---------------------------------------------------------------

    def send(self, payload: bytes) -> None:
        """Write exactly one command.

        Never concatenate commands: the firmware reads one buffer per main-loop
        iteration and parses a single command from the front of it, discarding
        the rest.
        """
        self._serial.write(payload)

    def send_all(self, payloads: list[bytes]) -> None:
        for payload in payloads:
            self._serial.write(payload)

    # -- display -----------------------------------------------------------

    def show(self, pixels: np.ndarray) -> None:
        """Display a (34, 9) greyscale frame.

        Always sends all 9 columns plus a flush - 10 commands, ~6 fps. Sending
        only changed columns is not an available optimisation: the firmware
        zeroes its staging buffer on every flush, so any column left unstaged
        goes black rather than holding its previous value. See the note in
        `ledmx.protocol`.
        """
        self.send_all(protocol.greyscale_commands(pixels))

    def show_bw(self, pixels: np.ndarray) -> None:
        """Display a (34, 9) 1-bit frame - one command, so ~10x the frame rate."""
        self.send(protocol.bw_frame(pixels))

    def clear(self) -> None:
        self.send_all(protocol.blank())

    # -- settings ----------------------------------------------------------

    def set_brightness(self, percent: int) -> None:
        self.send(protocol.brightness(percent))

    def set_sleeping(self, state: bool) -> None:
        self.send(protocol.sleeping(state))


def open_panels(names: list[str] | None = None) -> dict[str, Panel]:
    """Open the named panels (default: every one discovered).

    Names are ``"left"`` / ``"right"``, resolved via `identify_sides`.
    """
    sides = identify_sides()
    if not sides:
        raise RuntimeError(
            "no LED matrix modules found - check they're seated and that "
            "/dev/serial/by-path contains 32ac:0020 devices"
        )

    wanted = names or list(sides)
    missing = [n for n in wanted if n not in sides]
    if missing:
        raise RuntimeError(
            f"panel(s) {', '.join(missing)} not found; available: "
            f"{', '.join(sorted(sides))}"
        )

    return {name: Panel(sides[name]) for name in wanted}


def blank_frame() -> np.ndarray:
    return np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
