"""x265 component builder."""

from pathlib import Path


def build_x265(builder, component, source_dir: Path) -> None:
    builder.build_x265(component, source_dir)
