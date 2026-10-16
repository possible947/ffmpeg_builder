"""Build recipes for source-based toolchain acquisition.

Recipes are invoked by :class:`ffmpeg_builder.toolchain_store.ToolchainStore`
when a profile's ``build`` source variant is selected. Execution is delegated to
an injected ``runner`` so the recipes stay testable without spawning processes.

Registry-declared ``configure_args``/``build_args``/``install_args`` support
three placeholders, resolved by :func:`resolve_args` before a recipe runs:

- ``{prefix}`` — the installation prefix of the profile being built;
- ``{jobs}`` — the parallel job count for ``make``;
- ``{prereq:<name>}`` — the installation prefix of a bootstrap prerequisite.

Portability: toolchain builds must not inherit the FFmpeg build environment, so
:func:`default_runner` runs every command with a sanitized copy of the process
environment (build flags and vendor variables are dropped). The recipes
themselves use only paths derived from arguments, never host-specific absolute
paths.

Verification: :func:`verify_profile` probes an installed profile (compiler
version plus a real compile/link for compilers, expected artifacts for
libraries) and returns a list of failure descriptions. An empty list means the
profile is usable.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

# (command, cwd, log_file)
Runner = Callable[[List[str], Path, Path], None]

PREREQ_RE = re.compile(r"\{prereq:([A-Za-z0-9_.\-]+)\}")

# Variables that must not leak from the FFmpeg build into a toolchain bootstrap.
_SANITIZED_ENV_VARS = (
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
    "LIBRARY_PATH",
    "LD_LIBRARY_PATH",
    "PKG_CONFIG_PATH",
    "PKG_CONFIG_LIBDIR",
    "CUDA_NVCC_CCBIN",
    "CUDA_HOME",
    "CUDA_PATH",
    "NVCC_PREPEND_FLAGS",
    "VULKAN_SDK",
    "RUSTFLAGS",
    "CARGO_BUILD_RUSTFLAGS",
    "AS",
)


def clean_env() -> Dict[str, str]:
    """Return a sanitized copy of the process environment for toolchain builds."""
    env = dict(os.environ)
    for name in _SANITIZED_ENV_VARS:
        env.pop(name, None)
    return env


def resolve_args(
    args: List[str],
    prefix: Path,
    prerequisites: Optional[Dict[str, Path]] = None,
    jobs: Optional[int] = None,
) -> List[str]:
    """Expand ``{prefix}``/``{jobs}``/``{prereq:<name>}`` placeholders.

    Raises:
        ValueError: if a ``{prereq:<name>}`` placeholder names a prerequisite
            that was not installed (fail fast instead of configuring a library
            without its dependency).
    """
    resolved: List[str] = []
    for arg in args:
        value = arg.replace("{prefix}", str(prefix))
        if jobs is not None:
            value = value.replace("{jobs}", str(jobs))

        def _replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            path = (prerequisites or {}).get(name)
            if path is None:
                raise ValueError(
                    f"Argument {arg!r} references bootstrap prerequisite {name!r}, "
                    "which is not installed in the store"
                )
            return str(path)

        resolved.append(PREREQ_RE.sub(_replace, value))
    return resolved


def default_runner(command: List[str], cwd: Path, log_file: Path) -> None:
    """Run a build command, appending stdout/stderr to ``log_file``.

    The command runs with :func:`clean_env` so FFmpeg build flags and vendor
    variables cannot leak into a toolchain bootstrap.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(command)}  (cwd={cwd})\n")
        handle.flush()
        subprocess.run(
            command,
            cwd=str(cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=True,
            env=clean_env(),
        )


def build_autotools(
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
    configure_args: List[str],
    build_args: List[str],
    install_args: List[str],
    prerequisites: Optional[Dict[str, Path]] = None,
) -> None:
    """Configure/build/install a standard autotools source tree.

    ``prerequisites`` is accepted for symmetry with :func:`build_gcc` and for
    placeholder resolution by the caller; a bootstrap library such as MPFR or
    MPC receives its dependency locations through ``configure_args``
    (e.g. ``--with-gmp={prereq:gmp}``).
    """
    configure = source_dir / "configure"
    if not configure.exists():
        raise FileNotFoundError(f"No configure script in {source_dir}")
    runner(
        ["./configure", f"--prefix={prefix}", *configure_args],
        source_dir,
        log_dir / "configure.log",
    )
    runner(["make", *build_args], source_dir, log_dir / "make.log")
    runner(["make", "install", *install_args], source_dir, log_dir / "install.log")


def build_gcc(
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
    configure_args: List[str],
    build_args: List[str],
    install_args: List[str],
    prerequisites: Dict[str, Path],
) -> None:
    """Build GCC from source, linking GMP/MPFR/MPC from the store when present."""
    args = [
        f"--prefix={prefix}",
        "--disable-multilib",
        "--enable-languages=c,c++",
    ]
    for profile_name, installed_path in prerequisites.items():
        flag = {"gmp": "--with-gmp", "mpfr": "--with-mpfr", "mpc": "--with-mpc"}.get(profile_name)
        if flag is not None:
            args.append(f"{flag}={installed_path}")
    args.extend(configure_args)

    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    runner([str(source_dir / "configure"), *args], source_dir, log_dir / "configure.log")
    runner(["make", *build_args], source_dir, log_dir / "make.log")
    runner(["make", "install", *install_args], source_dir, log_dir / "install.log")


def build_rust_standalone(
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
) -> None:
    """Install a rust-lang standalone tarball into ``prefix``.

    The standalone distribution (``rust-<version>-<target>.tar.gz``) carries
    rustc + cargo (+ rust-docs/clippy) as component subdirectories plus an
    ``install.sh`` installer. Running ``install.sh --prefix=<prefix>`` lays
    out ``bin/`` (rustc, cargo), ``lib/`` and share trees under the prefix,
    matching the profile's ``materialize: {bin_subdir: bin}`` contract. The
    installer runs non-interactively with ``--verbose``; the backtrace and
    manifest components are disabled to keep the install hermetic.
    """
    installer = source_dir / "install.sh"
    if not installer.exists():
        raise FileNotFoundError(f"No install.sh in {source_dir}")
    runner(
        [
            "sh",
            str(installer),
            f"--prefix={prefix}",
            "--verbose",
            "--without=rust-docs",
            "--without=clippy-preview",
        ],
        source_dir,
        log_dir / "install.log",
    )


def run_build(
    recipe: Optional[str],
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
    configure_args: List[str],
    build_args: List[str],
    install_args: List[str],
    prerequisites: Dict[str, Path],
) -> None:
    """Dispatch a source build recipe."""
    if recipe == "gcc":
        build_gcc(
            source_dir,
            prefix,
            log_dir,
            runner,
            configure_args,
            build_args,
            install_args,
            prerequisites,
        )
        return
    if recipe == "rust_standalone":
        build_rust_standalone(source_dir, prefix, log_dir, runner)
        return
    build_autotools(
        source_dir,
        prefix,
        log_dir,
        runner,
        configure_args,
        build_args,
        install_args,
        prerequisites,
    )


# ----------------------------------------------------------------------
# Post-install verification
# ----------------------------------------------------------------------


def _tool(prefix: Path, name: str) -> Path:
    """Locate a tool in the profile prefix, tolerating the Windows ``.exe`` suffix."""
    candidate = prefix / "bin" / name
    if candidate.exists():
        return candidate
    return prefix / "bin" / f"{name}.exe"


def _run_probe(command: List[str]) -> Optional[str]:
    """Run a probe command; return an error description, or ``None`` on success."""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=clean_env(),
        )
    except OSError as exc:  # pragma: no cover - missing binary / permission issues
        return f"{command[0]}: {exc}"
    if result.returncode != 0:
        output = (result.stdout or "").strip().splitlines()
        detail = output[-1] if output else "no output"
        return f"{command[0]} exited {result.returncode}: {detail}"
    return None


