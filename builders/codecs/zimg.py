"""zimg component builder."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder
    from ...components import Component


def build_zimg(builder: FFmpegBuilder, component: Component, source_dir: Path) -> None:
    """Build zimg."""
    env = builder.get_build_env(component)

    def _resolve_tool_path(tool: str) -> Optional[str]:
        """Resolve executable path across native Windows and MSYS2 paths."""
        raw = shutil.which(tool)
        if raw:
            return raw

        msys_root = Path(builder.config.windows.msys2_root)
        candidates = [
            msys_root / "usr" / "bin" / tool,
            msys_root / "ucrt64" / "bin" / tool,
            msys_root / "usr" / "bin" / f"{tool}.exe",
            msys_root / "ucrt64" / "bin" / f"{tool}.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    # Use workspace GNU libtoolize first. On macOS it is commonly
    # installed as `glibtoolize` (Homebrew/MacPorts naming).
    candidates = [
        str(builder.workspace / "bin" / "libtoolize"),
        str(builder.workspace / "bin" / "glibtoolize"),
        _resolve_tool_path("libtoolize") or "",
        _resolve_tool_path("glibtoolize") or "",
    ]
    libtoolize = next((c for c in candidates if c and Path(c).exists()), None)
    if libtoolize is None:
        # Final fallback for environments where command is resolvable by
        # shell but not by absolute path probing.
        if builder._command_exists("libtoolize"):
            libtoolize = "libtoolize"
        elif builder._command_exists("glibtoolize"):
            libtoolize = "glibtoolize"
    if libtoolize is None:
        raise BuildError(component.name, "libtoolize not found (tried libtoolize and glibtoolize)")

    libtoolize_cmd = [libtoolize, "-i", "-f", "-q"]
    if builder._is_windows_ucrt64_backend():
        # In MSYS2, /usr/bin/libtoolize is a shell script, not a native
        # Win32 executable. Run it through sh.exe.
        suffix = Path(libtoolize).suffix.lower()
        if suffix not in (".exe", ".bat", ".cmd"):
            sh_path = _resolve_tool_path("sh") or "sh"
            libtoolize_cmd = [sh_path, libtoolize, "-i", "-f", "-q"]

    builder._run_step(
        component,
        ComponentStatus.CONFIGURING,
        "libtoolize",
        "Libtoolize failed",
        libtoolize_cmd,
        "libtoolize",
        source_dir,
        env,
    )

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
        ["./configure", f"--prefix={builder._ws_str()}", "--enable-static", "--disable-shared"],
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
