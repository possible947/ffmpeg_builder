"""Meson component builder."""

from pathlib import Path


def build_meson(builder, component, source_dir: Path) -> None:
    builder.build_meson(component, source_dir)
