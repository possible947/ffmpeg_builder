"""glslang component builder."""

from pathlib import Path


def build_glslang(builder, component, source_dir: Path) -> None:
    builder.build_glslang(component, source_dir)
