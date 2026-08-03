"""Long-running daemon with a control socket.

Runs as a ``systemd --user`` service, holds the panels open, and renders
whichever scene is active. Clients switch scenes over a Unix socket, which is
what makes desktop hotkeys practical: a keybinding runs ``ledmx next``, which
connects, sends one line and exits.

A *user* service is the right scope. Panel access comes from a uaccess ACL
granted to the logged-in user, so anything running as that user gets it for
free - whereas a system-level unit runs as root or a system user and would need
group membership or its own udev rule.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from pathlib import Path

from dataclasses import dataclass, field

from . import layout as layout_mod
from . import scenes as scenes_mod
from .device import Panel, discover, open_panels
from .protocol import WIDTH
from .runner import _PanelWriter


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("/tmp")
    return base / "ledmx.sock"


def wait_for_panels(timeout: float = 60.0, interval: float = 2.0) -> None:
    """Block until at least one panel is present and readable.

    A user service can start before the graphical session is fully up, and the
    uaccess ACL is only applied once the seat session becomes active - so the
    device node can exist while still being unreadable. Failing immediately
    would leave the unit dead until manually restarted.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        panels = discover()
        if panels and os.access(panels[0].device, os.R_OK | os.W_OK):
            return
        time.sleep(interval)
    raise RuntimeError(
        "no readable LED matrix found - check the modules are seated and that "
        "the udev rules from hardware.inputmodule.enable are installed"
    )


@dataclass
class _Group:
    """A set of panels rendered together by one producer.

    Grouping is what separates "one image spanning both panels" from "the same
    scene drawn twice". Panels in a group share a canvas; panels in different
    groups are wholly independent, including their scene clocks.
    """

    panels: list[str]
    scene: str
    producer: object
    started: float


