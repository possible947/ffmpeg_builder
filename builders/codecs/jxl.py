"""libjxl component builder."""

from pathlib import Path


def build_libjxl(builder, component, source_dir: Path) -> None:
    builder.build_libjxl(component, source_dir)
