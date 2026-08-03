"""Animation sources.

A source is any callable taking a timestamp in seconds and returning a
``(height, width)`` uint8 frame at the canvas size it was constructed with.
That is the whole interface - a plain function works as well as a class.
"""

from .procedural import SOURCES, Fire, Life, MatrixRain, Plasma, Sweep
from .text import ScrollingText, StaticText
from .video import VideoSource, probe_duration

__all__ = [
    "SOURCES",
    "Fire",
    "Life",
    "MatrixRain",
    "Plasma",
    "ScrollingText",
    "StaticText",
    "Sweep",
    "VideoSource",
    "probe_duration",
]
