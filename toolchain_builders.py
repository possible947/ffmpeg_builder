"""Build recipes for source-based toolchain acquisition.

Recipes are invoked by :class:`ffmpeg_builder.toolchain_store.ToolchainStore`
when a profile's ``build`` source variant is selected. Execution is delegated to
an injected ``runner`` so the recipes stay testable without spawning processes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

# (command, cwd, log_file)
Runner = Callable[[List[str], Path, Path], None]


def default_runner(command: List[str], cwd: Path, log_file: Path) -> None:
    """Run a build command, appending stdout/stderr to ``log_file``."""
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
        )


def build_autotools(
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
    configure_args: List[str],
    build_args: List[str],
    install_args: List[str],
) -> None:
    """Configure/build/install a standard autotools source tree."""
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
    build_autotools(source_dir, prefix, log_dir, runner, configure_args, build_args, install_args)
