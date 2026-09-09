"""Patch registry used to apply component-specific source adjustments."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .base import PatchStrategy, PatchTarget, SourcePatch
from .c23_fixes import (
    GETTEXT_RUNTIME_WCHAR_PATCH,
    GETTEXT_RUNTIME_INTL_WCHAR_PATCH,
    GETTEXT_RUNTIME_INTL_SEARCH_PATCH,
    GETTEXT_LIBASPRINTF_WCHAR_PATCH,
    GETTEXT_TOOLS_WCHAR_PATCH,
    GETTEXT_LIBGETTEXTPO_WCHAR_PATCH,
    GETTEXT_LIBGREP_WCHAR_PATCH,
    LIBTEXTSTYLE_WCHAR_PATCH,
    GETTEXT_RUNTIME_STDLIB_PATCH,
    GETTEXT_RUNTIME_INTL_STDLIB_PATCH,
    GETTEXT_RUNTIME_LIBASPRINTF_STDLIB_PATCH,
    GETTEXT_TOOLS_STDLIB_PATCH,
    GETTEXT_LIBGETTEXTPO_STDLIB_PATCH,
    GETTEXT_LIBGREP_STDLIB_PATCH,
    LIBTEXTSTYLE_STDLIB_PATCH,
)
from .cxx_headers import X265_JSON11_PATCH
from .darwin_patches import LIBJXL_REALPATH_PATCH, LIBVORBIS_CPUSUBTYPE_PATCH
from .ffmpeg_patches import FFMPEG_9_VULKAN_PATCH


class PatchRegistry:
    """Collect declarative source patches and apply matching entries for a component."""

    def __init__(self) -> None:
        self._patches: List[SourcePatch] = []
        for patch in (
            GETTEXT_RUNTIME_WCHAR_PATCH,
            GETTEXT_RUNTIME_INTL_WCHAR_PATCH,
            GETTEXT_RUNTIME_INTL_SEARCH_PATCH,
            GETTEXT_LIBASPRINTF_WCHAR_PATCH,
            GETTEXT_TOOLS_WCHAR_PATCH,
            GETTEXT_LIBGETTEXTPO_WCHAR_PATCH,
            GETTEXT_LIBGREP_WCHAR_PATCH,
            LIBTEXTSTYLE_WCHAR_PATCH,
            GETTEXT_RUNTIME_STDLIB_PATCH,
            GETTEXT_RUNTIME_INTL_STDLIB_PATCH,
            GETTEXT_RUNTIME_LIBASPRINTF_STDLIB_PATCH,
            GETTEXT_TOOLS_STDLIB_PATCH,
            GETTEXT_LIBGETTEXTPO_STDLIB_PATCH,
            GETTEXT_LIBGREP_STDLIB_PATCH,
            LIBTEXTSTYLE_STDLIB_PATCH,
            X265_JSON11_PATCH,
            LIBJXL_REALPATH_PATCH,
            LIBVORBIS_CPUSUBTYPE_PATCH,
            FFMPEG_9_VULKAN_PATCH,
        ):
            self.register(patch)

    def register(self, patch: SourcePatch) -> None:
        self._patches.append(patch)

    def apply_patches(
        self, component: PatchTarget, source_dir: Path, strategy: Optional[PatchStrategy]
    ) -> None:
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
