"""Tests for builder module split compatibility."""

import json
import shutil
from pathlib import Path

import pytest

from ffmpeg_builder.builder import BuildError, CARGO_C_VERSION, FFmpegBuilder
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


def test_build_all_removed_dead_code_guard():
    """build_all() was dead code that diverged from app._run_build() (H3).

    The real build loop lives in FFmpegBuilderApp._run_build() with
    retry/skip/abort handling; a second loop in the builder invites
    divergence. Keep it deleted.
    """
    assert not hasattr(FFmpegBuilder, "build_all")


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


def test_release_bundle_macos_rewrites_install_names_and_rpaths(tmp_path: Path):
    """M8: the macOS bundle must be relocatable.

    References to bundled dylibs are rewritten to @rpath/<name>, each
    bundled dylib's install name is set to @rpath/<name>, @loader_path is
    added as an rpath to every Mach-O file, and each file is re-signed
    ad-hoc. References to system libraries are left untouched.
    """

    class _Result:
        def __init__(self, stdout: str = "", stderr: str = "", success: bool = True):
            self.stdout = stdout
            self.stderr = stderr
            self.success = success

    bundled_dylib = tmp_path / "lib" / "libfoo.dylib"

    class _Executor:
        def __init__(self):
            self.install_name_tool_calls = []
            self.codesign_calls = []

        def execute(self, command, env=None):
            if command[0] == "otool" and command[1] == "-L":
                # Like real otool, the dylib lists its own install name
                # (bundled_dylib) as the first entry. /usr/lib/* is a
                # dyld-shared-cache reference (no file on disk, not
                # missing); /opt/local/lib/libFakeMissing is genuinely
                # missing.
                deps = [
                    str(bundled_dylib),
                    "/usr/lib/libFakeSystem.dylib",
                    "/opt/local/lib/libFakeMissing.dylib",
                ]
                stdout = f"{command[2]}:\n" + "".join(
                    f"\t{dep} (compatibility version 1.0.0)\n" for dep in deps
                )
                return _Result(stdout=stdout)
            if command[0] == "otool" and command[1] == "-l":
                return _Result(
                    stdout="      cmd LC_RPATH\n  cmdsize 32\n"
                    "     path /opt/local/lib/libomp (offset 12)\n"
                )
            if command[0] == "install_name_tool":
                self.install_name_tool_calls.append(list(command))
                return _Result()
            if command[0] == "codesign":
                self.codesign_calls.append(list(command))
                return _Result()
            raise AssertionError(f"Unexpected command: {command}")

    class _PlatformDetector:
        def get_build_backend_name(self):
            return "macos-native"

    class _Config:
        ffmpeg_version = "8.1"

        class windows:
            msys2_root = "C:/msys64"

    executor = _Executor()

    class _Builder:
        platform = "darwin"
        workspace = tmp_path
        config = _Config()
        platform_detector = _PlatformDetector()

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

    _Builder.executor = executor

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "ffprobe", "ffplay"):
        (bin_dir / name).write_text("binary", encoding="utf-8")
    bundled_dylib.parent.mkdir(parents=True)
    bundled_dylib.write_text("dylib", encoding="utf-8")

    release_dir = make_release_bundle(_Builder())

    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"] == [str(release_dir / "libfoo.dylib")]
    # System (dyld shared cache) references are not reported as missing;
    # only genuinely missing dylibs are.
    assert manifest["missing_dependencies"] == ["/opt/local/lib/libFakeMissing.dylib"]
    assert len(manifest["install_name_rewrites"]) == 4

    calls = executor.install_name_tool_calls
    changes = [call for call in calls if call[1] == "-change"]
    assert len(changes) == 3
    for call in changes:
        assert call[2] == str(bundled_dylib)
        assert call[3] == "@rpath/libfoo.dylib"

    ids = [call for call in calls if call[1] == "-id"]
    assert ids == [
        ["install_name_tool", "-id", "@rpath/libfoo.dylib", str(release_dir / "libfoo.dylib")]
    ]

    rpaths = [call for call in calls if call[1] == "-add_rpath"]
    assert len(rpaths) == 4
    for call in rpaths:
        assert call[2] == "@loader_path"

    assert len(executor.codesign_calls) == 4
    for call in executor.codesign_calls:
        assert call[1:4] == ["--force", "-s", "-"]


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


def _make_builder_with_archives(tmp_path: Path, source_archives_dir: str) -> FFmpegBuilder:
    config = BuildConfig(source_archives_dir=source_archives_dir)
    config.windows.backend = "msys2-ucrt64"
    return FFmpegBuilder(
        config=config,
        workspace=tmp_path / "workspace",
        packages=tmp_path / "packages",
        state_manager=StateManager(tmp_path / "state.json"),
        platform_detector=_PlatformDetector("windows"),
    )


def test_relative_source_archives_dir_anchored_to_project_root(tmp_path: Path):
    """M5: a relative source_archives_dir resolves against the project root, not the CWD."""
    from ffmpeg_builder import builder as builder_module

    builder = _make_builder_with_archives(tmp_path, "third_party/sources")
    assert builder.source_archives == builder_module.PROJECT_ROOT / "third_party/sources"


