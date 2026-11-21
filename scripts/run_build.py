#!/usr/bin/env python3
"""Run a full FFmpeg build headlessly (non-interactive twin of the TUI build).

Uses the same pipeline as `FFmpegBuilderApp._run_build`: buildable-component
selection (including the toolchain gate), per-component toolchain environment,
state tracking with resume support, and the release bundle step.

On `BuildError` the script stops at the failed component (like the TUI's
"abort" action) and exits non-zero; rerun with `--resume` after fixing the
cause. Logs stay under `<workspace>/logs/<component>_<step>.log`.

Examples:
    python scripts/run_build.py --dry-run
    python scripts/run_build.py
    python scripts/run_build.py --resume
    python scripts/run_build.py --ffmpeg-version 9.0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The package is the repository root (flat layout), so its *parent* goes on the path.
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))

from ffmpeg_builder.app import FFmpegBuilderApp, PROJECT_ROOT as APP_ROOT  # noqa: E402
from ffmpeg_builder.build_types import BuildError, SkipComponent  # noqa: E402
from ffmpeg_builder.builder import FFmpegBuilder  # noqa: E402
from ffmpeg_builder.builders.graphics.vulkan_headers import invalidate_vulkan_resume  # noqa: E402
from ffmpeg_builder.config import (  # noqa: E402
    TARGET_BACKEND_CROSS_MINGW,
    BuildConfig,
)
from ffmpeg_builder.g4_wave1 import run_g4_wave1  # noqa: E402
from ffmpeg_builder.g4_wave2 import run_g4_wave2  # noqa: E402
from ffmpeg_builder.g4_wave3a import run_g4_wave3a  # noqa: E402
from ffmpeg_builder.g4_wave3b import run_g4_wave3b  # noqa: E402
from ffmpeg_builder.g4_wave4a import run_g4_wave4a  # noqa: E402
from ffmpeg_builder.g4_wave4b import run_g4_wave4b  # noqa: E402
from ffmpeg_builder.g4_wave4c import run_g4_wave4c  # noqa: E402
from ffmpeg_builder.g4_wave5a import run_g4_wave5a  # noqa: E402
from ffmpeg_builder.g4_wave5b import run_g4_wave5b  # noqa: E402
from ffmpeg_builder.g4_wave5c import run_g4_wave5c  # noqa: E402
from ffmpeg_builder.g4_wave6 import run_g4_wave6  # noqa: E402
from ffmpeg_builder.g4_wave7 import run_g4_wave7  # noqa: E402
from ffmpeg_builder.platform_detect import PlatformDetector  # noqa: E402
from ffmpeg_builder.cuda_prerequisites import (  # noqa: E402
    cuda_nv_codec_identity,
    cuda_profile_identity,
    invalidate_cuda_resume,
    validate_required_cuda_components,
)
from ffmpeg_builder.state import ComponentStatus  # noqa: E402

assert APP_ROOT == PROJECT_ROOT, "app package root mismatch"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the previous build state instead of starting fresh.",
    )
    parser.add_argument(
        "--ffmpeg-version",
        help="Override the FFmpeg version from build_config.yaml (e.g. 8.1, 9.0).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate the configured environment and show its plan; "
            "cross builds stop after G4 Wave-0 selection."
        ),
    )
    parser.add_argument(
        "--no-release",
        action="store_true",
        help="Skip the release-bundle step even if make_release is enabled.",
    )
    parser.add_argument(
        "--g4-wave1",
        action="store_true",
        help=(
            "Build and accept only the fixed seven-component G4 Wave-1 subset "
            "for the linux-cross-mingw target."
        ),
    )
    parser.add_argument(
        "--g4-wave2",
        action="store_true",
        help=(
            "Build and accept only the fixed eleven-component G4 Wave-2 subset "
            "with the required primary OpenMP profile."
        ),
    )
    parser.add_argument(
        "--g4-wave3a",
        action="store_true",
        help=("Build and accept only the fixed six-component G4 Wave-3A LV2 " "dependency subset."),
    )
    parser.add_argument(
        "--g4-wave3b",
        action="store_true",
        help="Build and accept only the G4 Wave-3B Vorbis/Theora target subset.",
    )
    parser.add_argument(
        "--g4-wave4a",
        action="store_true",
        help="Build and accept only the G4 Wave-4A SVT-AV1 and AOM target subset.",
    )
    parser.add_argument(
        "--g4-wave4b",
        action="store_true",
        help="Build and accept only the G4 Wave-4B x264, x265 and libvpx target subset.",
    )
    parser.add_argument(
        "--g4-wave4c",
        action="store_true",
        help="Build and accept only the G4 Wave-4C zimg target subset.",
    )
    parser.add_argument(
        "--g4-wave5a",
        action="store_true",
        help="Build and accept only the G4 Wave-5A OpenSSL and SRT target subset.",
    )
    parser.add_argument(
        "--g4-wave5b",
        action="store_true",
        help="Build and accept only the G4 Wave-5B libzmq target subset.",
    )
    parser.add_argument("--g4-wave5c", action="store_true", help="Build and accept G4 Wave-5C.")
    parser.add_argument(
        "--g4-wave6", action="store_true", help="Build and accept only the G4 Wave-6 rav1e target."
    )
    parser.add_argument(
        "--g4-wave7", action="store_true", help="Build and accept the G4 Wave-7 Vulkan chain."
    )
    return parser.parse_args(argv)


def describe_plan(app: FFmpegBuilderApp, config: BuildConfig) -> int:
    """Print the build plan; return the component count."""
    components = app._get_buildable_components(config)
    toolchain_env = app._component_toolchain_env(config, components)
    print(f"FFmpeg {config.ffmpeg_version}: {len(components)} buildable components")
    for component in components:
        env = toolchain_env.get(component.name, {})
        tool = env.get("CC") or env.get("PATH_PREPEND") or "system"
        print(f"  {component.name:28s} {component.version:12s} cc={tool}")
    return len(components)


def run_build(app: FFmpegBuilderApp, config: BuildConfig, resume: bool, no_release: bool) -> int:
    """Execute the build; return an exit code."""
    try:
        session = app.prepare_target(config, resume)
        if getattr(config, "target_backend", "native") == TARGET_BACKEND_CROSS_MINGW:
            environment = app.prepare_cross_environment(config, session)
            print(
                "G3 cross-environment acceptance passed; "
                f"target={environment.target_triple}, "
                f"workspace={environment.paths.workspace_root}.",
                flush=True,
            )
            print(
                "Stopped before the G4 component graph; no full dependency-stack build was run.",
                flush=True,
            )
            return 2
        all_components = app._get_buildable_components(config)
        cuda_inputs = getattr(app.platform_detector.platform_info, "cuda_build_host_inputs", None)
        validate_required_cuda_components(config, all_components, cuda_inputs)
    except BuildError as exc:
        print(f"BLOCKED by identity guard: {exc}", flush=True)
        return 2
    for note in session.notes:
        print(f"NOTE: {note}", flush=True)
    components = list(all_components)
    nv_codec_identity = cuda_nv_codec_identity(
        all_components,
        vmaf_headers=(
            app.registry.get_nv_codec_component()
            if cuda_inputs and config.enable_libvmaf and config.enable_libvmaf_cuda
            else None
        ),
    )
    if resume:
        invalidate_vulkan_resume(app.state_manager, app.workspace, all_components)
        invalidate_cuda_resume(
            app.state_manager,
            cuda_inputs,
            libvmaf_cuda=config.enable_libvmaf and config.enable_libvmaf_cuda,
            ffmpeg_cuda=config.gpl_enabled or config.allow_nonfree_cuda,
            nv_codec=nv_codec_identity,
        )
        resume_point = app.state_manager.get_resume_point()
        if resume_point:
            idx = next((i for i, c in enumerate(components) if c.name == resume_point), 0)
            components = components[idx:]
            print(f"Resuming from {resume_point} ({len(components)} remaining)")
        else:
            print("No resume point: starting fresh")
            app.state_manager.reset()
    else:
        app.state_manager.reset()

    builder = FFmpegBuilder(
        config,
        app.workspace,
        app.packages,
        app.state_manager,
        app.platform_detector,
        toolchain_env=app._component_toolchain_env(config, all_components),
    )
    builder.prefetch_downloads(components)

    state = app.state_manager.get()
    state.config = config.to_dict()
    state.total_steps = len(all_components)
    state.cuda_profile = cuda_profile_identity(
        cuda_inputs,
        libvmaf_cuda=config.enable_libvmaf and config.enable_libvmaf_cuda,
        ffmpeg_cuda=config.gpl_enabled or config.allow_nonfree_cuda,
        nv_codec=nv_codec_identity,
    )
    app.state_manager.save()
    session.stamp(app.state_manager)

    completed = 0
    for position, component in enumerate(components, 1):
        label = f"[{position}/{len(components)}] {component.name} {component.version}"
        print(f"\n=== {label} ===", flush=True)
        state.current_step = all_components.index(component) + 1
        app.state_manager.save()
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
            builder.shutdown_downloads(wait=False)
            print(f"FAILED: {exc}", flush=True)
            if exc.log_file:
                print(f"  log: {exc.log_file}", flush=True)
            print(f"\nBuild stopped at {component.name} ({completed} completed).", flush=True)
            print("Rerun with --resume after fixing the cause.", flush=True)
            return 1
        finally:
            builder.shutdown_downloads(wait=False)

    print(f"\nAll components done: {completed} completed.", flush=True)
    drift = app.recheck_target_runtime(config, session)
    if drift:
        print(f"WARNING: {drift}", flush=True)
    if not no_release and config.make_release:
        try:
            release_dir = builder.make_release_bundle()
        except BuildError as exc:
            print(f"Release bundle failed: {exc}", flush=True)
            return 1
        print(f"Release bundle: {release_dir}", flush=True)
    return 0


def dry_run_cross_backend(app: FFmpegBuilderApp, config: BuildConfig, resume: bool) -> int:
    """Run G1-G3 preparation and report G4 Wave-0 selection without building."""
    try:
        session = app.prepare_target(config, resume)
        environment = app.prepare_cross_environment(config, session)
    except BuildError as exc:
        print(f"BLOCKED by identity guard or cross-environment acceptance: {exc}", flush=True)
        return 2
    for note in session.notes:
        print(f"NOTE: {note}", flush=True)
    print(
        "Dry-run passed: G3 cross-environment acceptance passed; "
        f"target={environment.target_triple}, "
        f"workspace={environment.paths.workspace_root}.",
        flush=True,
    )
    selection = app._get_component_selection(config)
    for component in selection.host_tools:
        tool = getattr(component, "system_tool_name", None) or component.name
        outcome = "satisfied" if tool in selection.system_satisfied_host_requirements else "missing"
        print(f"  HOST TOOL {component.name}: {outcome}", flush=True)
    if selection.missing_host_tools:
        print(
            "  MISSING HOST TOOLS: " + ", ".join(selection.missing_host_tools),
            flush=True,
        )
    for component in selection.target_inputs:
        print(f"  TARGET INPUT {component.name} {component.version}", flush=True)
    for component in selection.target_components_to_build:
        print(f"  TARGET BUILD {component.name} {component.version}", flush=True)
    for component in selection.mixed_components:
        print(f"  MIXED {component.name} {component.version}", flush=True)
    for component in selection.conditional_components:
        print(f"  CONDITIONAL {component.name} {component.version}", flush=True)
    for component in selection.skipped_by_feature:
        print(f"  SKIPPED BY FEATURE {component.name}", flush=True)
    for component in selection.skipped_by_backend:
        print(f"  SKIPPED BY BACKEND {component.name}", flush=True)
    print(
        "Stopped after G4 Wave-0 selection; no Wave-1 component was built.",
        flush=True,
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if (
        sum(
            (
                args.g4_wave1,
                args.g4_wave2,
                args.g4_wave3a,
                args.g4_wave3b,
                args.g4_wave4a,
                args.g4_wave4b,
                args.g4_wave4c,
                args.g4_wave5a,
                args.g4_wave5b,
                args.g4_wave5c,
                args.g4_wave6,
                args.g4_wave7,
            )
        )
        > 1
    ):
        raise SystemExit("G4 wave options are mutually exclusive.")
    if args.g4_wave1 and args.dry_run:
        raise SystemExit("--g4-wave1 performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave2 and args.dry_run:
        raise SystemExit("--g4-wave2 performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave3a and args.dry_run:
        raise SystemExit("--g4-wave3a performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave3b and args.dry_run:
        raise SystemExit("--g4-wave3b performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave4a and args.dry_run:
        raise SystemExit("--g4-wave4a performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave4b and args.dry_run:
        raise SystemExit("--g4-wave4b performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave4c and args.dry_run:
        raise SystemExit("--g4-wave4c performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave5a and args.dry_run:
        raise SystemExit("--g4-wave5a performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave5b and args.dry_run:
        raise SystemExit("--g4-wave5b performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave5c and args.dry_run:
        raise SystemExit("--g4-wave5c performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave6 and args.dry_run:
        raise SystemExit("--g4-wave6 performs real builds and cannot be combined with --dry-run.")
    if args.g4_wave7 and args.dry_run:
        raise SystemExit("--g4-wave7 performs real builds and cannot be combined with --dry-run.")
    if (
        args.g4_wave1
        or args.g4_wave2
        or args.g4_wave3a
        or args.g4_wave3b
        or args.g4_wave4a
        or args.g4_wave4b
        or args.g4_wave4c
        or args.g4_wave5a
        or args.g4_wave5b
        or args.g4_wave5c
        or args.g4_wave6
        or args.g4_wave7
    ) and args.ffmpeg_version not in (
        None,
        "9.0",
    ):
        raise SystemExit("G4 wave options are fixed to FFmpeg 9.0.")
    app = FFmpegBuilderApp()
    config = app.config_manager.get()
    if args.ffmpeg_version:
        config.ffmpeg_version = args.ffmpeg_version
    if (
        args.g4_wave1
        or args.g4_wave2
        or args.g4_wave3a
        or args.g4_wave3b
        or args.g4_wave4a
        or args.g4_wave4b
        or args.g4_wave4c
        or args.g4_wave5a
        or args.g4_wave5b
        or args.g4_wave5c
        or args.g4_wave6
        or args.g4_wave7
    ):
        config.ffmpeg_version = "9.0"
        config.target_backend = TARGET_BACKEND_CROSS_MINGW
        config.make_release = False
        app.platform_detector = PlatformDetector(config=config)
        app.system_info, app.platform_info, app.tools = app.platform_detector.detect_all()
    app._sync_workspace(config)

    if args.g4_wave1:
        return run_g4_wave1(app, config)
    if args.g4_wave2:
        return run_g4_wave2(app, config)
    if args.g4_wave3a:
        return run_g4_wave3a(app, config)
    if args.g4_wave3b:
        return run_g4_wave3b(app, config)
    if args.g4_wave4a:
        return run_g4_wave4a(app, config)
    if args.g4_wave4b:
        return run_g4_wave4b(app, config)
    if args.g4_wave4c:
        return run_g4_wave4c(app, config)
    if args.g4_wave5a:
        return run_g4_wave5a(app, config)
    if args.g4_wave5b:
        return run_g4_wave5b(app, config)
    if args.g4_wave5c:
        return run_g4_wave5c(app, config)
    if args.g4_wave6:
        return run_g4_wave6(app, config)
    if args.g4_wave7:
        return run_g4_wave7(app, config)
    if args.dry_run:
        if getattr(config, "target_backend", "native") == TARGET_BACKEND_CROSS_MINGW:
            return dry_run_cross_backend(app, config, resume=args.resume)
        describe_plan(app, config)
        return 0
    return run_build(app, config, resume=args.resume, no_release=args.no_release)


if __name__ == "__main__":
    sys.exit(main())
