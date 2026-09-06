"""Darwin-specific source patches."""

from __future__ import annotations

import shutil
from pathlib import Path

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.patches.base import (
    PatchStrategy,
    PatchTarget,
    SourcePatch,
    assert_patch_absent,
    assert_patch_present,
)


def _patch_libjxl_deps(path: Path, component: PatchTarget, strategy: PatchStrategy) -> None:
    if shutil.which("realpath") is not None:
        return
    content = path.read_text(encoding="utf-8")
    original = 'SELF=$(realpath "$0")'
    guard = "command -v realpath"
    if original in content and guard not in content:
        portable = (
            "if command -v realpath >/dev/null 2>&1; then\n"
            '  SELF=$(realpath "$0")\n'
            "else\n"
            '  SELF=$(cd -- "$(dirname -- "$0")" && pwd -P)/$(basename -- "$0")\n'
            "fi"
        )
        path.write_text(content.replace(original, portable, 1), encoding="utf-8")
    final = path.read_text(encoding="utf-8")
    if original in final and guard not in final:
        raise BuildError(
            component.name,
            f"Source patch did not take effect in {path}: unguarded '{original}' still present "
            "(realpath missing on this system). The libjxl version may have changed.",
        )
    assert_patch_present(component.name, path, "command -v realpath", "libjxl realpath guard")


def _patch_libvorbis_configure(path: Path, component: PatchTarget, strategy: PatchStrategy) -> None:
    content = path.read_text(encoding="utf-8")
    marker = "-force_cpusubtype_ALL"
    if marker in content:
        path.write_text(content.replace(marker, ""), encoding="utf-8")
    assert_patch_absent(component.name, path, marker, "libvorbis configure.ac cpusubtype")


LIBJXL_REALPATH_PATCH = SourcePatch(
    name="libjxl-realpath-guard",
    component_name="libjxl",
    target_rel_path="deps.sh",
    apply_fn=_patch_libjxl_deps,
)

LIBVORBIS_CPUSUBTYPE_PATCH = SourcePatch(
    name="libvorbis-cpusubtype-cleanup",
    component_name="libvorbis",
    target_rel_path="configure.ac",
    apply_fn=_patch_libvorbis_configure,
)

__all__ = ["LIBJXL_REALPATH_PATCH", "LIBVORBIS_CPUSUBTYPE_PATCH"]
