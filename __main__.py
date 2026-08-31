"""Entry point for FFmpeg Builder."""

import sys
from pathlib import Path

from .app import FFmpegBuilderApp

# The package is the repository root (flat layout), so this module's
# directory is the project root. Anchoring paths here keeps the launcher
# working no matter which directory it is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parent


def main():
    """Main entry point."""
    workspace = PROJECT_ROOT / "workspace"
    workspace.mkdir(exist_ok=True)

    app = FFmpegBuilderApp(workspace)
    sys.exit(app.run())


if __name__ == "__main__":
    main()
