"""Command line interface."""

from __future__ import annotations

import argparse
import sys

import numpy as np

from . import canvas, dither, layout as layout_mod
from .device import Panel, discover, identify_sides, open_panels
from .layout import Layout, Placement
from .protocol import HEIGHT, WIDTH
from .runner import Runner
from .sources import SOURCES, ScrollingText, StaticText, VideoSource
from .sources.system import build_gauges


def _build_layout(args: argparse.Namespace) -> Layout:
    if args.layout == "auto":
        return layout_mod.default_layout()

    panels = discover()
    if not panels:
        raise SystemExit("no LED matrix modules found")

    sides = identify_sides()
    if "left" in sides and "right" in sides:
        names = [sides["left"].device, sides["right"].device]
    else:
        names = [p.device for p in panels]

    if args.panel:
        names = [n for n in names if n.endswith(args.panel) or n == args.panel]
        if not names:
            raise SystemExit(f"panel '{args.panel}' not found")

    if len(names) == 1:
        return layout_mod.single(names[0])
    return layout_mod.preset(args.layout, names, args.gap)


def _open(layout: Layout) -> dict[str, Panel]:
    return {p.name: Panel(p.name) for p in layout.placements}


def _run(args: argparse.Namespace, make_source) -> None:
    layout = _build_layout(args)
    size = layout.size
    source = make_source(size)

    panels = _open(layout)

    # Dithering runs per panel, on panel-sized frames.
    ditherer = None
    if args.dither != "off":
        ditherer = dither.make(
            args.dither, (HEIGHT, WIDTH), **(
                {"phases": args.dither_phases} if args.dither == "ordered" else {}
            )
        )

    runner = Runner(
        panels, fps=args.fps, brightness=args.brightness, ditherer=ditherer
    )

    use_gamma = not args.no_gamma

    def produce(t: float):
        frame = source(t)
        if use_gamma:
            frame = canvas.gamma(frame, args.gamma)
        return layout.slice(frame)

    mode = f"1-bit/{args.dither}" if ditherer else "greyscale"
    print(
        f"canvas {size[1]}x{size[0]}  panels={len(panels)}  "
        f"layout={args.layout}  mode={mode}  target={args.fps} fps"
        + ("  (ctrl-c to stop)" if args.duration is None else "")
    )
    stats = runner.run(produce, duration=args.duration)
    print(
        f"\n{stats.frames} frames in {stats.elapsed:.1f}s "
        f"= {stats.fps:.1f} fps produced"
    )
    for name, written in stats.per_panel_written.items():
        dropped = stats.per_panel_dropped.get(name, 0)
        print(
            f"  {name}: {written} written"
            + (f", {dropped} dropped" if dropped else "")
        )
    for panel in panels.values():
        panel.close()


# -- subcommands -----------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    panels = discover()
    if not panels:
        print("no LED matrix modules found")
        return
    sides = {v.device: k for k, v in identify_sides().items()}
    print(f"{'device':14s} {'side':6s} {'bay':8s} usb path")
    for p in panels:
        print(
            f"{p.device:14s} {sides.get(p.device, '?'):6s} {p.port:8s} {p.usb_path}"
        )


def cmd_identify(args: argparse.Namespace) -> None:
    """Label each panel so you can see which is which."""
    sides = identify_sides()
    if not sides:
        raise SystemExit("no LED matrix modules found")
    for name, info in sorted(sides.items()):
        with Panel(info) as panel:
            panel.set_sleeping(False)
            panel.set_brightness(args.brightness or 40)
            frame = StaticText(name.upper(), (HEIGHT, WIDTH))(0.0)
            panel.show(frame)
        print(f"{name:6s} -> {info.device}  ({info.port})")
    print("\nlook at the panels; if the labels are wrong, the modules have "
          "been rearranged - use 'ledmx save-layout' to record the new order")


def cmd_play(args: argparse.Namespace) -> None:
    def make(size):
        return VideoSource(
            args.file,
            fps=args.fps,
            fit=args.fit,
            loop=not args.once,
            width=size[1],
            height=size[0],
        )

    _run(args, make)


def cmd_animate(args: argparse.Namespace) -> None:
    cls = SOURCES[args.effect]

    def make(size):
        return cls(size)

    _run(args, make)


