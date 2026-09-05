"""libzmq component builder."""

from pathlib import Path


def build_libzmq(builder, component, source_dir: Path) -> None:
    builder.build_libzmq(component, source_dir)
