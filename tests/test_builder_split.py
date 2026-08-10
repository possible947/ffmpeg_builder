"""Tests for builder module split compatibility."""

from pathlib import Path

from ffmpeg_builder.builder import BuildError, SkipComponent
from ffmpeg_builder.component_builders import get_custom_builder
from ffmpeg_builder.release_bundle import make_release_bundle


def test_builder_module_reexports_shared_exceptions():
    from ffmpeg_builder import build_types
    from ffmpeg_builder.builder import BuildError as BuilderBuildError
    from ffmpeg_builder.builder import SkipComponent as BuilderSkipComponent

    assert BuilderBuildError is build_types.BuildError
    assert BuilderSkipComponent is build_types.SkipComponent


def test_custom_builder_registry_contains_known_entries():
    assert get_custom_builder("build_ffmpeg") is not None
    assert get_custom_builder("build_libplacebo") is not None
    assert get_custom_builder("missing-builder") is None


def test_release_bundle_wrapper_creates_manifest(tmp_path: Path):
    class _Result:
        def __init__(self, stdout: str = "", stderr: str = "", success: bool = True):
            self.stdout = stdout
            self.stderr = stderr
            self.success = success

    class _Executor:
        def execute(self, command, env=None):
            if command[0] == "ldd":
                return _Result(stdout="")
            raise AssertionError(f"Unexpected command: {command}")

    class _PlatformDetector:
        def get_build_backend_name(self):
            return "linux-native"

    class _Config:
        ffmpeg_version = "8.1"

        class windows:
            msys2_root = "C:/msys64"

    class _Builder:
        platform = "linux"
        workspace = tmp_path
        config = _Config()
        platform_detector = _PlatformDetector()
        executor = _Executor()

        @staticmethod
        def _rmtree(path: Path) -> None:
            if path.exists():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink()
                    else:
                        child.rmdir()
                path.rmdir()

        def get_build_env(self):
            return {}

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "ffprobe", "ffplay"):
        (bin_dir / name).write_text("binary", encoding="utf-8")

    release_dir = make_release_bundle(_Builder())

    assert release_dir == tmp_path / "release"
    assert (release_dir / "manifest.json").exists()
