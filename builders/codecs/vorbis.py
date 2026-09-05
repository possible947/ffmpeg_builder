"""libvorbis component builder."""

from pathlib import Path


def build_libvorbis(builder, component, source_dir: Path) -> None:
    builder.build_libvorbis(component, source_dir)
