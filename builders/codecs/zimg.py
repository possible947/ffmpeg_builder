"""zimg component builder."""

from pathlib import Path


def build_zimg(builder, component, source_dir: Path) -> None:
    builder.build_zimg(component, source_dir)
