"""Ninja component builder."""

from pathlib import Path


def build_ninja(builder, component, source_dir: Path) -> None:
    builder.build_ninja(component, source_dir)
