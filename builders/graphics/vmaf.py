"""libvmaf component builder."""

from pathlib import Path


def build_libvmaf(builder, component, source_dir: Path) -> None:
    builder.build_libvmaf(component, source_dir)
