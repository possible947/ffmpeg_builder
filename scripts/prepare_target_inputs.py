#!/usr/bin/env python3
"""Materialise project-managed Windows target headers and link interfaces."""

import argparse
import sys
import tempfile
from pathlib import Path

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.target_inputs import materialize_target_inputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-prefix", type=Path, required=True)
    parser.add_argument(
        "--toolchain-root",
        type=Path,
        required=True,
        help="Installed LLVM-MinGW ToolchainStore profile root",
    )
    parser.add_argument(
        "--source-archives",
        type=Path,
        default=PROJECT_ROOT / "third_party" / "sources",
    )
    args = parser.parse_args()

    try:
        with tempfile.TemporaryDirectory(prefix="ffmpeg-target-input-packages-") as packages:
            manifest = materialize_target_inputs(
                registry=ComponentRegistry(),
                target_backend=TARGET_BACKEND_CROSS_MINGW,
                source_archives_dir=args.source_archives,
                packages_dir=Path(packages),
                target_prefix=args.target_prefix,
                toolchain_root=args.toolchain_root,
                allow_network_downloads=False,
            )
    except (BuildError, OSError, ValueError) as exc:
        print(f"Target-input preparation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Materialised target inputs at {args.target_prefix}")
    print(f"Materialisation identity: {manifest['materialization_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
