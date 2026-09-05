"""libvpx component builder."""

from pathlib import Path


def build_libvpx(builder, component, source_dir: Path) -> None:
    builder.build_libvpx(component, source_dir)
