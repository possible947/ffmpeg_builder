"""Ninja component builder."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_ninja(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build ninja build system from source."""
    env = builder.get_build_env(component)

    builder._run_step(
        component,
        ComponentStatus.BUILDING,
        "./configure.py --bootstrap",
        "Bootstrap failed",
        ["./configure.py", "--bootstrap"],
        "bootstrap",
        source_dir,
        env,
    )

    ninja_bin = source_dir / "ninja"
    dest = builder.workspace / "bin" / "ninja"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ninja_bin, dest)
