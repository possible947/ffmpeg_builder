"""G3 Linux-to-Windows cross-environment contract and acceptance tests."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.cross_environment import (
    _resolve_target_tools,
    construct_cross_environment,
    run_cross_environment_acceptance,
    validate_pkg_config_output,
)
from ffmpeg_builder.platforms.context import PlatformContext
from ffmpeg_builder.platforms.darwin import MacOSPlatform
from ffmpeg_builder.platforms.linux_cross_mingw import LinuxCrossMingwPlatform
from ffmpeg_builder.platforms.linux_gcc13 import LinuxGcc13Platform
from ffmpeg_builder.platforms.resolver import PlatformStrategyResolver
from ffmpeg_builder.platforms.windows_ucrt64 import WindowsUcrt64Platform
from ffmpeg_builder.toolchain_registry import ToolchainRegistry
from ffmpeg_builder.toolchain_store import (
    STATUS_COMPATIBLE,
    ToolchainStore,
    resolve_store_root,
)
from ffmpeg_builder.target_inputs import materialize_target_inputs
from ffmpeg_builder.wsl_mingw_preparation import resolve_wsl_mingw_project_toolchains
from ffmpeg_builder.scripts.run_build import dry_run_cross_backend, run_build

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _context(config: BuildConfig, platform_info, tmp_path: Path) -> PlatformContext:
    return PlatformContext(
        config=config,
        workspace=tmp_path / "workspace",
        packages=tmp_path / "packages",
        platform_detector=SimpleNamespace(),
        platform_info=platform_info,
        num_jobs=1,
    )


def test_platform_strategy_selects_cross_backend_without_changing_native_routes(tmp_path):
    resolver = PlatformStrategyResolver()
    linux = SimpleNamespace(
        is_linux=True,
        is_macos=False,
        is_windows=False,
        is_msys2=False,
        is_ucrt64=False,
        gcc_major_version=14,
    )
    cross = resolver.resolve(
        _context(BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW), linux, tmp_path)
    )
    native = resolver.resolve(_context(BuildConfig(), linux, tmp_path))
    legacy = resolver.resolve(
        _context(
            BuildConfig(),
            SimpleNamespace(is_linux=False, is_windows=True, is_msys2=True, is_ucrt64=True),
            tmp_path,
        )
    )
    macos = resolver.resolve(
        _context(
            BuildConfig(),
            SimpleNamespace(is_linux=False, is_macos=True, is_windows=False),
            tmp_path,
        )
    )
    windows = resolver.resolve(
        _context(
            BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW),
            SimpleNamespace(is_linux=False, is_windows=True, is_msys2=True, is_ucrt64=True),
            tmp_path,
        )
    )

    assert isinstance(cross, LinuxCrossMingwPlatform)
    assert isinstance(native, LinuxGcc13Platform)
    assert isinstance(legacy, WindowsUcrt64Platform)
    assert isinstance(macos, MacOSPlatform)
    assert windows is None


def test_missing_registered_target_tool_fails_without_ambient_fallback(tmp_path):
    prefix = tmp_path / "llvm-mingw"
    compiler = prefix / "bin" / "cross-clang"
    compiler.parent.mkdir(parents=True)
    compiler.write_text(
        "#!/bin/sh\n" "printf 'clang version 23.1.5\\nTarget: x86_64-w64-windows-gnu\\n'\n",
        encoding="utf-8",
    )
    compiler.chmod(0o755)
    preparation = SimpleNamespace(
        project_toolchains={
            "llvm-mingw": {
                "prefix": str(prefix),
                "target": {"triple": "x86_64-w64-mingw32", "runtime": "ucrt"},
                "provenance": {"verified": True},
                "paths": {"CC": str(compiler)},
            }
        }
    )

    with pytest.raises(BuildError) as raised:
        _resolve_target_tools(preparation)

    assert raised.value.code == "wsl_mingw_target_tool_mapping_missing"
    assert "cxx" in str(raised.value)


@pytest.mark.parametrize(
    "value",
    [
        "-I/usr/include",
        "-L/usr/lib/x86_64-linux-gnu",
        "-L/usr/lib64",
        "-I/opt/cuda/include",
        "-L/opt/vulkan-sdk/lib",
        "-I/tmp/session-state/include",
        "-L/c/msys64/ucrt64/lib",
        "-L C:/msys64/ucrt64/lib",
        "-Wl,-rpath=/usr/lib",
    ],
)
def test_pkg_config_validator_rejects_host_and_sdk_paths(value, tmp_path):
    target_prefix = tmp_path / "target-prefix"

    with pytest.raises(BuildError) as raised:
        validate_pkg_config_output(value, (target_prefix,))

    assert raised.value.code == "wsl_mingw_pkg_config_contamination"
    assert raised.value.diagnostic.variable == "pkg-config output"


def test_pkg_config_validator_uses_path_ancestry_not_string_prefix(tmp_path):
    target_prefix = tmp_path / "prefix"
    validate_pkg_config_output(f"-I{target_prefix}/include", (target_prefix,))

    with pytest.raises(BuildError):
        validate_pkg_config_output(f"-I{target_prefix}-sibling/include", (target_prefix,))


def test_headless_cross_route_stops_after_environment_acceptance(capsys):
    class HeadlessApp:
        def prepare_target(self, config, resume):
            return SimpleNamespace(uses_isolated_workspace=True)

        def prepare_cross_environment(self, config, session):
            return SimpleNamespace(
                target_triple="x86_64-w64-mingw32",
                paths=SimpleNamespace(workspace_root=Path("/identity/workspace")),
            )

        def _get_buildable_components(self, config):
            raise AssertionError("G3 pass 1 must stop before component selection")

    status = run_build(
        HeadlessApp(),
        BuildConfig(
            target_backend=TARGET_BACKEND_CROSS_MINGW,
            cuda_build_mode="disabled",
        ),
        resume=False,
        no_release=True,
    )

    assert status == 2
    assert "G3 pass-1 cross environment prepared and validated" in capsys.readouterr().out


def test_headless_cross_dry_run_performs_acceptance_without_component_selection(capsys):
    class HeadlessApp:
        def prepare_target(self, config, resume):
            return SimpleNamespace(
                uses_isolated_workspace=True,
                notes=("identity authorized",),
            )

        def prepare_cross_environment(self, config, session):
            return SimpleNamespace(
                target_triple="x86_64-w64-mingw32",
                paths=SimpleNamespace(workspace_root=Path("/identity/workspace")),
            )

        def _get_buildable_components(self, config):
            raise AssertionError("G3 pass 1 must stop before component selection")

    status = dry_run_cross_backend(
        HeadlessApp(),
        BuildConfig(
            target_backend=TARGET_BACKEND_CROSS_MINGW,
            cuda_build_mode="disabled",
        ),
        resume=False,
    )

    output = capsys.readouterr().out
    assert status == 0
    assert "identity authorized" in output
    assert "Dry-run passed: G3 pass-1 cross environment prepared and validated" in output


def _available_cross_inputs(tmp_path: Path):
    """Return real installed G2 inputs, or skip when this host lacks acceptance data."""
    host_tools = (
        "cmake",
        "meson",
        "ninja",
        "make",
        "pkg-config",
        "perl",
        "sh",
        "autoconf",
        "autoreconf",
    )
    if any(shutil.which(name) is None for name in host_tools):
        pytest.skip("G3 host build tools are not installed")
    tool_registry = ToolchainRegistry()
    store = ToolchainStore(
        resolve_store_root(PROJECT_ROOT),
        workspace=tmp_path / "store-workspace",
        source_archives_dir=PROJECT_ROOT / "third_party" / "sources",
        backend=TARGET_BACKEND_CROSS_MINGW,
    )
    for profile in ("llvm-mingw", "rust"):
        status, _reason = store.installed_status(tool_registry, profile)
        if status != STATUS_COMPATIBLE:
            pytest.skip(f"project-managed {profile} profile is unavailable")

    components = ComponentRegistry()
    inputs = components.get_target_inputs(TARGET_BACKEND_CROSS_MINGW)
    by_name = {item.name: item for item in components.get_all()}
    required = {item.name: item for item in inputs}
    for item in inputs:
        if item.target_import_interface:
            source = by_name.get(item.target_import_interface.definition_source_component)
            if source is not None:
                required[source.name] = source
    for component in required.values():
        archive = PROJECT_ROOT / "third_party" / "sources" / component.get_archive_filename()
        if not archive.is_file():
            pytest.skip(f"local target-input archive is missing: {archive.name}")
        with archive.open("rb") as source:
            is_lfs_pointer = source.read(64).startswith(
                b"version https://git-lfs.github.com/spec/v1"
            )
        if is_lfs_pointer:
            pytest.skip(f"local target-input archive is an unhydrated LFS pointer: {archive.name}")

    return components, tool_registry, store


def test_real_cross_environment_and_build_system_acceptance(tmp_path):
    components, tool_registry, store = _available_cross_inputs(tmp_path)
    toolchains = resolve_wsl_mingw_project_toolchains("8.1", tool_registry, store)
    identity_manifest = materialize_target_inputs(
        components,
        TARGET_BACKEND_CROSS_MINGW,
        PROJECT_ROOT / "third_party" / "sources",
        tmp_path / "identity-packages",
        tmp_path / "identity-prefix",
        Path(toolchains["llvm-mingw"]["prefix"]),
        allow_network_downloads=False,
    )
    preparation = SimpleNamespace(
        project_toolchains=toolchains,
        identity_summary={
            "target_input_materialization_id": identity_manifest["materialization_id"]
        },
        cuda=None,
        vulkan=None,
        diagnostic_only={
            "project_toolchains": {
                name: profile["provenance"] for name, profile in toolchains.items()
            }
        },
    )
    config = BuildConfig(
        target_backend=TARGET_BACKEND_CROSS_MINGW,
        cuda_build_mode="disabled",
    )
    parent = dict(os.environ)
    parent.update(
        {
            "CC": "/usr/bin/gcc",
            "CXX": "/usr/bin/g++",
            "CPATH": "/usr/include",
            "CUDA_HOME": "/opt/cuda",
            "VULKAN_SDK": "/opt/vulkan-sdk",
            "PKG_CONFIG_PATH": "/usr/lib/pkgconfig",
            "CMAKE_PREFIX_PATH": "/usr/local",
            "LD_LIBRARY_PATH": "/usr/lib",
            "PATH": "/usr/bin:/bin",
        }
    )
    snapshot = dict(parent)
    workspace = tmp_path / "identity-authorized-workspace"
    environment = construct_cross_environment(
        preparation,
        config,
        workspace,
        PROJECT_ROOT / "third_party" / "sources",
        parent_environment=parent,
        registry=components,
    )
    repeat = construct_cross_environment(
        preparation,
        config,
        workspace,
        PROJECT_ROOT / "third_party" / "sources",
        parent_environment=parent,
        registry=components,
    )

    assert parent == snapshot
    assert environment.semantic_projection() == repeat.semantic_projection()
    assert environment.compiler_environment["CC"] == str(
        Path(toolchains["llvm-mingw"]["paths"]["CC"])
    )
    assert environment.compiler_environment["PATH"] == "/usr/bin:/bin"
    assert "PKG_CONFIG_PATH" not in environment.compiler_environment
    for name in ("CUDA_HOME", "VULKAN_SDK", "CPATH", "LD_LIBRARY_PATH"):
        assert name not in environment.compiler_environment
    assert "-march=native" not in environment.compiler_environment["CFLAGS"]
    assert environment.autotools.build_tuple != environment.autotools.host_tuple
    assert environment.rust_tools.target == "x86_64-pc-windows-gnullvm"
    assert environment.paths.target_prefix == workspace / "prefix"
    assert environment.pkg_config_wrapper.is_file()
    assert environment.cmake_toolchain_file.is_file()
    assert environment.meson_cross_file.is_file()

    acceptance = run_cross_environment_acceptance(environment)

    assert set(acceptance) == {"pkg_config", "compiler", "cmake", "meson", "autotools"}
    assert acceptance["pkg_config"]["host_fallback_blocked"] is True
    assert acceptance["pkg_config"]["malicious_path_rejected"]
    assert acceptance["compiler"]["ucrt_import"] == "api-ms-win-crt-*"
    assert acceptance["cmake"]["built"] is True
    assert acceptance["cmake"]["host_only_package_blocked"] is True
    assert acceptance["meson"]["host_machine"]["system"] == "windows"
    assert acceptance["meson"]["build_machine"]["system"] == "linux"
    assert acceptance["autotools"]["cross_compiling"] is True
    assert not list(environment.paths.diagnostics_root.glob("g3-acceptance-*"))

    header = environment.paths.target_prefix / "include" / "vulkan" / "vulkan.h"
    original = header.read_bytes()
    header.write_bytes(original + b"\n")
    with pytest.raises(BuildError) as raised:
        construct_cross_environment(
            preparation,
            config,
            workspace,
            PROJECT_ROOT / "third_party" / "sources",
            parent_environment=parent,
            registry=components,
        )
    assert raised.value.code == "wsl_mingw_target_inputs_invalid"
    assert header.read_bytes() == original + b"\n"
