"""Linux GCC 15+ platform strategy."""

from __future__ import annotations

from typing import Dict

from .linux_gcc13 import LinuxGcc13Platform
from .context import PlatformContext


class LinuxGcc15Platform(LinuxGcc13Platform):
    """Linux strategy tuned for GCC 15+ default C23 behavior."""

    def setup_environment(self, context: PlatformContext) -> Dict[str, str]:
        env = super().setup_environment(context)
        if (
            context.platform_info.gcc_major_version is not None
            and context.platform_info.gcc_major_version >= 15
        ):
            env["CFLAGS"] = env.get("CFLAGS", "")
            env["CXXFLAGS"] = env.get("CXXFLAGS", "")
            if "-std=" not in env["CFLAGS"]:
                env["CFLAGS"] += " -std=gnu17"
            if "-std=" not in env["CXXFLAGS"]:
                env["CXXFLAGS"] += " -std=gnu++17"
        return env

    def get_cflags(self, context: PlatformContext) -> str:
        flags = super().get_cflags(context)
        if (
            context.platform_info.gcc_major_version is not None
            and context.platform_info.gcc_major_version >= 15
        ):
            if "-std=" not in flags:
                flags += " -std=gnu17"
        return flags.strip()

    def get_cxxflags(self, context: PlatformContext) -> str:
        flags = super().get_cxxflags(context)
        if (
            context.platform_info.gcc_major_version is not None
            and context.platform_info.gcc_major_version >= 15
        ):
            if "-std=" not in flags:
                flags += " -std=gnu++17"
        return flags.strip()
