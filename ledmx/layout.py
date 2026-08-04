"""Physical arrangement of panels within a virtual canvas.

The input deck is modular, so the panels are wherever you put them. Two common
arrangements behave very differently:

* **Flanking the keyboard** - two separate windows onto one scene, with a wide
  physical gap. Content should either be independent per panel, or deliberately
  exploit the gap.
* **Side by side** - one contiguous 18x34 display. Content spans seamlessly.

Rather than hard-coding either, a `Layout` places each panel at an offset in a
virtual canvas. Animations render once, at canvas size; the layout slices out
what each panel should show. Moving a module physically means editing the
layout, not touching any animation code.

Panels are identified by USB topology path, which is tied to the deck bay and
survives reboots - unlike ``ttyACMn`` numbering or the modules' serial numbers,
which are identical on both.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .device import PanelInfo, discover, identify_sides
from .protocol import HEIGHT, WIDTH

CONFIG_PATH = Path(
    os.environ.get("LEDMX_CONFIG")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "ledmx"
    / "layout.toml"
)


@dataclass(frozen=True)
class Placement:
    """Where one panel sits in the virtual canvas, and how it's oriented."""

    name: str
    x: int = 0
    y: int = 0
    #: Rotation applied to the slice before display, in degrees (0/90/180/270).
    #: A module inserted the other way up needs 180.
    rotate: int = 0
    flip_h: bool = False
    flip_v: bool = False

    def orient(self, tile: np.ndarray) -> np.ndarray:
        if self.rotate:
            tile = np.rot90(tile, k=(self.rotate // 90) % 4)
        if self.flip_h:
            tile = np.fliplr(tile)
        if self.flip_v:
            tile = np.flipud(tile)
        return np.ascontiguousarray(tile)


@dataclass
class Layout:
    placements: list[Placement] = field(default_factory=list)

    @property
    def size(self) -> tuple[int, int]:
        """Virtual canvas size as (height, width)."""
        if not self.placements:
            return (HEIGHT, WIDTH)
        h = max(p.y + HEIGHT for p in self.placements)
        w = max(p.x + WIDTH for p in self.placements)
        return (h, w)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.placements]

    def blank(self) -> np.ndarray:
        return np.zeros(self.size, dtype=np.uint8)

    @property
    def contiguous(self) -> bool:
        """Do the panels physically touch, forming one continuous display?

        Content that reads across panels - a time split into hours and minutes,
        an image spanning both - only works when they do. Separated by a
        keyboard, the same content reads as two unrelated things, so sources
        should check this and lay themselves out differently rather than
        assuming.
        """
        if len(self.placements) < 2:
            return False
        for i, a in enumerate(self.placements):
            for b in self.placements[i + 1:]:
                horizontal = a.y == b.y and abs(a.x - b.x) == WIDTH
                vertical = a.x == b.x and abs(a.y - b.y) == HEIGHT
                if horizontal or vertical:
                    return True
        return False

    def subset(self, names: list[str]) -> "Layout":
        """A layout covering only `names`, with its origin normalised.

        Normalising matters: a panel placed at x=99 in a flanking layout would
        otherwise produce a 108-wide canvas with 99 dead columns. Shifted back
        to the origin it becomes a tight 9x34, so a scene handed a single panel
        renders at panel size and lays itself out accordingly.
        """
        kept = [p for p in self.placements if p.name in names]
        if not kept:
            return Layout([])
        min_x = min(p.x for p in kept)
        min_y = min(p.y for p in kept)
        return Layout([
            Placement(
                name=p.name, x=p.x - min_x, y=p.y - min_y,
                rotate=p.rotate, flip_h=p.flip_h, flip_v=p.flip_v,
            )
            for p in kept
        ])

    def slice(self, canvas: np.ndarray) -> dict[str, np.ndarray]:
        """Cut a virtual-canvas frame into per-panel frames."""
        out: dict[str, np.ndarray] = {}
        for p in self.placements:
            tile = canvas[p.y:p.y + HEIGHT, p.x:p.x + WIDTH]
            if tile.shape != (HEIGHT, WIDTH):
                padded = np.zeros((HEIGHT, WIDTH), dtype=canvas.dtype)
                padded[:tile.shape[0], :tile.shape[1]] = tile
                tile = padded
            out[p.name] = p.orient(tile)
        return out


# -- presets ---------------------------------------------------------------

def side_by_side(names: list[str], gap: int = 0) -> Layout:
    """One contiguous display: 18x34, or wider with a gap between panels."""
    return Layout([
        Placement(names[0], x=0),
        Placement(names[1], x=WIDTH + gap),
    ])


#: Approximate width of the keyboard module expressed in panel pixels, for the
#: arrangement where the panels flank it. Only matters for content that spans
#: both panels: it sets how long something takes to cross the gap, so motion
#: leaving the left panel reappears on the right at the right moment. Eyeball
#: it against real content and adjust - there is no exact value, since the
#: physical gap isn't an integer number of LED pitches.
KEYBOARD_GAP = 90


def flanking(names: list[str], gap: int = KEYBOARD_GAP) -> Layout:
    """Panels either side of the keyboard - two windows onto one wide scene."""
    return Layout([
        Placement(names[0], x=0),
        Placement(names[1], x=WIDTH + gap),
    ])


def stacked(names: list[str], gap: int = 0) -> Layout:
    """One tall display: 9x68."""
    return Layout([
        Placement(names[0], y=0),
        Placement(names[1], y=HEIGHT + gap),
    ])


def cloned(names: list[str]) -> Layout:
    """Every panel shows the *same* frame - duplicated, not reflected."""
    return Layout([Placement(name, x=0, y=0) for name in names])


def reflected(names: list[str]) -> Layout:
    """Panels show the same frame, the second one flipped horizontally.

    A true mirror image rather than a duplicate. With the panels flanking the
    keyboard this reads as a symmetric pair - motion runs outward from the
    centre on both sides, or inward toward it - which suits the physical
    arrangement far better than two identical copies do.
    """
    return Layout([
        Placement(names[0], x=0, y=0),
        Placement(names[1], x=0, y=0, flip_h=True),
    ])


def single(name: str) -> Layout:
    return Layout([Placement(name)])


PRESETS = {
    "flanking": flanking,
    "side-by-side": side_by_side,
    "stacked": stacked,
    "clone": cloned,
    "reflect": reflected,
}


def preset(kind: str, names: list[str], gap: int | None = None) -> Layout:
    if kind == "clone":
        return cloned(names)
    if kind == "reflect":
        if len(names) < 2:
            return cloned(names)
        return reflected(names)
    if kind not in PRESETS:
        raise ValueError(
            f"unknown layout '{kind}'; choose from {', '.join(sorted(PRESETS))}"
        )
    if len(names) < 2:
        raise ValueError(f"layout '{kind}' needs two panels, got {len(names)}")
    if gap is None:
        gap = KEYBOARD_GAP if kind == "flanking" else 0
    return PRESETS[kind](names, gap)


# -- persistence -----------------------------------------------------------

def load(path: Path | None = None) -> Layout | None:
    """Load a saved layout, resolving USB paths to currently attached panels."""
    path = path or CONFIG_PATH
    if not path.exists():
        return None

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    by_usb = {p.usb_path: p for p in discover()}
    placements: list[Placement] = []
    for entry in data.get("panel", []):
        usb_path = entry.get("usb_path")
        if usb_path not in by_usb:
            continue
        placements.append(
            Placement(
                name=by_usb[usb_path].device,
                x=int(entry.get("x", 0)),
                y=int(entry.get("y", 0)),
                rotate=int(entry.get("rotate", 0)),
                flip_h=bool(entry.get("flip_h", False)),
                flip_v=bool(entry.get("flip_v", False)),
            )
        )

    # All or nothing. Moving one module changes its USB path, so a saved layout
    # can match some attached panels and not others - and applying it partially
    # leaves the unmatched panel with no placement at all, so it simply stays
    # dark with nothing to say why. Falling back to auto-detection puts content
    # on every panel, which is wrong in a visible, fixable way rather than an
    # invisible one.
    if len(placements) != len(by_usb):
        return None
    return Layout(placements) if placements else None


def save(layout: Layout, panels: dict[str, PanelInfo], path: Path | None = None) -> Path:
    """Persist a layout, keyed by USB path so it survives renumbering."""
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# ledmx panel layout.",
        "# Keyed by USB topology path: tied to the input deck bay, stable",
        "# across reboots, and unique per module (serial numbers are not).",
        "",
    ]
    for p in layout.placements:
        info = panels.get(p.name)
        if info is None:
            continue
        lines += [
            "[[panel]]",
            f'usb_path = "{info.usb_path}"',
            f"x = {p.x}",
            f"y = {p.y}",
            f"rotate = {p.rotate}",
            f"flip_h = {str(p.flip_h).lower()}",
            f"flip_v = {str(p.flip_v).lower()}",
            "",
        ]
    path.write_text("\n".join(lines))
    return path


def default_layout(kind: str = "flanking", gap: int | None = None) -> Layout:
    """Best-effort layout with no configuration.

    Prefers a saved config; otherwise orders panels left-to-right using the
    keyboard-adjacency heuristic, falling back to USB path order.
    """
    saved = load()
    if saved is not None:
        return saved

    panels = discover()
    if not panels:
        raise RuntimeError("no LED matrix modules found")
    if len(panels) == 1:
        return single(panels[0].device)

    sides = identify_sides()
    if "left" in sides and "right" in sides:
        names = [sides["left"].device, sides["right"].device]
    else:
        names = [p.device for p in panels]

    return preset(kind, names, gap)
