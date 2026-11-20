"""Platform execution context used by platform strategy objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ffmpeg_builder.config import BuildConfig
from ffmpeg_builder.platform_detect import PlatformDetector, PlatformInfo

if TYPE_CHECKING:
    from ffmpeg_builder.cross_environment import CrossEnvironment


@dataclass(frozen=True)
class PlatformContext:
    """Context passed to platform strategy methods."""

    config: BuildConfig
    workspace: Path
    packages: Path
    platform_detector: PlatformDetector
    platform_info: PlatformInfo
    num_jobs: int
    shell: Optional[str] = None
    cross_environment: Optional["CrossEnvironment"] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())
        object.__setattr__(self, "packages", Path(self.packages).resolve())
        if self.platform_info is None:
            raise ValueError("platform_info is required")
