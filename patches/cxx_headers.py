"""C++ standard library compatibility patches."""

from __future__ import annotations

from pathlib import Path

from ffmpeg_builder.patches.base import (
    PatchStrategy,
    PatchTarget,
    SourcePatch,
    assert_patch_present,
)


def _patch_x265_json11(path: Path, component: PatchTarget, strategy: PatchStrategy) -> None:
    content = path.read_text(encoding="utf-8")
    if "#include <cstdint>" not in content:
        lines = content.split("\n")
        insert_idx = None
        for idx, line in enumerate(lines):
            if line.strip() == "#include <limits>":
                insert_idx = idx + 1
                break
        if insert_idx is not None:
            lines.insert(insert_idx, "#include <cstdint>")
            path.write_text("\n".join(lines), encoding="utf-8")
    assert_patch_present(component.name, path, "#include <cstdint>", "json11.cpp cstdint include")


X265_JSON11_PATCH = SourcePatch(
    name="x265-json11-cstdint",
    component_name="x265",
    target_rel_path="source/dynamicHDR10/json11/json11.cpp",
    apply_fn=_patch_x265_json11,
)

__all__ = ["X265_JSON11_PATCH"]
