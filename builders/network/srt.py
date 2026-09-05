"""SRT component builder."""

from pathlib import Path


def build_srt(builder, component, source_dir: Path) -> None:
    builder.build_srt(component, source_dir)
