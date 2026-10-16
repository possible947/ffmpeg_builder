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
from ffmpeg_builder.config import BuildConfig  # noqa: E402
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
        help="List the buildable components with their toolchain env and exit.",
    )
    parser.add_argument(
        "--no-release",
        action="store_true",
        help="Skip the release-bundle step even if make_release is enabled.",
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
    all_components = app._get_buildable_components(config)
    components = list(all_components)
    if resume:
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
    app.state_manager.save()

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
    if not no_release and config.make_release:
        try:
            release_dir = builder.make_release_bundle()
        except BuildError as exc:
            print(f"Release bundle failed: {exc}", flush=True)
            return 1
        print(f"Release bundle: {release_dir}", flush=True)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    app = FFmpegBuilderApp()
    config = app.config_manager.get()
    if args.ffmpeg_version:
        config.ffmpeg_version = args.ffmpeg_version
    app._sync_workspace(config)

    if args.dry_run:
        describe_plan(app, config)
        return 0
    return run_build(app, config, resume=args.resume, no_release=args.no_release)


if __name__ == "__main__":
    sys.exit(main())
