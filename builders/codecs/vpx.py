"""libvpx component builder."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_libvpx(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build libvpx."""
    env = builder.get_build_env(component)

    if builder.platform == "darwin":
        makefile = source_dir / "build" / "make" / "Makefile"
        if makefile.exists():
            content = makefile.read_text()
            content = content.replace(",--version-script", "")
            content = content.replace(
                "-Wl,--no-undefined -Wl,-soname", "-Wl,-undefined,error -Wl,-install_name"
            )
            makefile.write_text(content)
            builder._assert_patch_absent(
                component, makefile, ",--version-script", "libvpx darwin Makefile link flags"
            )
            builder._assert_patch_absent(
                component,
                makefile,
                "-Wl,--no-undefined -Wl,-soname",
                "libvpx darwin Makefile link flags",
            )

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "./configure",
        "Configure failed",
        [
            "./configure",
            f"--prefix={builder._ws_str()}",
            "--disable-unit-tests",
            "--disable-shared",
            "--disable-examples",
            "--as=yasm",
            "--enable-vp9-highbitdepth",
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