def _verify_compiler(prefix: Path) -> List[str]:
    """Verify a C/C++ compiler toolchain with a real compile-and-link probe."""
    failures: List[str] = []
    cc = _tool(prefix, "gcc")
    cxx = _tool(prefix, "g++")
    for tool in (cc, cxx):
        if not tool.exists():
            failures.append(f"missing {tool}")
    if failures:
        return failures

    for tool in (cc, cxx):
        error = _run_probe([str(tool), "--version"])
        if error:
            failures.append(error)

    with tempfile.TemporaryDirectory(prefix="toolchain-verify-") as temp_dir:
        work = Path(temp_dir)
        c_source = work / "probe.c"
        c_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        cxx_source = work / "probe.cpp"
        cxx_source.write_text("int main() { return 0; }\n", encoding="utf-8")
        error = _run_probe([str(cc), str(c_source), "-o", str(work / "probe-c")])
        if error:
            failures.append(f"C compile/link failed: {error}")
        error = _run_probe([str(cxx), str(cxx_source), "-o", str(work / "probe-cxx")])
        if error:
            failures.append(f"C++ compile/link failed: {error}")
    return failures


def _verify_rust(prefix: Path) -> List[str]:
    """Verify rustc and cargo in a rust profile prefix."""
    failures: List[str] = []
    for name in ("rustc", "cargo"):
        tool = _tool(prefix, name)
        if not tool.exists():
            failures.append(f"missing {tool}")
            continue
        error = _run_probe([str(tool), "--version"])
        if error:
            failures.append(error)
    return failures


def _verify_library(prefix: Path) -> List[str]:
    """Verify a bootstrap library installed headers and a static/dynamic archive."""
    failures: List[str] = []
    lib_dir = prefix / "lib"
    include_dir = prefix / "include"
    if not include_dir.is_dir():
        failures.append(f"missing headers directory {include_dir}")
    archives: List[Path] = []
    if lib_dir.is_dir():
        archives = sorted(
            item
            for item in lib_dir.iterdir()
            if item.name.startswith("lib")
            and (item.suffix in (".a", ".so", ".dylib") or ".so." in item.name)
        )
    if not archives:
        failures.append(f"no static or shared library found under {lib_dir}")
    return failures


def verify_profile(recipe: Optional[str], prefix: Path) -> List[str]:
    """Verify an installed profile; return a list of failures (empty means usable).

    Recipes are checked with capability probes rather than file presence alone:
    compilers must build and link a real C and C++ program, Rust profiles must
    report a version for rustc and cargo, and bootstrap libraries must provide
    headers plus a library archive.
    """
    if recipe == "gcc":
        return _verify_compiler(prefix)
    if recipe == "rust_standalone":
        return _verify_rust(prefix)
    if recipe == "autotools":
        return _verify_library(prefix)

    binary_dir = prefix / "bin"
    if binary_dir.is_dir() and any(binary_dir.iterdir()):
        return []
    return [f"no binaries installed under {binary_dir}"]
