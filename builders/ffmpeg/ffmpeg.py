"""FFmpeg target component builder."""

from pathlib import Path


def build_ffmpeg(builder, component, source_dir: Path) -> None:
    builder.build_ffmpeg(component, source_dir)
