"""Tests for builder module split compatibility."""

import json
from pathlib import Path

import pytest

from ffmpeg_builder.builder import BuildError, FFmpegBuilder
from ffmpeg_builder.components import BuildSystem, Component, ComponentCategory
from ffmpeg_builder.config import BuildConfig
from ffmpeg_builder.component_builders import get_custom_builder
from ffmpeg_builder.release_bundle import make_release_bundle
from ffmpeg_builder.state import StateManager


def test_builder_module_reexports_shared_exceptions():
    from ffmpeg_builder import build_types
    from ffmpeg_builder.builder import BuildError as BuilderBuildError
    from ffmpeg_builder.builder import SkipComponent as BuilderSkipComponent

    assert BuilderBuildError is build_types.BuildError
    assert BuilderSkipComponent is build_types.SkipComponent


def test_custom_builder_registry_contains_known_entries():
    assert get_custom_builder("build_ffmpeg") is not None
    assert get_custom_builder("build_libplacebo") is not None
    assert get_custom_builder("build_meson") is not None
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


def test_release_bundle_wrapper_static_binary_skips_dependency_scan(tmp_path: Path):
    """A fully static binary makes ldd fail; the bundle must still succeed."""

    class _Result:
        def __init__(self, stdout: str = "", stderr: str = "", success: bool = True):
            self.stdout = stdout
            self.stderr = stderr
            self.success = success

    class _Executor:
        def execute(self, command, env=None):
            if command[0] == "ldd":
                # ldd on a fully static ELF: exit 1, "not a dynamic executable"
                return _Result(stdout="", stderr="not a dynamic executable", success=False)
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

    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["missing_binaries"] == []
    assert manifest["dependencies"] == []
    assert manifest["missing_dependencies"] == []


class _PlatformInfo:
    cuda_available = False
    cuda_path = None
    vulkan_available = True
    vaapi_available = False
    amf_available = False
    opencl_available = False
    qsv_available = False
    is_msys2 = True
    is_ucrt64 = True
    is_wsl2 = False
    macports_clang = None


class _PlatformDetector:
    def __init__(self, platform: str = "windows"):
        self.platform_info = _PlatformInfo()
        self._platform = platform

    def get_num_jobs(self, requested):
        return 4

    def get_platform_name(self):
        return self._platform


def _make_libplacebo_builder(tmp_path: Path, platform: str = "windows") -> FFmpegBuilder:
    config = BuildConfig(
        gpl_enabled=True,
        enable_libplacebo_vulkan=True,
        openmp=False,
    )
    config.windows.backend = "msys2-ucrt64"
    state_manager = StateManager(tmp_path / "state.json")
    builder = FFmpegBuilder(
        config=config,
        workspace=tmp_path / "workspace",
        packages=tmp_path / "packages",
        state_manager=state_manager,
        platform_detector=_PlatformDetector(platform),
    )
    builder.env["PKG_CONFIG_PATH"] = "C:/deps/pkgconfig;C:/more"
    return builder


def _make_libplacebo_component() -> Component:
    return Component(
        name="libplacebo",
        version="7.360.1",
        url="https://github.com/haasn/libplacebo/archive/refs/tags/v{version}.tar.gz",
        category=ComponentCategory.HW_ACCEL,
        build_system=BuildSystem.MESON,
        configure_args=["--prefix={workspace}", "--default-library=static"],
    )


def test_build_libplacebo_merges_windows_pkg_config_path_and_patches_glslang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    builder = _make_libplacebo_builder(tmp_path, platform="windows")
    component = _make_libplacebo_component()
    source_dir = tmp_path / "src"
    glsl_meson = source_dir / "src" / "glsl" / "meson.build"
    glsl_meson.parent.mkdir(parents=True, exist_ok=True)
    glsl_meson.write_text(
        "cxx.find_library('glslang', required: required, static: static)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        FFmpegBuilder,
        "_prepend_python_module_parent_to_pythonpath",
        staticmethod(lambda env, module_name: None),
    )

    class _Result:
        success = True

    def _execute_with_log(command, component_name, step, cwd, env, timeout=None, stdin=None):
        return _Result(), tmp_path / f"{component_name}_{step}.log"

    builder.executor.execute_with_log = _execute_with_log

    captured_env = {}

    def _run_step(self, component, status, detail, error_msg, command, step_name, work_dir, env, stdin=None):
        if step_name == "configure":
            captured_env["value"] = dict(env)
        return object(), tmp_path / f"{step_name}.log"

    monkeypatch.setattr(FFmpegBuilder, "_run_step", _run_step)
    monkeypatch.setattr(FFmpegBuilder, "_patch_libplacebo_pc", lambda self: None)

    builder.build_libplacebo(component, source_dir)

    assert "value" in captured_env
    env = captured_env["value"]
    expected_prefix = (
        str(tmp_path / "workspace" / "lib" / "pkgconfig").replace("\\", "/")
        + ";"
        + str(tmp_path / "workspace" / "lib64" / "pkgconfig").replace("\\", "/")
    )
    assert env["PKG_CONFIG_PATH"].startswith(
        expected_prefix
    )
    assert "C:/deps/pkgconfig" in env["PKG_CONFIG_PATH"]
    assert "dirs: vulkan_lib_dirs" in glsl_meson.read_text(encoding="utf-8")


def test_build_libplacebo_fails_when_glslang_patch_target_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    builder = _make_libplacebo_builder(tmp_path, platform="windows")
    component = _make_libplacebo_component()
    source_dir = tmp_path / "src"
    glsl_meson = source_dir / "src" / "glsl" / "meson.build"
    glsl_meson.parent.mkdir(parents=True, exist_ok=True)
    glsl_meson.write_text("project('libplacebo')\n", encoding="utf-8")

    monkeypatch.setattr(
        FFmpegBuilder,
        "_prepend_python_module_parent_to_pythonpath",
        staticmethod(lambda env, module_name: None),
    )

    class _Result:
        success = True

    def _execute_with_log(command, component_name, step, cwd, env, timeout=None, stdin=None):
        return _Result(), tmp_path / f"{component_name}_{step}.log"

    builder.executor.execute_with_log = _execute_with_log

    with pytest.raises(BuildError, match="Expected glslang lookup pattern"):
        builder.build_libplacebo(component, source_dir)
