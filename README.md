# ledmx

Animation and video toolkit for the Framework Laptop 16 LED Matrix input
modules.

```
ledmx list                                 # show attached panels
ledmx identify                             # label each panel on screen
ledmx text "BUILD PASSED" --direction up   # scrolling message
ledmx animate rain --layout reflect        # procedural effect
ledmx play clip.mp4 --fit cover            # video
ledmx clear
```

## What this hardware actually does

Most of the design here follows from measurements taken on real hardware, not
from the datasheet. They are worth reading before changing anything.

### One command per write(), always

The firmware's main loop does a single `serial.read(&mut buf)` of at most 64
bytes per iteration and parses **exactly one command** from the front of it.
Anything following that command in the same buffer is silently discarded.

Concatenating a frame into one write is therefore not an optimisation, it is
corruption: the USB stack splits the buffer at 64-byte boundaries and the
firmware misparses each fragment. It looks like noise on the panel, and the
write() calls all report success.

### The frame rate ceiling is ~59 commands/second

Not 59 frames - 59 *commands*. What that buys depends on the frame type:

| Frame type              | Commands | Ceiling  |
| ----------------------- | -------- | -------- |
| Black & white (`Draw`)  | 1        | ~59 fps  |
| Greyscale, full frame   | 10       | ~6 fps   |
| Greyscale, N columns    | N+1      | ~59/(N+1)|

Greyscale costs one `StageGreyCol` per column plus one flush. `Panel.show()`
sends only columns that changed, so mostly-static content runs far faster than
the 6 fps floor.

### Bit order differs between the two draw commands

This one is easy to get wrong because the two paths look symmetrical and are
not:

- **Greyscale** is column-major - one command carries one column of 34 values.
- **1-bit** is row-major - the firmware computes `index = x + WIDTH * y`.

Transposing the 1-bit path scatters a coherent image into isolated lit dots,
which reads as a dithering artefact rather than as a packing bug.

Both firmware paths apply `grid[8 - x]` internally, which cancels against the
physical LED wiring. No mirroring is needed at this layer - verified on
hardware with asymmetric static patterns and with motion.

### Dithering does not work here

Both approaches were built, measured on hardware, and rejected:

- **Temporal dithering** (varying a pixel's duty cycle across sub-frames)
  requires the duty cycle to repeat above the flicker fusion threshold, ~60 Hz.
  At 59 sub-frames/s a 4-phase cycle lands at ~15 Hz and a 2-phase cycle at
  ~30 Hz. The result is visible strobing, not perceived tone. The firmware's
  own hardware PWM already does this correctly, which is why native greyscale
  looks smooth.
- **Spatial dithering** works and cannot flicker, but 306 pixels leaves nowhere
  to hide a pattern. It reads as halftone print rather than as tone.

`ledmx.dither` keeps both for 1-bit conversion of photographic content, but
greyscale is the default and usually the right answer.

### Frame rate matters less than you would expect

A 9x34 grid quantises motion so coarsely that 6 fps and 56 fps are
indistinguishable for anything moving slower than roughly 6 px/sec - a drop
travelling 11 rows/sec only changes row 11 times a second, so the extra frames
show the same position repeatedly.

Spatial quantisation dominates temporal quantisation. Greyscale at 6 fps is the
default because it gives full tone at no perceptible motion cost.

### Never address panels by ttyACMn or by serial number

Both modules ship with the *same* serial number (`FRAKDEBZ0100000000`), so
`/dev/serial/by-id/` contains one symlink for two devices and resolves to
whichever enumerated first. `ttyACMn` numbering is a boot-time race.

The stable identifier is the USB topology path
(`pci-0000:c5:00.3-usb-0:4.2:1.0`), because each input deck bay is hardwired to
a fixed internal hub port. `ledmx.device.discover()` uses it exclusively.

Left and right are told apart by keyboard adjacency: on the Framework 16 the
left bay shares an internal hub with the keyboard and fingerprint reader, while
the right bay has a hub to itself. Sorting by USB path alone gets this
backwards.

### Text has to be rotated, and rotation is coupled to scroll direction

At 9 px wide, text rendered the obvious way is unreadable. Rotating it so the
reading direction runs along the 34 px axis gives glyphs nearly four times the
height.

The rotation direction is not free - it must match the scroll:

- **Scrolling up**: rotate clockwise, reading runs top-to-bottom.
- **Scrolling down**: rotate anticlockwise, reading runs bottom-to-top.

Pairing them the other way gives correctly-ordered upside-down letters, or
correctly-oriented letters in reverse order.

Do not rescale glyphs to fit the panel width. A bitmap font's strokes are a few
pixels across: interpolation blurs them together, nearest-neighbour deletes
whole pixel rows. Crop to the ink instead - the font's cell has enough blank
margin that the glyphs fit unscaled.

## Layouts

The input deck is modular, so panels are wherever you put them. A `Layout`
places each panel at an offset in a virtual canvas; animations render once at
canvas size and the layout slices out what each panel shows.

| Layout         | Canvas | Notes                                          |
| -------------- | ------ | ---------------------------------------------- |
| `flanking`     | 108x34 | Panels either side of the keyboard (default)   |
| `side-by-side` | 18x34  | Panels adjacent - one contiguous display       |
| `stacked`      | 9x68   | One tall display                               |
| `clone`        | 9x34   | Same frame on both panels                      |
| `reflect`      | 9x34   | Same frame, second panel flipped - symmetric   |

`flanking` uses `KEYBOARD_GAP` to represent the physical distance across the
keyboard, so motion leaving one panel reappears on the other with plausible
timing. It is an eyeballed constant, not an exact one.

## Development

```
nix develop          # dev shell with python, deps and ffmpeg
python3 -m ledmx --help
nix build            # build the package
```

Access to the panels needs no special group membership: `hardware.inputmodule.enable`
on NixOS installs `inputmodule-control` along with its
`50-framework-inputmodule.rules`, whose priority is load-bearing - the `uaccess`
tag has to be set before systemd's `73-seat-late.rules` evaluates it. Hand-copied
rules placed via `services.udev.extraRules` land at 99 and silently do nothing.
