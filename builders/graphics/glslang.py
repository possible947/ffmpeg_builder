"""glslang component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_glslang(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    builder.build_glslang(component, source_dir)
