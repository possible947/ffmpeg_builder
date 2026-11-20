"""zimg component builder."""

from __future__ import annotations

import shutil
import re
import shlex
import subprocess
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
    cross = builder.platform_context.cross_environment
    if cross is not None and component.build_role.value == "target":
        env.update(cross.autotools.environment)
        env["STL_LIBS"] = "-lc++ -lunwind"
        api_source = source_dir / "src/zimg/api/zimg.cpp"
        try:
            text = api_source.read_text(encoding="utf-8")
            if "#include <exception>\n" not in text:
                if text.count("#include <cmath>\n") != 1:
                    raise BuildError(component.name, "Unexpected zimg API source layout.")
                api_source.write_text(
                    text.replace(
                        "#include <cmath>\n", "#include <cmath>\n#include <exception>\n", 1
                    ),
                    encoding="utf-8",
                )
        except OSError as exc:
            raise BuildError(component.name, f"Cannot prepare zimg API source: {exc}") from exc
        if not (source_dir / "configure").is_file():
            builder._run_step(
                component,
                ComponentStatus.CONFIGURING,
                "autoreconf",
                "Autotools bootstrap failed",
                ["autoreconf", "--verbose", "--install", "--force"],
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
                *cross.autotools.arguments,
                f"--prefix={builder._ws_str()}",
                "--enable-static",
                "--disable-shared",
                "--disable-testapp",
                "--disable-example",
                "--disable-unit-test",
            ],
            "configure",
            source_dir,
            env,
        )
        builder._run_make(
            component,
            ComponentStatus.BUILDING,
            "make",
            "Build failed",
            source_dir,
            builder.num_jobs,
            env,
        )
        builder._run_step(
            component,
            ComponentStatus.INSTALLING,
            "install library and headers",
            "Install failed",
            ["make", "install-libLTLIBRARIES", "install-includeHEADERS", "install-pkgconfigDATA"],
            "install",
            source_dir,
            env,
        )
        libtool_file = builder.target_prefix / "lib/libzimg.la"
        try:
            symbols = subprocess.run(
                [str(cross.target_tools.nm), "-u", str(builder.target_prefix / "lib/libzimg.a")],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if symbols.returncode:
                raise BuildError(component.name, "Cannot inspect zimg runtime references.")
            text = libtool_file.read_text(encoding="utf-8")
            needle = "dependency_libs='"
            if text.count(needle) != 1:
                raise BuildError(component.name, "Unexpected zimg libtool metadata.")
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if line.startswith("inherited_linker_flags='") and line.endswith("'"):
                    flags = shlex.split(line.partition("=")[2].strip("'"))
                    if not re.search(r"pthread", symbols.stdout, re.IGNORECASE):
                        flags = [flag for flag in flags if flag not in ("-pthread", "-lpthread")]
                    if not re.search(r"__kmpc|GOMP_", symbols.stdout):
                        flags = [flag for flag in flags if flag != "-fopenmp"]
                    lines[index] = "inherited_linker_flags='" + " ".join(flags) + "'"
                    break
            else:
                raise BuildError(component.name, "Unexpected zimg inherited linker flags.")
            text = "\n".join(lines) + "\n"
            libtool_file.write_text(
                text.replace(needle, needle + " -lc++ -lunwind", 1), encoding="utf-8"
            )
        except OSError as exc:
            raise BuildError(component.name, f"Cannot update zimg libtool metadata: {exc}") from exc
        return

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
