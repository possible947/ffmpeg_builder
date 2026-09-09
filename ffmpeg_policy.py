"""FFmpeg version policy evaluation based on platform/toolchain compatibility."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ffmpeg_builder.config import BuildConfig, SUPPORTED_FFMPEG_VERSIONS
from ffmpeg_builder.platform_detect import PlatformInfo, ToolInfo


@dataclass(frozen=True)
class FfmpegPolicy:
    """Policy decision for FFmpeg version selection."""

    available_versions: List[str]
    selected_version_allowed: bool
    blocked_reason: str = ""
    notes: List[str] = field(default_factory=list)


def _major_from_version(version: Optional[str]) -> Optional[int]:
    if not version:
        return None
    match = re.search(r"(\d+)", version)
    if not match:
        return None
    return int(match.group(1))


def _tool_major(tools: Dict[str, ToolInfo], *names: str) -> Optional[int]:
    for name in names:
        info = tools.get(name)
        if info and info.available:
            major = _major_from_version(info.version)
            if major is not None:
                return major
    return None


def _macports_clang_17_available(config: BuildConfig) -> bool:
    configured = getattr(config.macos, "clang", "")
    if configured not in ("macports-clang-17", "clang-mp-17"):
        return False
    return bool(shutil.which("clang-mp-17") or shutil.which("macports-clang-17"))


def evaluate_ffmpeg_policy(
    config: BuildConfig,
    platform_info: PlatformInfo,
    tools: Dict[str, ToolInfo],
) -> FfmpegPolicy:
    """Evaluate build policy for the selected FFmpeg version."""
    available = list(SUPPORTED_FFMPEG_VERSIONS)
    notes: List[str] = []
    reasons: List[str] = []

    gcc_major = platform_info.gcc_major_version
    if gcc_major is None:
        gcc_major = _tool_major(tools, "gcc", "g++")
    clang_major = _tool_major(tools, "clang", "clang++")

    block_ffmpeg_81 = False

    if platform_info.is_macos:
        if not _macports_clang_17_available(config):
            block_ffmpeg_81 = True
            reasons.append(
                "На macOS FFmpeg 8.1 разрешен только с выбранным и доступным macports-clang-17."
            )
            notes.append(
                "Решение: установить clang-17 из MacPorts и выбрать macos.clang=macports-clang-17."
            )
    else:
        if gcc_major is not None and gcc_major > 13:
            block_ffmpeg_81 = True
            reasons.append(
                f"Системный GCC {gcc_major} выше поддерживаемого порога для FFmpeg 8.1 (<=13)."
            )
        if clang_major is not None and clang_major > 25:
            block_ffmpeg_81 = True
            reasons.append(
                f"Системный Clang {clang_major} выше поддерживаемого порога для FFmpeg 8.1 (<=25)."
            )

    if platform_info.is_linux and gcc_major is not None and gcc_major >= 15:
        block_ffmpeg_81 = True
        reasons.append("Для Linux GCC15+ по политике разрешена только сборка FFmpeg 9.0.")

    if block_ffmpeg_81 and "8.1" in available:
        available.remove("8.1")
        notes.append("Рекомендация: использовать FFmpeg 9.0.")

    selected_allowed = config.ffmpeg_version in available
    blocked_reason = " ".join(reasons) if not selected_allowed else ""

    return FfmpegPolicy(
        available_versions=available,
        selected_version_allowed=selected_allowed,
        blocked_reason=blocked_reason,
        notes=notes,
    )
