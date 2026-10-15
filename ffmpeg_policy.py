"""FFmpeg version policy evaluation based on platform/toolchain compatibility."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ffmpeg_builder.config import BuildConfig, SUPPORTED_FFMPEG_VERSIONS
from ffmpeg_builder.platform_detect import PlatformInfo, ToolInfo
from ffmpeg_builder.toolchain_registry import (
    ToolchainRegistry,
    _satisfies_range,
    _system_tool_in_range,
    _version_tuple,
)


@dataclass(frozen=True)
class FfmpegPolicy:
    """Policy decision for FFmpeg version selection."""

    available_versions: List[str]
    selected_version_allowed: bool
    blocked_reason: str = ""
    notes: List[str] = field(default_factory=list)


def _macports_clang_17_available(config: BuildConfig) -> bool:
    configured = getattr(config.macos, "clang", "")
    if configured not in ("macports-clang-17", "clang-mp-17"):
        return False
    return bool(shutil.which("clang-mp-17"))


def _detected_versions(
    platform_info: PlatformInfo, tools: Dict[str, ToolInfo]
) -> Dict[str, str]:
    """Map detected compiler names to version strings for range checks."""
    versions: Dict[str, str] = {}
    if platform_info.gcc_major_version is not None:
        versions["gcc"] = str(platform_info.gcc_major_version)
    for name in ("gcc", "clang"):
        info = tools.get(name)
        if info and info.available and info.version:
            match = re.search(r"\d+(?:\.\d+)*", str(info.version))
            if match:
                versions[name] = match.group(0)
    return versions


def _describe_range_failure(
    system_tool_range: Dict[str, str], versions: Dict[str, str], version: str
) -> str:
    """Human-readable reason the system toolchain is outside the allowed range."""
    labels = {"gcc": "GCC", "clang": "Clang"}
    parts: List[str] = []
    for tool, constraint in system_tool_range.items():
        detected = versions.get(tool)
        if detected is None:
            parts.append(f"{labels.get(tool, tool)} не определён (требуется {constraint})")
        elif not _satisfies_range(_version_tuple(detected), constraint):
            parts.append(f"{labels.get(tool, tool)} {detected} вне диапазона {constraint}")
    detail = "; ".join(parts) or "системный инструмент вне допустимого диапазона"
    return f"FFmpeg {version}: {detail}."


def _evaluate_macos_policy(
    config: BuildConfig, available: List[str]
) -> tuple[List[str], List[str]]:
    """macOS keeps its existing macports-clang-17 requirement for FFmpeg 8.1."""
    reasons: List[str] = []
    notes: List[str] = []
    if not _macports_clang_17_available(config):
        reasons.append(
            "На macOS FFmpeg 8.1 разрешен только с выбранным и доступным macports-clang-17."
        )
        notes.append(
            "Решение: установить clang-17 из MacPorts и выбрать macos.clang=macports-clang-17."
        )
        if "8.1" in available:
            available.remove("8.1")
    return reasons, notes


def evaluate_ffmpeg_policy(
    config: BuildConfig,
    platform_info: PlatformInfo,
    tools: Dict[str, ToolInfo],
) -> FfmpegPolicy:
    """Evaluate build policy for the selected FFmpeg version.

    On Linux/Windows the decision is driven by the toolchain registry: a version
    is available when its platform profile's system-tool range is satisfied, or
    when an out-of-range system toolchain can be replaced by a *deployable*
    ``fallback_toolchain``. macOS keeps its macports-clang-17 requirement.
    """
    available = list(SUPPORTED_FFMPEG_VERSIONS)
    notes: List[str] = []
    reasons: List[str] = []

    if platform_info.is_macos:
        reasons, notes = _evaluate_macos_policy(config, available)
    else:
        platform = platform_info.platform or "linux"
        versions = _detected_versions(platform_info, tools)
        try:
            registry: Optional[ToolchainRegistry] = ToolchainRegistry()
        except (OSError, ValueError):
            registry = None
        if registry is None:
            notes.append("Toolchain registry unavailable; FFmpeg policy not evaluated.")
        else:
            for version in list(available):
                profile = registry.resolve_platform_profile(version, platform)
                if profile is None:
                    continue
                if _system_tool_in_range(profile.system_tool_range, versions):
                    continue
                fallback = profile.fallback_toolchain
                fallback_profile = registry.get_profile(fallback) if fallback else None
                if fallback_profile is not None and fallback_profile.deployable:
                    # Out-of-range system toolchain is acceptable: it is deployed.
                    continue
                available.remove(version)
                reasons.append(
                    _describe_range_failure(profile.system_tool_range, versions, version)
                )

    if "8.1" not in available and "9.0" in available:
        notes.append("Рекомендация: использовать FFmpeg 9.0.")

    selected_allowed = config.ffmpeg_version in available
    blocked_reason = " ".join(reasons) if not selected_allowed else ""

    return FfmpegPolicy(
        available_versions=available,
        selected_version_allowed=selected_allowed,
        blocked_reason=blocked_reason,
        notes=notes,
    )
