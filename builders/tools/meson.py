"""Meson component builder."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_meson(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Install meson from source when the system package is unavailable."""
    env = builder.get_build_env(component)
    python_bin = shutil.which("python3", path=env.get("PATH")) or shutil.which(
        "python", path=env.get("PATH")
    )
    if python_bin is None:
        raise BuildError(component.name, "Python interpreter is required to build meson")

    setup_py = source_dir / "setup.py"
    if setup_py.exists():
        builder._run_step(
            component,
            ComponentStatus.INSTALLING,
            "python setup.py install",
            "Meson install failed",
            [python_bin, "setup.py", "install", f"--prefix={builder._ws_str()}"],
            "install",
            source_dir,
            env,
        )
        return

    meson_py = source_dir / "meson.py"
    if meson_py.exists():
        builder._run_step(
            component,
            ComponentStatus.CONFIGURING,
            "python meson.py setup build",
            "Meson bootstrap configure failed",
            [python_bin, "meson.py", "setup", "build", f"--prefix={builder._ws_str()}"],
            "configure",
            source_dir,
            env,
        )
        builder._run_step(
            component,
            ComponentStatus.INSTALLING,
            "python meson.py install -C build",
            "Meson bootstrap install failed",
            [python_bin, "meson.py", "install", "-C", "build"],
            "install",
            source_dir,
            env,
        )
        return

    raise BuildError(
        component.name,
        "Meson source archive does not contain setup.py or meson.py bootstrap entrypoint",
    )
