"""libplacebo component builder."""

from pathlib import Path


def build_libplacebo(builder, component, source_dir: Path) -> None:
    builder.build_libplacebo(component, source_dir)
