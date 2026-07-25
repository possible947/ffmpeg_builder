"""Entry point for FFmpeg Builder."""
import sys
from pathlib import Path
from .app import FFmpegBuilderApp


def main():
    """Main entry point."""
    workspace = Path("workspace")
    workspace.mkdir(exist_ok=True)
    
    app = FFmpegBuilderApp(workspace)
    sys.exit(app.run())


if __name__ == "__main__":
    main()