def test_absolute_source_archives_dir_kept(tmp_path: Path):
    """M5: an absolute source_archives_dir is used as-is."""
    archives = tmp_path / "archives"
    builder = _make_builder_with_archives(tmp_path, str(archives))
    assert builder.source_archives == archives


def test_build_cargo_installs_pinned_cargo_c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """M6: the cargo-c install must pin an explicit version."""
    builder = _make_builder_with_archives(tmp_path, str(tmp_path / "archives"))
    component = _make_libplacebo_component()
    source_dir = tmp_path / "src"

    monkeypatch.setattr(FFmpegBuilder, "_get_rustc_version", lambda self: (1, 95, 0))
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)

    commands = []

    class _Result:
        success = True

    def _execute_with_log(command, component_name, step, cwd, env, timeout=None, stdin=None):
        commands.append(list(command))
        return _Result(), tmp_path / f"{component_name}_{step}.log"

    builder.executor.execute_with_log = _execute_with_log

    builder._build_cargo(component, source_dir)

    assert commands[0] == ["cargo", "install", "cargo-c", "--version", CARGO_C_VERSION]
    assert commands[1][:2] == ["cargo", "cinstall"]


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

    def _run_step(
        self, component, status, detail, error_msg, command, step_name, work_dir, env, stdin=None
    ):
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
    assert env["PKG_CONFIG_PATH"].startswith(expected_prefix)
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


def _make_patch_component(name: str = "x265") -> Component:
    return Component(
        name=name,
        version="1.0",
        url=f"https://example.invalid/{name}.tar.gz",
        category=ComponentCategory.VIDEO_CODEC,
        build_system=BuildSystem.CUSTOM,
    )


class TestSourcePatchAssertions:
    """M7: source patches must verify they took effect and fail loudly if not."""

    def test_assert_patch_absent_passes_when_marker_removed(self, tmp_path):
        builder = _make_libplacebo_builder(tmp_path)
        f = tmp_path / "file.txt"
        f.write_text("clean content", encoding="utf-8")
        builder._assert_patch_absent(_make_patch_component(), f, "bad_marker", "ctx")

    def test_assert_patch_absent_raises_when_marker_present(self, tmp_path):
        builder = _make_libplacebo_builder(tmp_path)
        f = tmp_path / "file.txt"
        f.write_text("has bad_marker here", encoding="utf-8")
        with pytest.raises(BuildError, match="version may have changed"):
            builder._assert_patch_absent(_make_patch_component(), f, "bad_marker", "ctx")

    def test_assert_patch_present_passes_when_marker_present(self, tmp_path):
        builder = _make_libplacebo_builder(tmp_path)
        f = tmp_path / "file.txt"
        f.write_text("#include <cstdint>\n", encoding="utf-8")
        builder._assert_patch_present(_make_patch_component(), f, "#include <cstdint>", "ctx")

    def test_assert_patch_present_raises_when_marker_missing(self, tmp_path):
        builder = _make_libplacebo_builder(tmp_path)
        f = tmp_path / "file.txt"
        f.write_text("#include <string>\n", encoding="utf-8")
        with pytest.raises(BuildError, match="version may have changed"):
            builder._assert_patch_present(_make_patch_component(), f, "#include <cstdint>", "ctx")

    def test_build_x265_fails_when_json11_anchor_missing(self, tmp_path):
        # json11.cpp without the `#include <limits>` anchor and without
        # cstdint: the patch cannot apply, so the build must fail fast with
        # a clear message instead of an obscure uint8_t compile error.
        builder = _make_libplacebo_builder(tmp_path)
        component = _make_patch_component("x265")
        json11_dir = tmp_path / "source" / "dynamicHDR10" / "json11"
        json11_dir.mkdir(parents=True)
        (json11_dir / "json11.cpp").write_text(
            "#include <string>\nnamespace json { struct object {}; }\n", encoding="utf-8"
        )
        with pytest.raises(BuildError, match="version may have changed"):
            builder.build_x265(component, tmp_path)

    def test_build_x265_inserts_cstdint_when_anchor_present(self, tmp_path):
        builder = _make_libplacebo_builder(tmp_path)
        component = _make_patch_component("x265")
        json11_dir = tmp_path / "source" / "dynamicHDR10" / "json11"
        json11_dir.mkdir(parents=True)
        json11_file = json11_dir / "json11.cpp"
        json11_file.write_text("#include <limits>\nint x;\n", encoding="utf-8")
        # No build/linux dir: the method proceeds past the json11 patch and
        # fails on the missing build dir, proving the patch assertion passed.
        with pytest.raises(BuildError, match="Build directory not found"):
            builder.build_x265(component, tmp_path)
        patched = json11_file.read_text(encoding="utf-8")
        assert "#include <cstdint>" in patched
        assert patched.index("#include <limits>") < patched.index("#include <cstdint>")
