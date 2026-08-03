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

from . import layout as layout_mod
from . import scenes as scenes_mod
from .device import Panel, discover, open_panels
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
        self.scene_name = scene
        self._producer = scenes_mod.get(scene).build(self.layout, self.names)
        self._scene_started = 0.0

        for panel in self.panels.values():
            panel.set_sleeping(False)
            panel.set_brightness(self.brightness)

    def _resolve_layout(self):
        saved = layout_mod.load()
        if saved is not None:
            # The saved layout keys panels by device path; keep only what is
            # currently attached.
            placements = [p for p in saved.placements if p.name in self.panels]
            if placements:
                return layout_mod.Layout(placements)
        return layout_mod.default_layout()

    # -- control -----------------------------------------------------------

    def set_scene(self, name: str, *, now: float) -> str:
        scene = scenes_mod.get(name)
        producer = scene.build(self.layout, self.names)
        with self._lock:
            self._producer = producer
            self.scene_name = name
            self._scene_started = now
        return name

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
                    return f"OK {self.scene_name}"
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
                return (
                    f"OK scene={self.scene_name} brightness={self.brightness} "
                    f"panels={len(self.panels)} canvas="
                    f"{self.layout.size[1]}x{self.layout.size[0]}"
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
                    producer = self._producer
                    started = self._scene_started
                # Scene time restarts on switch so animations begin at zero
                # rather than jumping into the middle of their cycle.
                frames = producer((now - self._t0) - started)
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
