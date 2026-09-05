"""Patch registry used to apply component-specific source adjustments."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .base import SourcePatch
from .c23_fixes import OPENSSL_C11_PATCH, XVIDCORE_BOOL_PATCH
from .cxx_headers import X265_JSON11_PATCH
from .darwin_patches import LIBJXL_REALPATH_PATCH, LIBVORBIS_CPUSUBTYPE_PATCH
from .ffmpeg_patches import FFMPEG_9_VULKAN_PATCH


class PatchRegistry:
    """Collect declarative source patches and apply matching entries for a component."""

    def __init__(self) -> None:
        self._patches: List[SourcePatch] = []
        for patch in (
            XVIDCORE_BOOL_PATCH,
            OPENSSL_C11_PATCH,
            X265_JSON11_PATCH,
            LIBJXL_REALPATH_PATCH,
            LIBVORBIS_CPUSUBTYPE_PATCH,
            FFMPEG_9_VULKAN_PATCH,
        ):
            self.register(patch)

    def register(self, patch: SourcePatch) -> None:
        self._patches.append(patch)

    def apply_patches(self, component: object, source_dir: Path, strategy: Optional[object]) -> None:
        """Apply every patch whose component and condition match this build."""
        for patch in self._patches:
            if patch.component_name != getattr(component, "name", None):
                continue
            if patch.condition is not None and not patch.condition(component, strategy):
                continue
            target = source_dir / patch.target_rel_path
            if not target.exists():
                continue
            patch.apply_fn(target, component, strategy)


_global_registry = PatchRegistry()


def get_patch_registry() -> PatchRegistry:
    return _global_registry