def cmd_text(args: argparse.Namespace) -> None:
    def make(size):
        if args.static:
            return StaticText(args.message, size)
        return ScrollingText(
            args.message, size, speed=args.speed, direction=args.direction
        )

    _run(args, make)


def cmd_monitor(args: argparse.Namespace) -> None:
    """One gauge per panel, so it works adjacent or flanking alike."""
    panels = open_panels()
    names = list(panels)
    stats, gauges = build_gauges(names, (HEIGHT, WIDTH))
    stats.interval = args.interval

    ditherer = None
    if args.dither != "off":
        ditherer = dither.make(args.dither, (HEIGHT, WIDTH))

    runner = Runner(
        panels, fps=args.fps, brightness=args.brightness, ditherer=ditherer
    )

    def produce(t: float):
        stats.update(t)
        return {name: gauges[name](t) for name in names}

    for name, gauge in gauges.items():
        print(f"  {name}: {gauge.label}")
    print(
        f"panels={len(panels)}  target={args.fps} fps"
        + ("  (ctrl-c to stop)" if args.duration is None else "")
    )

    result = runner.run(produce, duration=args.duration)
    print(f"\n{result.frames} frames in {result.elapsed:.1f}s")
    for panel in panels.values():
        panel.close()


def cmd_daemon(args: argparse.Namespace) -> None:
    from .daemon import Daemon, wait_for_panels

    if args.wait:
        wait_for_panels(timeout=args.wait)
    d = Daemon(fps=args.fps, brightness=args.brightness, scene=args.scene)
    # Unbuffered: under systemd stdout is a pipe to the journal, so without an
    # explicit flush the startup line only appears when the process exits.
    print(
        f"ledmx daemon: scene={d.scene_name} panels={len(d.panels)} "
        f"canvas={d.layout.size[1]}x{d.layout.size[0]} socket={d.socket_path}",
        flush=True,
    )
    d.run()
    print("ledmx daemon: stopped", flush=True)


def cmd_ctl(args: argparse.Namespace) -> None:
    """Send a command to the running daemon."""
    from .daemon import send

    print(send(" ".join([args.command, *args.args])))


def cmd_clear(args: argparse.Namespace) -> None:
    panels = open_panels()
    for name, panel in panels.items():
        panel.clear()
        panel.close()
        print(f"cleared {name}")


