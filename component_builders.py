"""Custom component builder dispatch and helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

if TYPE_CHECKING:
    from .builder import FFmpegBuilder
    from .components import Component


CustomBuilder = Callable[["FFmpegBuilder", "Component", Path], None]


def get_custom_builder(name: str) -> Optional[CustomBuilder]:
    """Return custom builder method by configured name."""
    return CUSTOM_BUILDERS.get(name)


CUSTOM_BUILDERS: Dict[str, CustomBuilder] = {
    "build_meson": lambda builder, component, source_dir: builder.build_meson(component, source_dir),
    "build_openssl": lambda builder, component, source_dir: builder.build_openssl(component, source_dir),
    "build_x264": lambda builder, component, source_dir: builder.build_x264(component, source_dir),
    "build_x265": lambda builder, component, source_dir: builder.build_x265(component, source_dir),
    "build_libvpx": lambda builder, component, source_dir: builder.build_libvpx(component, source_dir),
    "build_zimg": lambda builder, component, source_dir: builder.build_zimg(component, source_dir),
    "build_libvorbis": lambda builder, component, source_dir: builder.build_libvorbis(component, source_dir),
    "build_libjxl": lambda builder, component, source_dir: builder.build_libjxl(component, source_dir),
    "build_libvmaf": lambda builder, component, source_dir: builder.build_libvmaf(component, source_dir),
    "build_srt": lambda builder, component, source_dir: builder.build_srt(component, source_dir),
    "build_libzmq": lambda builder, component, source_dir: builder.build_libzmq(component, source_dir),
    "build_glslang": lambda builder, component, source_dir: builder.build_glslang(component, source_dir),
    "build_libplacebo": lambda builder, component, source_dir: builder.build_libplacebo(component, source_dir),
    "build_ninja": lambda builder, component, source_dir: builder.build_ninja(component, source_dir),
    "build_ffmpeg": lambda builder, component, source_dir: builder.build_ffmpeg(component, source_dir),
}
