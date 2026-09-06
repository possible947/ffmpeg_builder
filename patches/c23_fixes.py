"""Compiler and C23 compatibility source patches."""

from __future__ import annotations

from pathlib import Path

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.patches.base import (
    PatchStrategy,
    PatchTarget,
    SourcePatch,
    assert_patch_absent,
    assert_patch_present,
)


def _patch_xvidcore_encoder_h(path: Path, component: PatchTarget, strategy: PatchStrategy) -> None:
    if component.name != "xvidcore":
        return
    content = path.read_text(encoding="utf-8")
    legacy = "typedef int bool;"
    patched = (
        "#if !defined(__STDC_VERSION__) || __STDC_VERSION__ < 202311L\n"
        "typedef int bool;\n"
        "#endif"
    )
    if legacy in content and patched not in content:
        path.write_text(content.replace(legacy, patched, 1), encoding="utf-8")
    final = path.read_text(encoding="utf-8")
    if legacy in final and patched not in final:
        raise BuildError(
            component.name,
            f"Source patch did not take effect in {path}: unguarded '{legacy}' still present "
            "(C23 bool typedef gate). The xvidcore version may have changed.",
        )
    assert_patch_present(
        component.name,
        path,
        "#if !defined(__STDC_VERSION__) || __STDC_VERSION__ < 202311L",
        "C23 bool typedef gate",
    )


def _patch_openssl_configdata(path: Path, component: PatchTarget, strategy: PatchStrategy) -> None:
    content = path.read_text(encoding="utf-8")
    if "-std=c11" in content:
        updated = content.replace("-std=c11", "-std=gnu11")
        path.write_text(updated, encoding="utf-8")
    assert_patch_absent(component.name, path, "-std=c11", "configdata.pm -std=c11 -> -std=gnu11")


XVIDCORE_BOOL_PATCH = SourcePatch(
    name="xvidcore-c23-bool-gate",
    component_name="xvidcore",
    target_rel_path="src/encoder.h",
    apply_fn=_patch_xvidcore_encoder_h,
)

OPENSSL_C11_PATCH = SourcePatch(
    name="openssl-c11-gnu11",
    component_name="openssl",
    target_rel_path="configdata.pm",
    apply_fn=_patch_openssl_configdata,
)

__all__ = [
    "XVIDCORE_BOOL_PATCH",
    "OPENSSL_C11_PATCH",
]