def cmd_save_layout(args: argparse.Namespace) -> None:
    panels = {p.device: p for p in discover()}
    if len(panels) < 1:
        raise SystemExit("no LED matrix modules found")

    order = args.order or sorted(panels)
    missing = [d for d in order if d not in panels]
    if missing:
        raise SystemExit(f"unknown device(s): {', '.join(missing)}")

    placements = []
    for i, device in enumerate(order):
        if args.layout == "stacked":
            placements.append(Placement(device, y=i * HEIGHT))
        elif args.layout == "clone":
            placements.append(Placement(device))
        elif args.layout == "reflect":
            placements.append(Placement(device, flip_h=(i == 1)))
        else:
            gap = args.gap if args.gap is not None else (
                layout_mod.KEYBOARD_GAP if args.layout == "flanking" else 0
            )
            placements.append(Placement(device, x=i * (WIDTH + gap)))

    path = layout_mod.save(Layout(placements), panels)
    print(f"wrote {path}")
    for p in placements:
        print(f"  {p.name} at ({p.x}, {p.y})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledmx",
        description="Animation and video toolkit for Framework 16 LED matrices",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_render_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--fps", type=float, default=8.0,
                       help="target frame rate (default: 8, matched to the ~6 "
                            "fps greyscale ceiling; raise it only in 1-bit mode)")
        p.add_argument("--brightness", type=int, default=40,
                       help="panel brightness percent (default: 40)")
        p.add_argument("--duration", type=float, default=None,
                       help="stop after N seconds (default: run until ctrl-c)")
        p.add_argument("--layout", default="auto",
                       choices=["auto", "flanking", "side-by-side", "stacked",
                                "clone", "reflect"],
                       help="how panels map to the canvas: 'clone' shows the "
                            "same frame on both, 'reflect' flips the second "
                            "for a symmetric pair, the rest tile one canvas "
                            "across both")
        p.add_argument("--gap", type=int, default=None,
                       help="gap between panels, in pixels")
        p.add_argument("--panel", default=None,
                       help="drive a single panel (e.g. /dev/ttyACM0)")
        p.add_argument("--gamma", type=float, default=2.2,
                       help="gamma correction exponent (default: 2.2)")
        p.add_argument("--no-gamma", action="store_true",
                       help="skip gamma correction")
        p.add_argument("--dither", default="off",
                       choices=["ordered", "floyd", "threshold", "off"],
                       help="'off' (default) uses native greyscale: full 8-bit "
                            "depth at ~6 fps. The others convert to 1-bit for "
                            "~59 fps, which is only worth it for content moving "
                            "faster than ~6 px/sec - below that, frame rate is "
                            "hidden by the 9x34 pixel grid and greyscale simply "
                            "looks better")
        p.add_argument("--dither-phases", type=int, default=1,
                       help="sub-frames per dither cycle. Leave at 1: anything "
                            "higher dithers over time, and at 59 sub-frames/s "
                            "a multi-phase cycle lands near 15 Hz, well below "
                            "flicker fusion, so pixels visibly strobe")

    p_list = sub.add_parser("list", help="show attached panels")
    p_list.set_defaults(func=cmd_list)

    p_ident = sub.add_parser("identify", help="label each panel on screen")
    p_ident.add_argument("--brightness", type=int, default=40)
    p_ident.set_defaults(func=cmd_identify)

    p_play = sub.add_parser("play", help="play a video file")
    p_play.add_argument("file")
    p_play.add_argument("--fit", default="cover",
                        choices=["cover", "contain", "stretch"])
    p_play.add_argument("--once", action="store_true", help="don't loop")
    add_render_args(p_play)
    p_play.set_defaults(func=cmd_play)

    p_anim = sub.add_parser("animate", help="run a procedural effect")
    p_anim.add_argument("effect", choices=sorted(SOURCES))
    add_render_args(p_anim)
    p_anim.set_defaults(func=cmd_animate)

    p_text = sub.add_parser("text", help="scroll a message")
    p_text.add_argument("message")
    p_text.add_argument("--speed", type=float, default=12.0)
    p_text.add_argument("--direction", default="up", choices=["up", "down"],
                        help="scroll direction; glyph rotation follows it so "
                             "the message reads correctly either way")
    p_text.add_argument("--static", action="store_true")
    add_render_args(p_text)
    p_text.set_defaults(func=cmd_text)

    p_mon = sub.add_parser("monitor", help="per-thread CPU and memory bars")
    p_mon.add_argument("--interval", type=float, default=0.4,
                       help="seconds between /proc samples (default: 0.4)")
    add_render_args(p_mon)
    p_mon.set_defaults(func=cmd_monitor)

    p_daemon = sub.add_parser("daemon", help="run the display daemon")
    p_daemon.add_argument("--scene", default="monitor")
    p_daemon.add_argument("--fps", type=float, default=6.0)
    p_daemon.add_argument("--brightness", type=int, default=45)
    p_daemon.add_argument("--wait", type=float, default=60.0,
                          help="seconds to wait for panels before giving up; "
                               "0 to fail immediately")
    p_daemon.set_defaults(func=cmd_daemon)

    # Thin clients - these are what hotkeys invoke.
    for name, help_text in [
        ("scene", "switch scene, or print the current one"),
        ("next", "cycle to the next scene"),
        ("prev", "cycle to the previous scene"),
        ("brightness", "set or print panel brightness"),
        ("status", "print daemon status"),
        ("scenes", "list available scenes"),
        ("notify", "scroll a message, then restore the previous scene"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("args", nargs="*")
        # "scenes" is friendlier on the command line than the wire command.
        p.set_defaults(func=cmd_ctl, command="list" if name == "scenes" else name)

    p_clear = sub.add_parser("clear", help="blank all panels")
    p_clear.set_defaults(func=cmd_clear)

    p_save = sub.add_parser("save-layout", help="record the physical arrangement")
    p_save.add_argument("--layout", default="flanking",
                        choices=["flanking", "side-by-side", "stacked",
                                 "clone", "reflect"])
    p_save.add_argument("--gap", type=int, default=None)
    p_save.add_argument("--order", nargs="*",
                        help="devices left-to-right, e.g. /dev/ttyACM1 /dev/ttyACM0")
    p_save.set_defaults(func=cmd_save_layout)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
