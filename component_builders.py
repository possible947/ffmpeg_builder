"""Custom component builder dispatch and helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

from .builders.codecs.jxl import build_libjxl
from .builders.codecs.tiff import build_libtiff
from .builders.codecs.vorbis import build_libvorbis
from .builders.codecs.vpx import build_libvpx
from .builders.codecs.x264 import build_x264
from .builders.codecs.x265 import build_x265
from .builders.codecs.zimg import build_zimg
from .builders.crypto.openssl import build_openssl
from .builders.ffmpeg.ffmpeg import build_ffmpeg
from .builders.graphics.glslang import build_glslang
from .builders.graphics.placebo import build_libplacebo
from .builders.graphics.vmaf import build_libvmaf
from .builders.network.srt import build_srt
from .builders.network.zmq import build_libzmq
from .builders.tools.meson import build_meson
from .builders.tools.ninja import build_ninja

if TYPE_CHECKING:
    from .builder import FFmpegBuilder
    from .components import Component


CustomBuilder = Callable[["FFmpegBuilder", "Component", Path], None]


def get_custom_builder(name: str) -> Optional[CustomBuilder]:
    """Return custom builder method by configured name."""
    return CUSTOM_BUILDERS.get(name)


CUSTOM_BUILDERS: Dict[str, CustomBuilder] = {
    "build_meson": build_meson,
    "build_openssl": build_openssl,
    "build_x264": build_x264,
    "build_x265": build_x265,
    "build_libvpx": build_libvpx,
    "build_zimg": build_zimg,
    "build_libvorbis": build_libvorbis,
    "build_libjxl": build_libjxl,
    "build_libtiff": build_libtiff,
    "build_libvmaf": build_libvmaf,
    "build_srt": build_srt,
    "build_libzmq": build_libzmq,
    "build_glslang": build_glslang,
    "build_libplacebo": build_libplacebo,
    "build_ninja": build_ninja,
    "build_ffmpeg": build_ffmpeg,
}
