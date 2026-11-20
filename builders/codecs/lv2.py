"""Static pkg-config metadata fixes for the LV2 target dependency chain."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...builders.base import ComponentBuildContext, build_meson
from ...config import TARGET_BACKEND_CROSS_MINGW

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_lv2_target(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Omit LV2's unconditional native validator utility from Windows installs."""
    if builder.config.target_backend == TARGET_BACKEND_CROSS_MINGW:
        meson_file = source_dir / "meson.build"
        try:
            content = meson_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise BuildError(
                component.name, f"Cannot read LV2 Meson project {meson_file}: {exc}"
            ) from exc
        utility_subdir = "subdir('util')"
        if content.count(utility_subdir) != 1:
            raise BuildError(
                component.name,
                "Pinned LV2 source no longer has the expected single unconditional "
                "util subdirectory; refusing to build its non-required validator utility.",
            )
        try:
            meson_file.write_text(content.replace(utility_subdir, "", 1), encoding="utf-8")
        except OSError as exc:
            raise BuildError(component.name, f"Cannot adapt LV2 Meson project: {exc}") from exc
    build_meson(ComponentBuildContext.from_builder(builder), component, source_dir)
