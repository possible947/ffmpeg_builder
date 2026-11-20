"""soxr cross-build integration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...builders.base import ComponentBuildContext, build_cmake
from ...config import TARGET_BACKEND_CROSS_MINGW

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_soxr(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build soxr and provide pkg-config metadata omitted by its Windows CMake path."""
    build_cmake(ComponentBuildContext.from_builder(builder), component, source_dir)

    cross = builder.platform_context.cross_environment
    if cross is None or builder.config.target_backend != TARGET_BACKEND_CROSS_MINGW:
        return

    prefix = Path(builder._ws_str())
    for name in ("README", "LICENCE", "NEWS"):
        (prefix / "doc" / name).unlink(missing_ok=True)

    pkg_config = prefix / "lib" / "pkgconfig" / "soxr.pc"
    pkg_config.parent.mkdir(parents=True, exist_ok=True)
    pkg_config.write_text(
        "\n".join(
            (
                f"prefix={prefix}",
                "exec_prefix=${prefix}",
                "libdir=${prefix}/lib",
                "includedir=${prefix}/include",
                "",
                "Name: soxr",
                "Description: High quality, one-dimensional sample-rate conversion library",
                f"Version: {component.version}",
                "Libs: -L${libdir} -lsoxr",
                "Libs.private: -fopenmp",
                "Cflags: -I${includedir}",
                "",
            )
        ),
        encoding="utf-8",
    )