class Daemon:
    """Renders the active scene; accepts control commands on a Unix socket."""

    def __init__(
        self,
        *,
        fps: float = 6.0,
        brightness: int = 45,
        scene: str = "monitor",
        socket_path: Path | None = None,
    ):
        self.fps = fps
        self.brightness = brightness
        self.socket_path = socket_path or default_socket_path()

        self.panels: dict[str, Panel] = open_panels()
        self.names = list(self.panels)
        self.layout = self._resolve_layout()

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._groups: list[_Group] = []
        self._set_all(scene, now=0.0)

        for panel in self.panels.values():
            panel.set_sleeping(False)
            panel.set_brightness(self.brightness)

    def _resolve_layout(self):
        """Load the layout and rename its placements to match panel keys.

        Layouts identify panels by device path, because that is what survives
        being saved to disk. The daemon addresses them by side - "left",
        "right" - because that is what a person types at a hotkey. Those two
        namespaces have to be reconciled somewhere, and it has to be here:
        `Layout.subset` and `Layout.slice` both match on placement name, so a
        mismatch silently yields an empty layout and no frames. Per-panel
        scenes never touch the layout and so keep working, which makes the
        failure look like it belongs to specific scenes rather than to the
        naming.
        """
        by_device = {p.info.device: name for name, p in self.panels.items()}

        saved = layout_mod.load()
        base = saved if saved is not None else layout_mod.default_layout()

        placements = [
            layout_mod.Placement(
                name=by_device[p.name], x=p.x, y=p.y, rotate=p.rotate,
                flip_h=p.flip_h, flip_v=p.flip_v,
            )
            for p in base.placements
            if p.name in by_device
        ]
        if not placements:
            # Nothing matched - fall back to side-by-side over whatever is
            # attached, rather than rendering nothing at all.
            placements = [
                layout_mod.Placement(name=name, x=i * WIDTH)
                for i, name in enumerate(self.panels)
            ]
        return layout_mod.Layout(placements)

    # -- scene assignment --------------------------------------------------

    def _build(self, names: list[str], scene: str, now: float) -> "_Group":
        sub = self.layout.subset(names)
        producer = scenes_mod.get(scene).build(sub, names)
        return _Group(panels=list(names), scene=scene, producer=producer,
                      started=now)

    def _set_all(self, scene: str, *, now: float) -> str:
        """One scene across every panel, sharing a single canvas.

        Distinct from assigning the same scene to each panel individually: a
        spanning scene built once over the full layout draws one image across
        both panels, whereas per-panel builds would draw two unrelated copies.
        """
        group = self._build(self.names, scene, now)
        with self._lock:
            self._groups = [group]
        return scene

    def _set_panel(self, panel: str, scene: str, *, now: float) -> str:
        if panel not in self.panels:
            raise KeyError(
                f"unknown panel '{panel}'; known: {', '.join(self.names)}"
            )
        with self._lock:
            groups = list(self._groups)

        rebuilt: list[_Group] = []
        for group in groups:
            if panel not in group.panels:
                rebuilt.append(group)
                continue
            # Rebuild the group this panel is leaving, so a spanning scene
            # reflows onto the panels it still owns rather than rendering into
            # a hole.
            remaining = [p for p in group.panels if p != panel]
            if remaining:
                rebuilt.append(self._build(remaining, group.scene, now))

        rebuilt.append(self._build([panel], scene, now))
        with self._lock:
            self._groups = rebuilt
        return scene

    @property
    def scene_name(self) -> str:
        """The scene, when one covers everything; otherwise 'mixed'."""
        with self._lock:
            if len(self._groups) == 1:
                return self._groups[0].scene
            return "mixed"

    def assignments(self) -> dict[str, str]:
        with self._lock:
            return {
                name: group.scene
                for group in self._groups
                for name in group.panels
            }

    def set_scene(self, name: str, *, now: float) -> str:
        return self._set_all(name, now=now)

    def set_brightness(self, percent: int) -> int:
        percent = max(0, min(100, percent))
        with self._lock:
            self.brightness = percent
        for panel in self.panels.values():
            panel.set_brightness(percent)
        return percent

    def handle(self, line: str, *, now: float) -> str:
        parts = line.strip().split()
        if not parts:
            return "ERR empty command"
        cmd, args = parts[0].lower(), parts[1:]

        try:
            if cmd == "scene":
                if not args:
                    return "OK " + " ".join(
                        f"{p}={s}" for p, s in sorted(self.assignments().items())
                    )
                # "scene <name>" spans every panel; "scene <panel> <name>"
                # gives one panel its own scene.
                if len(args) >= 2:
                    return f"OK {args[0]}={self._set_panel(args[0], args[1], now=now)}"
                return f"OK {self.set_scene(args[0], now=now)}"
            if cmd in ("next", "prev"):
                step = 1 if cmd == "next" else -1
                target = scenes_mod.next_in_cycle(self.scene_name, step)
                return f"OK {self.set_scene(target, now=now)}"
            if cmd == "brightness":
                if not args:
                    return f"OK {self.brightness}"
                return f"OK {self.set_brightness(int(args[0]))}"
            if cmd == "off":
                return f"OK {self.set_scene('off', now=now)}"
            if cmd == "list":
                return "OK " + " ".join(sorted(scenes_mod.SCENES))
            if cmd == "status":
                assigned = " ".join(
                    f"{p}={s}" for p, s in sorted(self.assignments().items())
                )
                return (
                    f"OK {assigned} brightness={self.brightness} "
                    f"canvas={self.layout.size[1]}x{self.layout.size[0]} "
                    f"contiguous={self.layout.contiguous}"
                )
            if cmd in ("quit", "stop"):
                self._stop.set()
                return "OK stopping"
        except (KeyError, ValueError) as exc:
            return f"ERR {exc}"
        return f"ERR unknown command '{cmd}'"

    # -- socket ------------------------------------------------------------

    def _serve(self, server: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(1024).decode("utf-8", "replace")
                except OSError:
                    continue
                reply = self.handle(data, now=time.perf_counter() - self._t0)
                try:
                    conn.sendall((reply + "\n").encode())
                except OSError:
                    pass

    # -- main loop ---------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Stop cleanly on SIGTERM.

        systemd stops a unit with SIGTERM, whose default action terminates the
        process outright - the cleanup in run()'s finally block never executes,
        so the panels keep displaying the last frame and a stale socket is left
        behind for the next start to trip over. Catching it turns a kill into a
        normal shutdown.
        """

        def stop(signum, frame):  # noqa: ARG001 - signal handler signature
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, stop)
            except (OSError, ValueError):
                pass

    def run(self) -> None:
        self._install_signal_handlers()
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        # Owner-only: the socket controls a display, and there is no
        # authentication beyond filesystem permissions.
        os.chmod(self.socket_path, 0o600)
        server.listen(4)

        writers = {
            name: _PanelWriter(name, panel) for name, panel in self.panels.items()
        }
        for w in writers.values():
            w.start()

        self._t0 = time.perf_counter()
        threading.Thread(target=self._serve, args=(server,), daemon=True).start()

        interval = 1.0 / self.fps
        deadline = self._t0
        try:
            while not self._stop.is_set():
                now = time.perf_counter()
                with self._lock:
                    groups = list(self._groups)
                elapsed = now - self._t0
                for group in groups:
                    # Each group keeps its own clock, so a scene assigned to
                    # one panel starts from zero rather than joining another
                    # panel's animation mid-cycle.
                    frames = group.producer(elapsed - group.started)
                    for name, frame in frames.items():
                        writer = writers.get(name)
                        if writer is not None:
                            writer.submit(frame)

                deadline += interval
                slack = deadline - time.perf_counter()
                if slack > 0:
                    time.sleep(slack)
                else:
                    deadline = time.perf_counter()
        except KeyboardInterrupt:
            pass
        finally:
            for w in writers.values():
                w.stop()
            for w in writers.values():
                w.join(timeout=1.0)
            for panel in self.panels.values():
                try:
                    panel.clear()
                except OSError:
                    pass
                panel.close()
            server.close()
            if self.socket_path.exists():
                self.socket_path.unlink()


def send(command: str, socket_path: Path | None = None, timeout: float = 3.0) -> str:
    """Send one command to a running daemon and return its reply."""
    path = socket_path or default_socket_path()
    if not path.exists():
        raise RuntimeError(
            f"no daemon socket at {path} - is 'ledmx daemon' running? "
            f"(systemctl --user status ledmx)"
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(command.encode())
        return client.recv(4096).decode("utf-8", "replace").strip()
