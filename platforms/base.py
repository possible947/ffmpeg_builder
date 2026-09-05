"""Abstract platform strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List

from .context import PlatformContext


class BasePlatformStrategy(ABC):
    """Common interface for platform and toolchain strategy implementations."""

    @abstractmethod
    def setup_environment(self, context: PlatformContext) -> Dict[str, str]:
        """Construct environment variables for subprocess execution."""

    @abstractmethod
    def normalize_path(self, path: str | Path, context: PlatformContext) -> str:
        """Normalize a path for build scripts or flags."""

    @abstractmethod
    def get_pkg_config_path(self, context: PlatformContext, for_posix_shell: bool = False) -> str:
        """Return PKG_CONFIG_PATH formatted for the selected execution context."""

    @abstractmethod
    def get_cflags(self, context: PlatformContext) -> str:
        """Return C compile flags for the platform/toolchain."""

    @abstractmethod
    def get_cxxflags(self, context: PlatformContext) -> str:
        """Return C++ compile flags for the platform/toolchain."""

    @abstractmethod
    def get_ldflags(self, context: PlatformContext) -> str:
        """Return link flags for the platform/toolchain."""

    @abstractmethod
    def get_ldexeflags(self, context: PlatformContext) -> str:
        """Return executable-link flags for the platform/toolchain."""

    @abstractmethod
    def get_extralibs(self, context: PlatformContext) -> str:
        """Return platform-specific extra libraries."""

    @abstractmethod
    def get_cpp_runtime_lib(self) -> str:
        """Return the C++ runtime library link flag."""

    @abstractmethod
    def get_ffmpeg_threading_flag(self) -> str:
        """Return the FFmpeg threading configure flag."""

    @abstractmethod
    def get_system_lib_paths(self, context: PlatformContext) -> List[Path]:
        """Return system library search paths relevant to build tooling."""
