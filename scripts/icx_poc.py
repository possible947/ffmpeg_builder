#!/usr/bin/env python3
"""PoC: build the configured FFmpeg component set with Intel oneAPI icx/icpx.

This is a throw-away evaluation harness. It does NOT touch the toolchain
registry, the persistent store, or the standard workspaces. It:

* locates an Intel oneAPI install (default ``/opt/intel/oneapi``),
* injects ``CC=icx`` / ``CXX=icpx`` plus the compiler bin dir as the
  per-component ``toolchain_env`` consumed by ``FFmpegBuilder``,
* exports the oneAPI runtime paths (``LD_LIBRARY_PATH``/``LIBRARY_PATH``) so
  ``libiomp5.so`` resolves at build *and* runtime,
* builds into a dedicated workspace (default ``/tmp/kilo/icx_poc/workspace_<ver>``).

Logs land under ``<workspace>/logs/<component>_<step>.log``. On ``BuildError``
the script stops at the failed component; rerun with ``--resume``.

Examples:
    python scripts/icx_poc.py --dry-run
    python scripts/icx_poc.py --ffmpeg-version 9.0
    python scripts/icx_poc.py --components av1,ffmpeg
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))

from ffmpeg_builder.app import FFmpegBuilderApp, PROJECT_ROOT as APP_ROOT  # noqa: E402
from ffmpeg_builder.build_types import BuildError, SkipComponent  # noqa: E402
from ffmpeg_builder.builder import FFmpegBuilder  # noqa: E402
from ffmpeg_builder.components import PlatformOverride  # noqa: E402
from ffmpeg_builder.config import BuildConfig  # noqa: E402
from ffmpeg_builder.state import ComponentStatus  # noqa: E402

assert APP_ROOT == PROJECT_ROOT, "app package root mismatch"

DEFAULT_ONEAPI_ROOT = Path("/opt/intel/oneapi")
DEFAULT_WORKSPACE_ROOT = Path("/tmp/kilo/icx_poc")

# ICX/clang baseline for the PoC: unlike GCC, clang-based compilers treat an
# implicit function declaration as a hard error and reject `register` in
# -std=c++17. `_GNU_SOURCE` re-exposes the POSIX/GNU prototypes (clock_gettime,
# strtok_r, ...) that a strict -std=c11 hides, and -Wno-register demotes the
# C++17 register diagnostic. Applied to every component for this run only.
# ICX/clang baseline for the PoC:
#  * `_GNU_SOURCE` re-exposes the POSIX/GNU prototypes (clock_gettime, strtok_r,
#    ...) that a strict -std=c11 hides; clang-based compilers make an implicit
#    function declaration a hard error, unlike GCC.
#  * `-Wno-register` demotes the C++17 `register` diagnostic (an error under
#    clang/ICX, a warning under GCC).
#  * `-ffp-model=precise` restores GCC-like strict FP semantics: ICX defaults
#    to a fast FP model, which rejects `#pragma STDC FENV_ACCESS` and otherwise
#    changes floating-point results.
#  * `-Wno-recommended-option` silences the "use -qopenmp instead of -fopenmp"
#    driver warning, which -Werror builds (e.g. libzmq) otherwise reject.
ICX_BASELINE_CFLAGS = "-D_GNU_SOURCE -ffp-model=precise -Wno-recommended-option"
ICX_BASELINE_CXXFLAGS = "-Wno-register -D_GNU_SOURCE -ffp-model=precise -Wno-recommended-option"

# Components whose build system links with a separate $(LD) variable that
# libvpx's configure hardcodes to `gcc` in its generated *.mk (not overridable
# via LD env). Mixing icx-compiled objects with a gcc link leaves icx's
# runtime symbols (_intel_fast_memcpy, __svml_*) unresolved, so override LD as
# a make command-line variable through MAKEFLAGS for those components.
ICX_LD_COMPONENTS = {"libvpx"}

# Extra meson/cmake configure args the ICX run needs, keyed by component.
# (libplacebo's glslang lookup is handled by a source patch instead.)
ICX_EXTRA_CONFIGURE_ARGS: Dict[str, List[str]] = {}


def apply_icx_component_flags(components) -> None:
    """Merge the ICX baseline flags into each component's existing overrides."""
    for component in components:
        override = component.platform_overrides.get("linux")
        if override is None:
            override = PlatformOverride()
            component.platform_overrides["linux"] = override
        override.extra_cflags = f"{override.extra_cflags} {ICX_BASELINE_CFLAGS}".strip()
        override.extra_cxxflags = f"{override.extra_cxxflags} {ICX_BASELINE_CXXFLAGS}".strip()
        if component.name in ICX_LD_COMPONENTS and icx_paths.get("icx"):
            component.extra_env["MAKEFLAGS"] = f"LD={icx_paths['icx']}"
        extra_args = ICX_EXTRA_CONFIGURE_ARGS.get(component.name)
        if extra_args:
            component.configure_args.extend(extra_args)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ffmpeg-version", default="9.0", help="FFmpeg version (default 9.0).")
    parser.add_argument(
        "--oneapi-root", default=str(DEFAULT_ONEAPI_ROOT), help="Intel oneAPI install root."
    )
    parser.add_argument(
        "--workspace-root",
        default=str(DEFAULT_WORKSPACE_ROOT),
        help="Root under which the isolated workspace is created.",
    )
    parser.add_argument(
        "--components",
        help="Comma-separated subset of component names to build (default: all buildable).",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from prior build state.")
    parser.add_argument("--dry-run", action="store_true", help="List the plan and exit.")
    parser.add_argument("--no-release", action="store_true", help="Skip the release bundle step.")
    return parser.parse_args(argv)


def resolve_icx(oneapi_root: Path) -> Dict[str, str]:
    """Resolve icx/icpx paths and the oneAPI runtime library directory."""
    compiler = oneapi_root / "compiler" / "latest"
    bindir = compiler / "bin"
    icx = bindir / "icx"
    icpx = bindir / "icpx"
    if not icx.exists() or not icpx.exists():
        raise SystemExit(f"icx/icpx not found under {bindir}")
    # libiomp5.so lives in <compiler>/lib; keep the OpenMP runtime discoverable
    # for both the linker and the runtime of configure test executables.
    lib_dirs = [p for p in (compiler / "lib", compiler / "opt" / "compiler" / "lib") if p.is_dir()]
    return {
        "bindir": str(bindir),
        "icx": str(icx),
        "icpx": str(icpx),
        "lib_dirs": lib_dirs,
    }


def apply_oneapi_runtime_env(icx: Dict[str, str]) -> None:
    """Prepend oneAPI paths to PATH/LD_LIBRARY_PATH/LIBRARY_PATH in os.environ.

    ``CommandExecutor`` merges ``os.environ`` into every subprocess, so setting
    these here makes libiomp5.so resolvable during configure/link and when the
    built ffmpeg runs.
    """
    libs = [str(p) for p in icx["lib_dirs"]]
    os.environ["PATH"] = os.pathsep.join([icx["bindir"], os.environ.get("PATH", "")])
    for key in ("LD_LIBRARY_PATH", "LIBRARY_PATH"):
        existing = os.environ.get(key, "")
        os.environ[key] = os.pathsep.join([*libs, existing] if existing else libs)


def icx_toolchain_env(components) -> Dict[str, Dict[str, str]]:
    """Every selected component builds with icx/icpx (single OpenMP runtime)."""
    env = {}
    for component in components:
        # Mirror CC/CXX into component.extra_env so declarative source patches
        # (patch registry) can gate on the active compiler.
        component.extra_env.setdefault("CC", icx_paths["icx"])
        component.extra_env.setdefault("CXX", icx_paths["icpx"])
        env[component.name] = {
            "PATH_PREPEND": icx_paths["bindir"],
            "CC": icx_paths["icx"],
            "CXX": icx_paths["icpx"],
        }
        component.extra_env.setdefault("CC", icx_paths["icx"])
        component.extra_env.setdefault("CXX", icx_paths["icpx"])
    return env


icx_paths: Dict[str, str] = {}


def main(argv: Optional[List[str]] = None) -> int:
    global icx_paths
    args = parse_args(argv)

    icx_paths = resolve_icx(Path(args.oneapi_root))
    apply_oneapi_runtime_env(icx_paths)
    print(f"icx : {icx_paths['icx']}")
    print(f"icpx: {icx_paths['icpx']}")
    print(f"omp lib dirs: {icx_paths['lib_dirs']}")

    workspace = BuildConfig(ffmpeg_version=args.ffmpeg_version).get_workspace_path(PROJECT_ROOT)
    app = FFmpegBuilderApp(workspace=workspace)
    config = app.config_manager.get()
    config.ffmpeg_version = args.ffmpeg_version
    # Evaluation profile: host-tuned, OpenMP on, GPL stack, no forced static.
    config.native_build = True
    config.openmp = True
    config.full_static = False
    config.make_release = not args.no_release
    app._sync_workspace(config)

    components = app._get_buildable_components(config)
    if args.components:
        wanted = {name.strip() for name in args.components.split(",") if name.strip()}
        components = [c for c in components if c.name in wanted]
        missing = wanted - {c.name for c in components}
        if missing:
            print(f"warning: not buildable/unknown: {', '.join(sorted(missing))}", file=sys.stderr)

    toolchain_env = icx_toolchain_env(components)
    apply_icx_component_flags(components)

    print(f"\nFFmpeg {config.ffmpeg_version}: {len(components)} components, workspace {workspace}")
    for component in components:
        print(f"  {component.name:24s} {component.version:16s} cc=icx")
    if args.dry_run:
        return 0

    if args.resume:
        resume_point = app.state_manager.get_resume_point()
        if resume_point:
            idx = next((i for i, c in enumerate(components) if c.name == resume_point), 0)
            components = components[idx:]
            print(f"Resuming from {resume_point} ({len(components)} remaining)")
    else:
        app.state_manager.reset()

    builder = FFmpegBuilder(
        config,
        app.workspace,
        app.packages,
        app.state_manager,
        app.platform_detector,
        toolchain_env=toolchain_env,
    )
    builder.prefetch_downloads(components)

    state = app.state_manager.get()
    state.config = config.to_dict()
    state.total_steps = len(components)
    app.state_manager.save()

    completed = 0
    for position, component in enumerate(components, 1):
        print(
            f"\n=== [{position}/{len(components)}] {component.name} {component.version} ===",
            flush=True,
        )
        try:
            builder.build_component(component)
            current = app.state_manager.get().components.get(component.name)
            if current is None or current.status != ComponentStatus.SYSTEM:
                app.state_manager.mark_component_status(
                    component.name, ComponentStatus.COMPLETED, component.version
                )
            completed += 1
        except SkipComponent as exc:
            app.state_manager.mark_component_status(
                component.name, ComponentStatus.SKIPPED, component.version, str(exc)
            )
            print(f"SKIPPED: {exc}", flush=True)
        except BuildError as exc:
            app.state_manager.mark_component_status(
                component.name,
                ComponentStatus.FAILED,
                component.version,
                str(exc),
                str(exc.log_file) if exc.log_file else None,
            )
            print(f"FAILED: {exc}", flush=True)
            if exc.log_file:
                print(f"  log: {exc.log_file}", flush=True)
            print(f"\nStopped at {component.name} ({completed} completed).", flush=True)
            return 1

    print(f"\nAll components done: {completed} completed.", flush=True)
    if config.make_release:
        try:
            release_dir = builder.make_release_bundle()
        except BuildError as exc:
            print(f"Release bundle failed: {exc}", flush=True)
            return 1
        print(f"Release bundle: {release_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
