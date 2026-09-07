"""libtiff component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libtiff(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libtiff via autotools.

    libtiff's tools (tiffset.c, tiff2pdf.c, ...) call fseeko/ftello through
    macros in tiffiop.h. Those are POSIX extensions gated behind
    __USE_XOPEN2K/__USE_MISC in glibc's stdio.h; with the project's default
    strict "-std=c11" CFLAGS, glibc leaves them undeclared, breaking the
    build with -Wimplicit-function-declaration errors. Appending
    "-std=gnu11" after "-std=c11" restores glibc's default-source feature
    macros (GCC honors the last -std= flag) without relaxing anything else.
    Scoped to LinuxGcc15Platform: that's the toolchain class where this has
    been observed.
    """
    env = builder.get_build_env(component)
    if type(builder.platform_strategy).__name__ == "LinuxGcc15Platform":
        env["CFLAGS"] = env.get("CFLAGS", "") + " -std=gnu11"

    configure_args = [
        arg.replace("{workspace}", builder._ws_str()) for arg in component.configure_args
    ]

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        ["./configure"] + configure_args,
        "configure",
        source_dir,
        env,
    )

    builder._run_make(
        component,
        ComponentStatus.BUILDING,
        f"make -j{builder.num_jobs}",
        "Build failed",
        source_dir,
        builder.num_jobs,
        env,
    )

    builder._run_install(
        component,
        ComponentStatus.INSTALLING,
        "make install",
        "Install failed",
        source_dir,
        env,
    )
