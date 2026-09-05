"""Resolver for platform and toolchain strategy selection."""

from __future__ import annotations

from typing import Optional

from .base import BasePlatformStrategy
from .context import PlatformContext
from .darwin import MacOSPlatform
from .linux_gcc13 import LinuxGcc13Platform
from .linux_gcc15 import LinuxGcc15Platform
from .windows_ucrt64 import WindowsUcrt64Platform


class PlatformStrategyResolver:
    """Select the concrete strategy based on the detected platform and toolchain."""

    def resolve(self, context: PlatformContext) -> Optional[BasePlatformStrategy]:
        info = context.platform_info
        is_macos = getattr(info, "is_macos", False)
        is_windows = getattr(info, "is_windows", False)
        is_msys2 = getattr(info, "is_msys2", False)
        is_ucrt64 = getattr(info, "is_ucrt64", False)
        is_linux = getattr(info, "is_linux", False)
        gcc_major_version = getattr(info, "gcc_major_version", None)

        if is_macos:
            return MacOSPlatform()

        if is_windows and is_msys2 and is_ucrt64:
            return WindowsUcrt64Platform()

        if is_linux:
            if gcc_major_version is not None and gcc_major_version >= 15:
                return LinuxGcc15Platform()
            return LinuxGcc13Platform()

        for strategy in (LinuxGcc15Platform, LinuxGcc13Platform, WindowsUcrt64Platform, MacOSPlatform):
            if strategy is LinuxGcc15Platform and gcc_major_version is not None and gcc_major_version >= 15:
                return LinuxGcc15Platform()
        return None


def resolve_platform_strategy(context: PlatformContext) -> Optional[BasePlatformStrategy]:
    """Convenience function for platform strategy resolution."""
    return PlatformStrategyResolver().resolve(context)
