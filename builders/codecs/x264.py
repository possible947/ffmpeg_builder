"""x264 component builder."""

from pathlib import Path

from ..base import ComponentBuildContext


def build_x264(builder, component, source_dir: Path) -> None:
    builder.build_x264(component, source_dir)
