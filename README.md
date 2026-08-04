# ledmx

Animation, video and status display toolkit for the Framework Laptop 16 LED
Matrix input modules. Two 9x34 greyscale panels, driven as one display or as
two independent ones.

```
ledmx list                      # attached panels and their USB paths
ledmx identify                  # label each panel on screen
ledmx monitor                   # CPU and memory gauges
ledmx animate rain              # procedural effect
ledmx play clip.mp4             # video
ledmx clear
```

Most of it runs through a daemon with a control socket, so a keybinding or a
script can change what is showing in one line.

---

## Contents

- [Control surface](#control-surface) - every command
- [Scenes](#scenes), [Gauges](#gauges), [Alerts](#alerts)
- [Driving it from a script or agent](#driving-it-from-a-script-or-agent)
- [Daemon and hotkeys](#daemon-and-hotkeys)
- [Layouts](#layouts)
- [Architecture](#architecture)
- [What this hardware actually does](#what-this-hardware-actually-does) - read
  before changing the rendering
- [Design constraints](#design-constraints-that-are-not-obvious)
- [State of things](#state-of-things)

---

## Control surface

Everything below is both a CLI subcommand and a line on the daemon's Unix
socket at `$XDG_RUNTIME_DIR/ledmx.sock`.

| Command | Effect |
| --- | --- |
| `scene <name>` | one scene across all panels, sharing a canvas |
| `scene <panel> <name>` | one panel only; the rest keep what they had |
| `next` / `prev` | cycle scenes (skips `off` and `listen`) |
| `toggle` | blank, or restore exactly what was showing |
| `off` | blank, one way |
| `brightness [0-100]` | set or read |
| `notify <message>` | scroll a message, then restore |
| `alert <pattern>` | flash a wordless pattern, then restore |
| `gauge <panel> <label> [value]` | show or update a gauge on one panel |
| `status` | current assignments, brightness, canvas |
| `scenes` / `alerts` | list what is available |

Commands that take free text accept `--` to end options, so a message
beginning with a dash survives:

```
ledmx notify "deploy finished" --speed 8 --direction down
ledmx alert rise --repeat 2
```

Standalone commands (not daemon clients): `list`, `identify`, `save-layout`,
`monitor`, `animate`, `text`, `play`, `clear`, `daemon`.

---

## Scenes

| Scene | |
| --- | --- |
| `monitor` | CPU on one panel, memory on the other |
| `clock` | hours and minutes, split across panels when adjacent, whole time on each when separated |
| `spectrum` | audio spectrum of **system output**, spanned across every panel it is given |
| `listen` | audio spectrum of the **microphone** - not in the cycle, no hotkey, must be named explicitly |
| `rain` | falling drops with fading trails |
| `plasma` | smooth interference pattern |
| `fire` | see `--source` below |
| `life` | Conway's, with a fade trail and auto-reseed on stagnation |
| `sweep` | a moving bar; a geometry test rather than decoration |
| `off` | blank, daemon still running |

`fire` burns from any edge: `bottom`, `top`, `left`, `right`, `both`
(top+bottom) or `sides` (left+right). Sideways sources suit this panel better -
see [Design constraints](#design-constraints-that-are-not-obvious). Paired with
the `reflect` layout, a sideways fire renders mirrored, each panel burning
inward from its outer edge.

---

## Gauges

A **gauge** shows a value. `spin` is not one - it shows only that something is
happening, which is a different job.

| Style | For | Notes |
| --- | --- | --- |
| `bar` | a fraction, 0-100 | optional peak marker |
| `blocks` | discrete steps | one block per step, **max 9**, clamped above that |
| `big` | a bare count | large digits, no fill |
| `sparkline` | trend over time | auto-scaled to its own data, no number |
| `dual` | a pair | two bars side by side |
| `bipolar` | signed values | fills up or down from a zero line |
| `spin` | unknown duration | four shapes, most with direction |

```
ledmx gauge left BD 60                      # 60%
ledmx gauge left ST 3 --of 5                # step 3 of 5
ledmx gauge left SY --spinner chase         # working, no idea how long
ledmx gauge left NT 42900 --style sparkline # trend; call repeatedly
ledmx gauge left IO 80 --second 25 --style dual
ledmx gauge left LT -45 --style bipolar
```

Spinners: `slide` (bounces, ignores direction), `orbit`, `chase`, `wave`.
Directions: `up`/`down` for chase, `left`/`right` for wave, `cw`/`ccw` for
orbit. Different shapes and directions exist so that with several jobs running,
the shape says *which* one a panel is showing.

Labels fit two characters. Longer ones fall back to a single initial rather
than a fragment - `MEM` truncated to `ME` reads as a different word.

Updating a gauge's *value* mutates state in place, so a caller polling every
second never restarts the animation. Changing its *style* rebuilds, because
the render mode belongs to the group.

---

## Alerts

Wordless patterns that interrupt, play, and restore. An alert only has to be
*recognised*, which is a far lower bar across a room than reading scrolling
text - and it is what most notifications actually want.

| | | | |
| --- | --- | --- | --- |
| `rise` success | `fall` failure | `blink` urgent | `pulse` informational |
| `fill` complete | `drain` time running out | `converge` closing | `diverge` opening |
| `bounce` in progress | `heartbeat` alive | `flicker` something wrong | |

```
ledmx alert rise
ledmx alert flicker --repeat 3
```

Monochrome 9x34 leaves two axes a person can distinguish at a glance -
direction and rhythm - so the set varies only those, and leans on existing
associations (rising for success) to avoid a mapping that has to be memorised.

---

## Driving it from a script or agent

The socket is the API. No D-Bus, no library - one line per call, and each one
leaves the other panel alone.

```bash
#!/usr/bin/env bash
# A long job reporting to the left panel while the clock keeps the right.

ledmx scene right clock
ledmx gauge left BD --spinner chase        # working, duration unknown

steps=(checkout build test package deploy)
for i in "${!steps[@]}"; do
    ledmx gauge left ST "$((i + 1))" --of "${#steps[@]}"   # step N of 5
    run_step "${steps[$i]}" || {
        ledmx alert fall                                   # failed
        ledmx notify "failed at ${steps[$i]}"
        exit 1
    }
done

ledmx alert rise                                           # succeeded
ledmx notify "deploy complete"
ledmx scene left clock                                     # hand the panel back
```

For percentage progress, update as often as you like - value updates are cheap
and do not restart anything:

```bash
while read -r pct; do ledmx gauge left DL "$pct"; done < <(download_with_progress)
```

Mixing scenes across panels is a script's job rather than a hotkey's - the
hotkeys deliberately do the obvious thing and take both panels:

```bash
ledmx scene left monitor      # CPU only (memory is the second metric,
ledmx scene right spectrum    # so one panel gets just CPU)
```

For a trend rather than a level, call the same gauge repeatedly with
`--style sparkline`; it keeps the last nine samples and scales to their range,
so any units work:

```bash
while :; do
    ledmx gauge left QD "$(queue_depth)" --style sparkline
    sleep 10
done
```

Parsing state back:

```bash
ledmx status      # OK left=gauge right=clock brightness=45 canvas=108x34 contiguous=False
ledmx scenes      # OK clock fire life listen monitor off plasma rain spectrum sweep
```

Two things worth knowing when generating calls:

- **Always pass `--` before free text** you did not write. Without it, a
  message beginning with a dash loses its first two words to option parsing.
- **`ledmx` exits non-zero and prints to stderr** if the daemon is not running,
  so a failed call is detectable rather than silent.

---

## Daemon and hotkeys

Run it as a **user** service, not a system one: panel access comes from a
uaccess ACL granted to the logged-in user, so a process running as that user
gets it for free, while a system unit would need group membership or a udev
rule of its own. Bind it to `graphical-session.target`, because the ACL only
exists while a seat session is active, and pass `--wait` to tolerate starting
before logind has applied it at login.

`contrib/ledmx.service` is a ready-to-install unit.
`contrib/home-manager.nix` does the same declaratively and adds keybindings.

This machine's setup lives in `nixos-configs`:

- `flake.nix` - the `ledmx` input, following the system nixpkgs
- `home/nmcbride/framework-16-nixos.nix` - package, user service, keybindings

FW16-only by placement: the FW13 imports a different home config, so it never
sees any of it. The input sitting in the shared `flake.nix` costs nothing,
since an unreferenced input is only a source.

| Key | |
| --- | --- |
| `Super+Alt+M` | CPU / memory |
| `Super+Alt+A` | audio spectrum |
| `Super+Alt+N` | next scene |
| `Super+Alt+O` | blank / restore |

**Not `Super+Alt+S`** - that is hardcoded in GNOME Shell as the screen reader
toggle and does not appear in `gsettings`, so scanning for conflicts will not
find it. Same for `Super+Alt+Left/Right`, which are workspace switching (those
*are* visible in `gsettings`). The only way to find a hardcoded collision is to
press the key.

`dconf.settings` replaces the `custom-keybindings` list wholesale, so any
shortcut added by hand in Settings and not declared here is dropped on the next
rebuild.

---

## Layouts

The input deck is modular, so panels are wherever you put them. A `Layout`
places each at an offset in a virtual canvas; scenes render once at canvas size
and the layout slices out what each panel shows.

| Layout | Canvas | |
| --- | --- | --- |
| `flanking` | 108x34 | either side of the keyboard |
| `side-by-side` | 18x34 | adjacent - one contiguous display |
| `stacked` | 9x68 | one tall display |
| `clone` | 9x34 | same frame on both |
| `reflect` | 9x34 | same frame, second panel flipped |

```
ledmx save-layout --layout flanking --order /dev/ttyACM1 /dev/ttyACM0
```

Saved to `~/.config/ledmx/layout.toml`, keyed by USB path. Sources check
`layout.contiguous` and lay themselves out accordingly - the clock splits the
time across adjacent panels but shows the whole time on each when separated,
since a time split across a keyboard reads as two unrelated numbers.

**Moving a module changes its USB path.** After rearranging: `ledmx list`,
`ledmx identify`, then `save-layout` again. A saved layout matching only some
attached panels is discarded entirely rather than applied partially - applying
it partially leaves the unmatched panel dark with nothing to say why.

---

## Architecture

```
protocol.py     command framing. Read the hardware notes before touching.
device.py       discovery by USB path, persistent connections, Panel
layout.py       Region placement, slicing, save/load, contiguity
runner.py       frame pacing, per-panel writer threads
daemon.py       control socket, scene groups, per-panel assignment
scenes.py       named scenes; each declares its render mode and rate
alerts.py       wordless patterns
font.py         3x5 micro font, fitting rules
canvas.py       image fitting, gamma, text strips for scrolling
dither.py       1-bit conversion (see why it mostly does not help)
sources/
  draw.py       Region + fill/segments/marker/line/block primitives
  progress.py   every gauge style, arranged from those primitives
  system.py     /proc sampling; renders via progress.Gauge
  audio.py      PipeWire capture and FFT
  clock.py, procedural.py, text.py, video.py
```

**Add a gauge style in `progress.py` using `draw` primitives.** Do not write
fill arithmetic inline - that is how two independent gauge implementations
happened, where fixing the peak marker in one silently missed the other.

**Scenes declare their own mode and rate.** A clock wants tone at 6 fps, a
spectrum wants speed at 55, and with per-panel assignment both can be live
simultaneously.

---

## What this hardware actually does

Measured on real hardware, not from the datasheet. Worth reading before
changing anything in the rendering path.

### One command per write(), always

The firmware's main loop does a single `serial.read(&mut buf)` of at most 64
bytes per iteration and parses **exactly one command** from the front of it.
Anything following that command in the same buffer is silently discarded.

Concatenating a frame into one write is not an optimisation, it is corruption:
the USB stack splits the buffer at 64-byte boundaries and the firmware
misparses each fragment. It looks like noise on the panel, and every `write()`
reports success.

### The frame rate ceiling is ~59 commands/second

Not 59 frames - 59 *commands*.

| Frame type | Commands | Ceiling |
| --- | --- | --- |
| Black & white (`Draw`) | 1 | ~59 fps |
| Greyscale, full frame | 10 | ~6 fps |

### Delta column updates do not work

`DrawGreyColBuffer` copies the staging buffer to the display and then *zeroes*
it:

```rust
state.grid = state.col_buffer.clone();
state.col_buffer = percentage(0);   // all-zero grid
```

Nothing carries over between frames. A column not re-staged before a flush is
displayed as **black**, not left alone.

Easy to ship by accident, because it is invisible for the common cases:
content on a black background, and fast motion where nearly every column
changes anyway. It surfaces as pixels winking out of slow-moving shapes - and
it inflates measured frame rates above the 6 fps ceiling, which is the tell.

### Bit order differs between the two draw commands

- **Greyscale** is column-major - one command carries one column of 34 values.
- **1-bit** is row-major - the firmware computes `index = x + WIDTH * y`.

Transposing the 1-bit path scatters a coherent image into isolated lit dots,
which reads as a dithering artefact rather than a packing bug.

Both firmware paths apply `grid[8 - x]` internally, which cancels against the
physical wiring. No mirroring is needed at this layer - verified with
asymmetric static patterns and with motion.

### Dithering does not work here

Both approaches were built, measured, and rejected:

- **Temporal** needs the duty cycle to repeat above flicker fusion (~60 Hz). At
  59 sub-frames/s a 4-phase cycle lands at ~15 Hz. The result is visible
  strobing, not perceived tone. The firmware's hardware PWM already does this
  properly, which is why native greyscale looks smooth.
- **Spatial** works and cannot flicker, but 306 pixels leaves nowhere to hide a
  pattern. It reads as halftone print rather than as tone.

### Frame rate matters less than you would expect

A 9x34 grid quantises motion so coarsely that 6 fps and 56 fps are
indistinguishable below roughly 6 px/sec. Spatial quantisation dominates
temporal quantisation. Greyscale at 6 fps is the default because it gives full
tone at no perceptible motion cost.

The exception is content where latency is visible against an external
reference - the audio spectrum, where the 1-bit path at ~48 fps removes about
340 ms of display lag that is plainly visible against a beat.

### Flush after every frame

`write()` returns once the kernel accepts the bytes, so submitting faster than
the module drains fills the tty buffer instead of blocking. A few KB against
~2.2 KB/s of real throughput is over a second of standing backlog that
frame-dropping cannot reach.

### Never address panels by ttyACMn or by serial number

Both modules ship with the *same* serial (`FRAKDEBZ0100000000`), so
`/dev/serial/by-id/` contains one symlink for two devices and resolves to
whichever enumerated first. `ttyACMn` numbering is a boot-time race.

The stable identifier is the USB topology path
(`pci-0000:c5:00.3-usb-0:4.2:1.0`) - each deck bay is hardwired to a fixed
internal hub port.

Left and right are told apart by keyboard adjacency: the bay sharing an
internal hub with the keyboard is the left one. Sorting by USB path gets this
backwards.

### Text has to be rotated, and rotation is coupled to scroll direction

At 9 px wide, text rendered the obvious way is unreadable. Rotating it so the
reading direction runs along the 34 px axis gives glyphs nearly four times the
height.

- **Scrolling up**: rotate clockwise, reading runs top-to-bottom.
- **Scrolling down**: rotate anticlockwise, reading runs bottom-to-top.

Pairing them the other way gives correctly-ordered upside-down letters, or
correctly-oriented letters in reverse order. Reversed text still *looks* like
scrolling text - right motion, right rhythm, legible glyphs - so it passes a
casual glance. Check the first frame, not the animation.

Do not rescale glyphs to fit. Interpolation blurs a 3px stroke into mush;
nearest-neighbour deletes whole pixel rows. Crop to the ink instead - the
font's cell has enough blank margin that glyphs fit unscaled.

### Audio capture is not what the obvious flag suggests

`pw-record --target <sink>` does **not** capture that sink's monitor. PipeWire
ignores it for a capture stream and falls back to the default input - the
microphone. `stream.capture.sink=true` is what actually selects monitor ports.

That failure is near-undetectable by watching the display, because a microphone
hears the speakers: audio playing still moves the bars. `verify_source` now
inspects the graph links after starting and refuses to run if the stream landed
somewhere other than what was asked for.

Analysis runs in the capture thread at ~50 Hz, not at display rate. A 46 ms FFT
window sampled every 167 ms sees a quarter of the audio, and a 25 ms transient
vanishes three times in four.

---

## Design constraints that are not obvious

Things learned by looking at the panels, which no amount of unit testing would
have surfaced.

**Legibility beats information density.** An early monitor drew one
one-pixel-wide bar per hardware thread, which fit the canvas exactly and was
unreadable. 306 pixels carries a few large shapes and a couple of words.

**Perceptual caps, not physical ones.** `blocks` stops at 9 because people read
about four items instantly and can count nine or ten with effort; beyond that
they estimate proportion, at which point the blocks are decoration on a bar.
More would fit.

**Rates must be in seconds, not frames.** A coefficient applied per frame
silently changes meaning between 6 fps and 59 fps. This caused two separate
bugs - rain trails vanishing, and the spectrum lagging by two thirds of a
second.

**Nine pixels of width cannot show horizontal detail.** Fire reads through
sideways licking; on this panel a bottom-burning fire is a vertical shimmer.
Burning from a *long* edge gives the spread 34 pixels, which is the only axis
with room. Effects with a strong vertical axis - rain, falling, rising - work
because falling is a vertical idea.

**Ambiguity at the extremes is what misleads.** Any progress should light at
least one block, and anything short of complete should leave one unlit -
being told a job has finished when it has not is worse than under-reporting.

---

## State of things

Verified on hardware: protocol in both modes, discovery and side detection, all
five layouts, every scene, every gauge style and spinner, all eleven alerts,
notify in both directions, the blank toggle, video playback single and spanning,
the audio spectrum against music and a click track, per-panel assignment, and
the daemon under systemd.

**Not verified: startup at login.** `--wait 90` exists for the daemon starting
before logind grants the panel ACL, and it has only ever started against a
session that already had it. The first reboot is the test;
`systemctl --user status ledmx` will report a failure if it comes to one.

Ideas not built: a D-Bus listener so desktop notifications appear automatically
(wants filtering, or every "screenshot saved" scrolls past); per-key keyboard
RGB so a sweep can cross the keyboard between panels (needs root for
`framework_tool --rgbkbd`); network and temperature gauges.

---

## Development

```
nix develop          # python, deps, ffmpeg, inputmodule-control
python3 -m ledmx --help
nix build
```

Access needs no group membership: `hardware.inputmodule.enable` on NixOS
installs `inputmodule-control` along with its
`50-framework-inputmodule.rules`, whose priority is load-bearing - the
`uaccess` tag must be set before systemd's `73-seat-late.rules` evaluates it.
Hand-copied rules placed via `services.udev.extraRules` land at 99 and silently
do nothing.

When something looks wrong on the panel, render a frame to stdout as ASCII
before testing on hardware. Nearly every rendering bug in this project was
obvious in a dumped frame and invisible in a passing test - reversed text,
scattered dots from a transpose, a bar thresholded out of existence, blocks
that were secretly a fill.
