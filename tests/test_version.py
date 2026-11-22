import tomllib
from pathlib import Path

from ffmpeg_builder import __version__


def test_package_version_matches_project_metadata():
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert __version__ == project["project"]["version"]
