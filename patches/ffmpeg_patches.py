"""FFmpeg version-specific source patches."""

from __future__ import annotations

from pathlib import Path

from ffmpeg_builder.patches.base import (
    PatchStrategy,
    PatchTarget,
    SourcePatch,
    assert_patch_present,
)


def _patch_ffmpeg_9_vulkan_renderer(
    path: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    include = '#include "libavutil/hwcontext_vulkan.h"'
    anchor = '#include "libavutil/internal.h"'
    content = path.read_text(encoding="utf-8")
    if include not in content and anchor in content:
        path.write_text(content.replace(anchor, f"{anchor}\n{include}", 1), encoding="utf-8")
    assert_patch_present(
        component.name, path, include, "FFmpeg 9 ffplay Vulkan context declarations"
    )


FFMPEG_9_VULKAN_PATCH = SourcePatch(
    name="ffmpeg-9-vulkan-renderer-context",
    component_name="ffmpeg",
    target_rel_path="fftools/ffplay_renderer.c",
    apply_fn=_patch_ffmpeg_9_vulkan_renderer,
    condition=lambda component, strategy: getattr(component, "version", None) == "9.0",
)

__all__ = ["FFMPEG_9_VULKAN_PATCH"]
