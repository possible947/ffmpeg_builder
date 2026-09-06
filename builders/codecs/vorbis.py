"""libvorbis component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libvorbis(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libvorbis."""
    env = builder.get_build_env(component)

    # The -force_cpusubtype_ALL cleanup is applied by the patch registry
    # (patches/darwin_patches.py) after extraction.
    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./autogen.sh",
        "Autogen failed",
        ["./autogen.sh", f"--prefix={builder._ws_str()}"],
        "autogen",
        source_dir,
        env,
    )

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        [
            "./configure",
            f"--prefix={builder._ws_str()}",
            f"--with-ogg-libraries={builder._ws_str()}/lib",
            f"--with-ogg-includes={builder._ws_str()}/include/",
            "--enable-static",
            "--disable-shared",
            "--disable-oggtest",
        ],
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
