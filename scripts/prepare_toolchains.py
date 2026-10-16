#!/usr/bin/env python3
"""Provision toolchain profiles headlessly (non-interactive twin of the TUI store).

Selection defaults to the profiles recommended by the current system analysis
(see `ToolchainRegistry.recommend_profiles`). Use `--all` for every deployable
profile or `--profiles NAME ...` for an explicit, ordered list.

Examples:
    python scripts/prepare_toolchains.py --dry-run
    python scripts/prepare_toolchains.py
    python scripts/prepare_toolchains.py --profiles gmp mpfr mpc gcc12 rust
    python scripts/prepare_toolchains.py --all --jobs 8 --allow-network

Exit code is non-zero when any profile fails; acquisition logs stay under
`<store>/logs/<profile>/` for inspection.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The package is the repository root (flat layout), so its *parent* goes on the path.
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))

from ffmpeg_builder.config import ConfigManager  # noqa: E402
from ffmpeg_builder.ffmpeg_policy import detected_system_tool_versions  # noqa: E402
from ffmpeg_builder.platform_detect import PlatformDetector  # noqa: E402
from ffmpeg_builder.toolchain_registry import ToolchainRegistry  # noqa: E402
from ffmpeg_builder.toolchain_store import InstalledToolchain, ToolchainStore  # noqa: E402
from ffmpeg_builder import toolchain_builders  # noqa: E402


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        metavar="NAME",
        help="Explicit ordered list of profiles to acquire.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Acquire every deployable profile for the platform.",
    )
    parser.add_argument(
        "--ffmpeg-version",
        help="FFmpeg version used for the recommendation (default: build_config.yaml).",
    )
    parser.add_argument(
        "--platform",
        help="Target platform for the recommendation (default: detected).",
    )
    parser.add_argument("--jobs", type=int, help="Parallel make jobs (default: CPU count).")
    parser.add_argument(
        "--store",
        type=Path,
        help="Toolchain store root (default: <repo>/toolchains).",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Permit downloading archives missing from the local sources.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved acquisition order and exit.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help=(
            "Re-verify already installed profiles instead of acquiring anything "
            "(used to test store relocation and detect broken installs)."
        ),
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help=(
            "With --verify-only and --store pointing at a moved copy: rewrite "
            "recorded paths to the new root after successful re-verification."
        ),
    )
    return parser.parse_args(argv)


def select_profiles(
    registry: ToolchainRegistry,
    args: argparse.Namespace,
    platform: str,
    ffmpeg_version: str,
    versions: dict,
    installed: List[str],
    cuda_version: Optional[str],
) -> List[str]:
    """Resolve the acquisition order for the requested selection mode."""
    if args.profiles:
        return list(args.profiles)
    if args.all:
        return sorted(
            name
            for name, profile in registry.profiles.items()
            if profile.deployable and platform in profile.platforms
        )
    return [
        name
        for name, _reason in registry.recommend_profiles(
            ffmpeg_version,
            platform,
            versions,
            components=[],
            installed=installed,
            cuda_version=cuda_version,
        )
    ]


def verify_installed(store: ToolchainStore, adopt_relocated: bool = False) -> int:
    """Re-run verification for every installed profile; return an exit code.

    Resolution goes through the store (:meth:`ToolchainStore.resolve_entry_path`),
    so running with ``--store <moved-copy>`` is the store-relocation acceptance
    check. Failures are stamped unverified in that store's state. With
    ``--adopt``, successfully re-anchored entries get their recorded path
    rewritten to the new root, making the moved copy authoritative.
    """
    registry = ToolchainRegistry()
    entries = store.list_installed()
    if not entries:
        print("Nothing installed to verify.")
        return 0

    failures = store.reverify(registry, adopt_relocated=adopt_relocated)
    for entry in sorted(entries, key=lambda item: item.profile):
        resolved = store.resolve_entry_path(entry)
        status = "FAILED" if entry.profile in failures else "ok    "
        print(f"{status} {entry.profile} ({entry.variant}) at {resolved}")
    if failures:
        print(f"\n{len(failures)} profile(s) failed verification.")
        return 1
    if adopt_relocated:
        print("\nRelocated store adopted: recorded paths now point at the new root.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    registry = ToolchainRegistry()
    config = ConfigManager(PROJECT_ROOT / "build_config.yaml").load()
    _, platform_info, tools = PlatformDetector().detect_all()
    platform = args.platform or platform_info.platform or "linux"
    ffmpeg_version = args.ffmpeg_version or config.ffmpeg_version
    versions = detected_system_tool_versions(platform_info, tools)

    store = ToolchainStore(
        args.store or (PROJECT_ROOT / "toolchains"),
        source_archives_dir=(
            PROJECT_ROOT / config.source_archives_dir if args.store is None else None
        ),
        allow_network_downloads=args.allow_network or config.allow_network_downloads,
    )

    installed_entries = store.list_installed()
    installed = [item.profile for item in installed_entries]

    if args.verify_only:
        return verify_installed(store, args.adopt)

    profiles = select_profiles(
        registry,
        args,
        platform,
        ffmpeg_version,
        versions,
        installed,
        platform_info.cuda_version,
    )

    print(f"Store:          {store.root}")
    print(f"Platform:       {platform}  (FFmpeg {ffmpeg_version})")
    print(f"Jobs:           {args.jobs or 'auto'}")
    print(f"Network:        {'allowed' if store.allow_network_downloads else 'disabled'}")
    print(f"Already in store: {', '.join(sorted(installed)) or '<none>'}")
    print(f"Acquisition order ({len(profiles)}): {', '.join(profiles) or '<none>'}")
    if not profiles:
        print("Nothing to do: the system analysis resolves the current FFmpeg version.")
        return 0
    if args.dry_run:
        return 0

    failures = 0
    for name in profiles:
        print(f"\n=== {name} ===", flush=True)
        try:
            entry = store.acquire(registry, name, jobs=args.jobs)
        except Exception as exc:  # reported, then the run continues
            failures += 1
            print(f"FAILED {name}: {exc}", flush=True)
            print(f"  logs: {store.logs_dir / name}", flush=True)
            continue
        print(f"installed ({entry.variant}) at {entry.path}", flush=True)

    if failures:
        print(f"\n{failures} profile(s) failed.")
        return 1
    print("\nAll requested profiles are installed and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
