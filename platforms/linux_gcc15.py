"""Linux GCC 15+ platform strategy."""

from __future__ import annotations

from .linux_gcc13 import LinuxGcc13Platform


class LinuxGcc15Platform(LinuxGcc13Platform):
    """Linux strategy marker for GCC 15+ hosts (FFmpeg 9 policy path)."""
