"""G3 Linux-to-Windows cross-environment contract and acceptance tests."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builder import FFmpegBuilder
from ffmpeg_builder.builders import base as shared_builders
from ffmpeg_builder.components import (
    BuildRole,
    BuildSystem,
    Component,
    ComponentCategory,
    ComponentRegistry,
)
from ffmpeg_builder.config import BuildConfig, TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.cuda_prerequisites import (
    resolve_wsl_mingw_cuda_inputs,
    validate_cuda_output_modes,
    validate_cuda_ptx_compatibility,
)
from ffmpeg_builder.cross_environment import (
    CPU_TUNING_FLAGS,
    DeferredCudaHostTools,
    _cuda_host_environment,
    _host_environment,
    _host_tool_path,
    _resolve_target_tools,
    _target_interface_projection,
    _validate_cuda_host_tools,
    _validate_cpu_tuning,
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


def _fake_tuning_compiler(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.environ['TUNING_PROBE_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(1 if '-x' in sys.argv and sys.argv[sys.argv.index('-x') + 1] == "
        "os.environ.get('REJECT_LANGUAGE') else 0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_cpu_tuning_probe_uses_exact_flags_for_c_and_cxx(tmp_path):
    log = tmp_path / "compiler-args.txt"
    compiler = _fake_tuning_compiler(tmp_path / "clang")
    _validate_cpu_tuning(
        SimpleNamespace(cc=compiler, cxx=compiler),
        CPU_TUNING_FLAGS,
        {"TUNING_PROBE_LOG": str(log)},
    )

    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert calls[0].split() == [
        *CPU_TUNING_FLAGS,
        "-fsyntax-only",
        "-x",
        "c",
        "-",
    ]
    assert calls[1].split() == [
        *CPU_TUNING_FLAGS,
        "-fsyntax-only",
        "-x",
        "c++",
        "-",
    ]
    assert CPU_TUNING_FLAGS == ("-march=native", "-mtune=native")


def test_cpu_tuning_probe_reports_selected_compiler_rejection(tmp_path):
    log = tmp_path / "compiler-args.txt"
    compiler = _fake_tuning_compiler(tmp_path / "clang")
    with pytest.raises(BuildError) as raised:
        _validate_cpu_tuning(
            SimpleNamespace(cc=compiler, cxx=compiler),
            CPU_TUNING_FLAGS,
            {"TUNING_PROBE_LOG": str(log), "REJECT_LANGUAGE": "c++"},
        )

    assert raised.value.code == "wsl_mingw_cpu_tuning_probe_failed"
    assert "LLVM-MinGW C++ compiler probe" in str(raised.value)
    assert "-march=native -mtune=native" in str(raised.value)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_host_environment_drops_target_projections_and_native_tuning():
    projected = _host_environment(
        {
            "PATH": "/usr/bin",
            "CC": "/target/bin/clang",
            "CFLAGS": "-O2 -march=native -mtune=native",
            "CXXFLAGS": "-O3 -march=native -mtune=native",
            "RUSTFLAGS": "-C target-cpu=native -C debuginfo=1",
            "PKG_CONFIG_LIBDIR": "/target/lib/pkgconfig",
            "CUDA_HOME": "/opt/cuda",
            "CMAKE_TOOLCHAIN_FILE": "/target/toolchain.cmake",
        }
    )

    assert projected["PATH"] == "/usr/bin"
    assert "CFLAGS" not in projected
    assert "CXXFLAGS" not in projected
    assert "RUSTFLAGS" not in projected
    assert not {"CC", "PKG_CONFIG_LIBDIR", "CUDA_HOME", "CMAKE_TOOLCHAIN_FILE"} & set(projected)


def test_host_tool_path_prioritizes_resolved_host_tool_directories():
    path = _host_tool_path(
        {"cmake": "/opt/host-tools/bin/cmake", "ninja": "/usr/bin/ninja"},
        "/usr/local/bin:/usr/bin:/opt/cuda/bin",
        ("/opt/cuda",),
    )

    assert path.split(os.pathsep) == ["/opt/host-tools/bin", "/usr/bin", "/usr/local/bin"]


def test_cuda_host_environment_is_scoped_and_never_uses_target_flags(tmp_path):
    cuda_root = tmp_path / "cuda"
    nvcc = cuda_root / "bin" / "nvcc"
    nvcc.parent.mkdir(parents=True)
    nvcc.write_text("", encoding="utf-8")
    ccbin = tmp_path / "toolchain" / "bin" / "gcc"
    ccbin.parent.mkdir(parents=True)
    ccbin.write_text("", encoding="utf-8")
    tools = DeferredCudaHostTools(
        nvcc=nvcc,
        ccbin=ccbin,
        common_flags=(f"-ccbin={ccbin}",),
        compute_architecture="compute_70",
    )
    preparation = SimpleNamespace(cuda=SimpleNamespace(cuda_root=cuda_root))

    projected = _cuda_host_environment(
        tools,
        preparation,
        {"PATH": "/usr/bin", "CFLAGS": "-march=native", "CC": "/target/clang"},
    )

    assert projected["CUDA_HOME"] == str(cuda_root)
    assert projected["CUDACXX"] == str(nvcc)
    assert projected["CUDA_NVCC_CCBIN"] == str(ccbin)
    assert projected["NVCC_PREPEND_FLAGS"] == f"-ccbin={ccbin}"
    assert "CFLAGS" not in projected
    assert "CC" not in projected
    assert "-march=native" not in projected["NVCC_PREPEND_FLAGS"]


def test_cuda_consumer_probe_checks_ptx_fatbin_and_selected_bin2c(tmp_path, monkeypatch):
    nvcc = tmp_path / "nvcc"
    bin2c = tmp_path / "bin2c"
    nvcc.touch()
    bin2c.touch()
    tools = DeferredCudaHostTools(
        nvcc=nvcc,
        ccbin=tmp_path / "gcc",
        toolkit_release="12.4",
        common_flags=("-ccbin=/toolchain/bin/gcc",),
        compute_architecture="compute_70",
        bin2c=bin2c,
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == str(bin2c):
            return SimpleNamespace(
                returncode=0,
                stdout="const unsigned char g3_probe[] = {0x01};\n",
                stderr="",
            )
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_text("probe", encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="COFF-x86-64\nMagic: 0x20B",
            stderr="",
        )

    monkeypatch.setattr("ffmpeg_builder.cross_environment.subprocess.run", fake_run)
    outcome = _validate_cuda_host_tools(
        tools,
        {"PATH": "/usr/bin"},
        tmp_path,
        SimpleNamespace(
            cc=tmp_path / "clang",
            readobj=tmp_path / "readobj",
            objdump=tmp_path / "objdump",
        ),
        {"PATH": "/usr/bin", "CFLAGS": "-march=native -mtune=native"},
    )

    assert len(calls) == 8
    assert "--ptx" in calls[0]
    assert "--fatbin" in calls[1]
    assert calls[2][0] == str(bin2c)
    assert all("-ccbin=/toolchain/bin/gcc" in call for call in calls[:2])
    assert "-march=native" not in " ".join(arg for call in calls[:3] for arg in call)
    assert outcome["target_object"] == "COFF-x86-64"
    assert outcome["target_executable"] == "PE32+ x86-64"


def test_target_interface_projection_separates_sdk_shader_tools(tmp_path):
    shader = tmp_path / "sdk/bin/glslc"
    validator = tmp_path / "sdk/bin/spirv-val"
    projection = _target_interface_projection(
        tmp_path / "prefix",
        SimpleNamespace(
            vulkan=SimpleNamespace(
                shader_compiler_path=shader,
                spirv_validator_path=validator,
                spirv_optimizer_path=None,
            )
        ),
    )

    assert projection.vulkan_headers == tmp_path / "prefix/include/vulkan"
    assert projection.vulkan_import_library == tmp_path / "prefix/lib/libvulkan.a"
    assert projection.opencl_import_library == tmp_path / "prefix/lib/libOpenCL.a"
    assert projection.host_shader_tools == {
        "shader_compiler": str(shader),
        "spirv_validator": str(validator),
    }


def test_builder_selects_isolated_host_target_and_cargo_projections(tmp_path):
    workspace = tmp_path / "identity"
    target_prefix = workspace / "prefix"
    cross = SimpleNamespace(
        backend=TARGET_BACKEND_CROSS_MINGW,
        paths=SimpleNamespace(
            workspace_root=workspace,
            target_prefix=target_prefix,
            generated_files_root=workspace / "generated",
        ),
        host_environment={"PATH": "/usr/bin", "CFLAGS": "-O2"},
        compiler_environment={
            "PATH": "/usr/bin",
            "CC": "/llvm-mingw/bin/clang",
            "CFLAGS": f"-I{target_prefix / 'include'}",
        },
        rust_environment={
            "RUSTC": "/project-rust/bin/rustc",
            "CARGO": "/project-rust/bin/cargo",
            "CARGO_BUILD_TARGET": "x86_64-pc-windows-gnullvm",
        },
        cargo_environment={
            "RUSTC": "/project-rust/bin/rustc",
            "CARGO": "/project-rust/bin/cargo",
            "CARGO_BUILD_TARGET": "x86_64-pc-windows-gnullvm",
            "CARGO_HOME": str(workspace / "generated" / "cargo-home"),
            "CFLAGS_x86_64_pc_windows_gnullvm": "-march=native -mtune=native",
        },
        rust_tools=SimpleNamespace(cargo=Path("/project-rust/bin/cargo")),
    )
    builder = object.__new__(FFmpegBuilder)
    builder.platform_strategy = LinuxCrossMingwPlatform()
    builder.platform_context = SimpleNamespace(cross_environment=cross)
    builder.toolchain_env = {}

    host = Component(
        "generator",
        "1",
        "https://example.org/generator.tar.gz",
        ComponentCategory.BUILD_TOOL,
        BuildSystem.CMAKE,
        build_role=BuildRole.HOST_TOOL,
    )
    target = Component(
        "library",
        "1",
        "https://example.org/library.tar.gz",
        ComponentCategory.OTHER_LIB,
        BuildSystem.CMAKE,
    )
    cargo = Component(
        "rust-library",
        "1",
        "https://example.org/rust.tar.gz",
        ComponentCategory.OTHER_LIB,
        BuildSystem.CARGO,
    )
    mixed = Component(
        "mixed",
        "1",
        "https://example.org/mixed.tar.gz",
        ComponentCategory.OTHER_LIB,
        BuildSystem.CUSTOM,
        build_role=BuildRole.MIXED,
    )

    host_env = builder.get_build_env(host)
    target_env = builder.get_build_env(target)
    cargo_env = builder.get_build_env(cargo)

    assert builder._ws_str(host) == str(workspace / "host-prefix")
    assert builder._ws_str(target) == str(target_prefix)
    assert "CC" not in host_env and target_env["CC"] == "/llvm-mingw/bin/clang"
    assert host_env["CMAKE_PREFIX_PATH"] == str(workspace / "host-prefix")
    assert "PKG_CONFIG_LIBDIR" not in host_env
    assert "-march=native" not in host_env.get("CFLAGS", "")
    assert str(target_prefix) not in host_env.get("CMAKE_PREFIX_PATH", "")
    assert cargo_env["CARGO_BUILD_TARGET"] == "x86_64-pc-windows-gnullvm"
    assert "RUSTFLAGS" not in cargo_env
    assert "CFLAGS" not in cargo_env
    assert cargo_env["CARGO_HOME"] == str(workspace / "generated" / "cargo-home")
    with pytest.raises(BuildError, match="separately"):
        builder.get_build_env(mixed)


def test_shared_builder_helpers_emit_scoped_cross_commands(tmp_path, monkeypatch):
    workspace = tmp_path / "identity"
    target_prefix = workspace / "prefix"
    target_cc = "/llvm-mingw/bin/x86_64-w64-mingw32-clang"
    target_cxx = "/llvm-mingw/bin/x86_64-w64-mingw32-clang++"
    toolchain_file = workspace / "generated/cmake-toolchain.cmake"
    meson_file = workspace / "generated/meson-cross.ini"
    pkg_config = workspace / "generated/pkg-config"
    target_env = {
        "PATH": "/usr/bin:/bin",
        "CC": target_cc,
        "CXX": target_cxx,
        "AR": "/llvm-mingw/bin/llvm-ar",
        "RANLIB": "/llvm-mingw/bin/llvm-ranlib",
        "STRIP": "/llvm-mingw/bin/llvm-strip",
        "CFLAGS": f"-march=native -mtune=native -I{target_prefix / 'include'}",
        "CXXFLAGS": f"-march=native -mtune=native -I{target_prefix / 'include'}",
        "LDFLAGS": f"-L{target_prefix / 'lib'}",
        "PKG_CONFIG": str(pkg_config),
        "CMAKE_PREFIX_PATH": str(target_prefix),
    }
    cargo_env = {
        "PATH": "/usr/bin:/pinned/bin",
        "RUSTC": "/project-rust/bin/rustc",
        "CARGO": "/project-rust/bin/cargo",
        "CARGO_BUILD_TARGET": "x86_64-pc-windows-gnullvm",
        "CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER": target_cc,
        "CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_AR": "/llvm-mingw/bin/llvm-ar",
        "CARGO_HOME": str(workspace / "generated/cargo-home"),
        "CARGO_TARGET_DIR": str(workspace / "build/cargo-target"),
        "PKG_CONFIG": str(pkg_config),
        "CARGO_NET_OFFLINE": "true",
    }
    cross = SimpleNamespace(
        backend=TARGET_BACKEND_CROSS_MINGW,
        paths=SimpleNamespace(
            workspace_root=workspace,
            target_prefix=target_prefix,
            generated_files_root=workspace / "generated",
        ),
        host_environment={"PATH": "/usr/bin:/bin"},
        compiler_environment=target_env,
        cargo_environment=cargo_env,
        autotools=SimpleNamespace(
            arguments=("--build=x86_64-linux-gnu", "--host=x86_64-w64-mingw32"),
            environment={
                **target_env,
                "CPPFLAGS": f"-I{target_prefix / 'include'}",
                "LIBS": "",
            },
        ),
        cmake_toolchain_file=toolchain_file,
        meson_cross_file=meson_file,
        rust_tools=SimpleNamespace(
            cargo=Path(cargo_env["CARGO"]),
            cargo_cinstall=Path("/pinned/bin/cargo-cinstall"),
        ),
    )
    builder = object.__new__(FFmpegBuilder)
    builder.platform_strategy = LinuxCrossMingwPlatform()
    builder.platform_context = SimpleNamespace(cross_environment=cross)
    builder.platform_detector = SimpleNamespace(platform_info=SimpleNamespace(cuda_path=None))
    builder.toolchain_env = {}
    builder.config = BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW, native_build=True)
    builder.env = {}
    builder.platform = "linux"
    builder.workspace = workspace
    builder.num_jobs = 4
    builder._active_build_component = None
    calls = []

    def capture_step(context, component, status, detail, error, command, name, workdir, env):
        calls.append((component.name, name, command, dict(env)))
        if component.build_system == BuildSystem.CARGO:
            stage = Path(command[command.index("--destdir") + 1])
            output = stage / target_prefix.relative_to("/") / "lib" / "test.a"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"target")

    def capture_make(context, component, status, detail, error, workdir, jobs, env, timeout=None):
        calls.append((component.name, "make", [f"make -j{jobs}"], dict(env)))

    def capture_install(context, component, status, detail, error, workdir, env):
        calls.append((component.name, "install", ["make", "install"], dict(env)))

    monkeypatch.setattr(shared_builders, "run_step", capture_step)
    monkeypatch.setattr(shared_builders, "run_make", capture_make)
    monkeypatch.setattr(shared_builders, "run_install", capture_install)
    monkeypatch.setattr(shared_builders, "get_rustc_version", lambda *args: (1, 99, 0))
    monkeypatch.setattr(
        shared_builders.shutil,
        "which",
        lambda name, *args, **kwargs: (
            f"{kwargs['path']}/cargo-cinstall" if name == "cargo-cinstall" else None
        ),
    )
    context = SimpleNamespace(
        builder=builder,
        executor=SimpleNamespace(
            execute=lambda command, **kwargs: SimpleNamespace(
                success=True,
                stdout=(
                    f"cargo-c {shared_builders.CARGO_C_VERSION}"
                    if command[0].startswith("/pinned/")
                    else "cargo-c 0.10.16"
                ),
            )
        ),
        state_manager=None,
    )
    sources = tmp_path / "sources"
    sources.mkdir()
    cross.paths.generated_files_root.mkdir(parents=True)
    components = {
        system: Component(
            f"g3-{system.value}",
            "1",
            "https://invalid.example/source.tar.gz",
            ComponentCategory.OTHER_LIB,
            system,
            configure_args=("-DCMAKE_INSTALL_PREFIX={workspace}", "--prefix={workspace}"),
            build_args=("TARGET=all",),
        )
        for system in (
            BuildSystem.AUTOTOOLS,
            BuildSystem.CMAKE,
            BuildSystem.MESON,
            BuildSystem.MAKE_ONLY,
        )
    }
    components[BuildSystem.CARGO] = Component(
        "g3-cargo",
        "1",
        "https://invalid.example/cargo.tar.gz",
        ComponentCategory.OTHER_LIB,
        BuildSystem.CARGO,
    )

    shared_builders.build_autotools(context, components[BuildSystem.AUTOTOOLS], sources)
    shared_builders.build_cmake(context, components[BuildSystem.CMAKE], sources)
    shared_builders.build_meson(context, components[BuildSystem.MESON], sources)
    shared_builders.build_make_only(context, components[BuildSystem.MAKE_ONLY], sources)
    shared_builders.build_cargo(context, components[BuildSystem.CARGO], sources)

    autotools = next(call for call in calls if call[0] == "g3-autotools" and call[1] == "configure")
    assert autotools[2][0] == "./configure"
    assert "--build=x86_64-linux-gnu" in autotools[2]
    assert "--host=x86_64-w64-mingw32" in autotools[2]
    assert autotools[3]["CC"] == target_cc
    assert autotools[3]["PKG_CONFIG"] == str(pkg_config)
    cmake = next(call for call in calls if call[0] == "g3-cmake" and call[1] == "configure")
    assert f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}" in cmake[2]
    assert f"-DCMAKE_INSTALL_PREFIX={target_prefix}" in cmake[2]
    meson = next(call for call in calls if call[0] == "g3-meson" and call[1] == "configure")
    assert meson[2][:3] == ["meson", "setup", "build"]
    assert ["--cross-file", str(meson_file)] == meson[2][3:5]
    make = next(call for call in calls if call[0] == "g3-make_only" and call[1] == "build")
    assert make[3]["CC"] == target_cc
    assert make[3]["CFLAGS"].split()[:2] == ["-march=native", "-mtune=native"]
    cargo_build = next(call for call in calls if call[0] == "g3-cargo" and call[1] == "build")
    assert not any(call[1] == "install-cargo-c" for call in calls)
    assert cargo_build[3]["CARGO_BUILD_TARGET"] == "x86_64-pc-windows-gnullvm"
    assert cargo_build[3]["CARGO_TARGET_DIR"] == str(workspace / "build/cargo-target")
    assert cargo_build[2][0] == "/pinned/bin/cargo-cinstall"
    assert "--locked" in cargo_build[2]
    assert cargo_build[2][cargo_build[2].index("--target") + 1] == "x86_64-pc-windows-gnullvm"
    assert cargo_build[3]["CARGO_NET_OFFLINE"] == "true"
    assert cargo_build[3]["CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_RUSTFLAGS"] == (
        "-C target-cpu=native"
    )
    assert "RUSTFLAGS" not in cargo_build[3]
    assert all(
        "-march=native" not in " ".join(call[2]) or call[3].get("CC") == target_cc for call in calls
    )
    assert all("MSYS" not in value and "sh.exe" not in value for call in calls for value in call[2])


def test_headless_cross_route_uses_normal_production_workflow(monkeypatch):
    app = object()
    calls = []
    monkeypatch.setattr(
        "ffmpeg_builder.scripts.run_build.run_cross_build",
        lambda actual_app, config, **options: calls.append((actual_app, config, options)) or 7,
    )
    config = BuildConfig(target_backend=TARGET_BACKEND_CROSS_MINGW, cuda_build_mode="disabled")
    status = run_build(
        app,
        config,
        resume=False,
        no_release=True,
    )

    assert status == 7
    assert calls == [(app, config, {"resume": False, "no_release": True})]


def test_headless_cross_dry_run_reports_wave_zero_selection_without_building(capsys):
    def component(name, version="1"):
        return SimpleNamespace(name=name, version=version)

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

        def _get_component_selection(self, config):
            return SimpleNamespace(
                host_tools=[component("cmake", "4.4.3")],
                system_satisfied_host_requirements=["cmake"],
                missing_host_tools=[],
                target_inputs=[component("vulkan-headers")],
                target_components_to_build=[component("zlib", "1.3.2")],
                mixed_components=[component("mixed-generator")],
                conditional_components=[component("amf")],
                skipped_by_feature=[component("libvmaf")],
                skipped_by_backend=[],
            )

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
    assert "Dry-run passed: G3 cross-environment acceptance passed" in output
    assert "HOST TOOL cmake: satisfied" in output
    assert "TARGET INPUT vulkan-headers" in output
    assert "TARGET BUILD zlib" in output
    assert "MIXED mixed-generator" in output
    assert "CONDITIONAL amf" in output
    assert "SKIPPED BY FEATURE libvmaf" in output
    assert "no production component was built" in output


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
        native_build=True,
    )
    parent = dict(os.environ)
    parent.update(
        {
            "CC": "/usr/bin/gcc",
            "CXX": "/usr/bin/g++",
            "CFLAGS": "-march=skylake -mtune=native -mavx2",
            "CXXFLAGS": "-march=skylake -mtune=native -mavx2",
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
    assert environment.compiler_environment["PATH"] == environment.host_environment["PATH"]
    host_path_entries = environment.compiler_environment["PATH"].split(os.pathsep)
    assert host_path_entries[0] == "/usr/bin"
    assert host_path_entries[-1] == "/bin"
    assert all(
        str(Path(tool).parent) in host_path_entries
        for tool in environment.host_tools.paths.values()
    )
    assert "PKG_CONFIG_PATH" not in environment.compiler_environment
    cargo_env = environment.cargo_environment
    assert cargo_env["HOST_PKG_CONFIG"] == environment.host_tools.paths["pkg-config"]
    assert cargo_env["TARGET_PKG_CONFIG"] == str(environment.pkg_config_wrapper)
    assert "PKG_CONFIG_LIBDIR" not in cargo_env
    assert (
        cargo_env["TARGET_PKG_CONFIG_LIBDIR"]
        == environment.compiler_environment["PKG_CONFIG_LIBDIR"]
    )
    for name in ("CUDA_HOME", "VULKAN_SDK", "CPATH", "LD_LIBRARY_PATH"):
        assert name not in environment.compiler_environment
    assert environment.cpu_tuning_flags == CPU_TUNING_FLAGS
    for name in ("CFLAGS", "CXXFLAGS"):
        assert environment.compiler_environment[name].split() == [
            *CPU_TUNING_FLAGS,
            "-fopenmp",
            f"-I{environment.paths.target_prefix / 'include'}",
        ]
        assert environment.autotools.environment[name].split() == [
            *CPU_TUNING_FLAGS,
            "-fopenmp",
            f"-I{environment.paths.target_prefix / 'include'}",
        ]
    assert environment.autotools.environment["CFLAGS"] == environment.compiler_environment["CFLAGS"]
    assert (
        environment.autotools.environment["CXXFLAGS"]
        == environment.compiler_environment["CXXFLAGS"]
    )
    assert "-march=native" not in environment.cmake_toolchain_file.read_text(encoding="utf-8")
    meson_cross = environment.meson_cross_file.read_text(encoding="utf-8")
    assert "c_args =" not in meson_cross
    assert "cpp_args =" not in meson_cross
    assert all("-march=native" not in value for value in environment.host_tools.paths.values())
    assert "-march=native" not in " ".join(environment.cuda_host_tools.common_flags)
    assert all("-march=native" not in value for value in environment.rust_environment.values())
    assert environment.autotools.build_tuple != environment.autotools.host_tuple
    assert environment.rust_tools.target == "x86_64-pc-windows-gnullvm"
    assert environment.paths.target_prefix == workspace / "prefix"
    assert environment.pkg_config_wrapper.is_file()
    portable = construct_cross_environment(
        preparation,
        BuildConfig(
            target_backend=TARGET_BACKEND_CROSS_MINGW,
            gpl_enabled=True,
            enable_hardware_acceleration=False,
            cuda_build_mode="disabled",
            native_build=False,
            openmp=False,
        ),
        tmp_path / "portable-workspace",
        PROJECT_ROOT / "third_party" / "sources",
        parent_environment=parent,
        registry=components,
    )
    assert portable.cpu_tuning_flags == ()
    for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        flags = portable.compiler_environment[key].split()
        assert "-march=native" not in flags
        assert "-mtune=native" not in flags
        assert "-march=skylake" not in flags
        assert "-mavx2" not in flags
        assert "-fopenmp" not in flags
    assert environment.cmake_toolchain_file.is_file()
    assert environment.meson_cross_file.is_file()

    generic_acceptance = run_cross_environment_acceptance(portable)
    assert generic_acceptance["cmake"]["built"] is True
    assert generic_acceptance["meson"]["built"] is True
    acceptance = run_cross_environment_acceptance(environment)

    assert set(acceptance) == {
        "pkg_config",
        "compiler",
        "cmake",
        "meson",
        "autotools",
        "builder_roles",
        "mixed_role",
        "cargo",
        "transitive_cpp_link",
        "cuda",
    }
    assert acceptance["pkg_config"]["host_fallback_blocked"] is True
    assert acceptance["pkg_config"]["malicious_path_rejected"]
    assert acceptance["compiler"]["ucrt_import"] == "api-ms-win-crt-*"
    assert acceptance["cmake"]["built"] is True
    assert acceptance["cmake"]["host_only_package_blocked"] is True
    assert acceptance["meson"]["host_machine"]["system"] == "windows"
    assert acceptance["meson"]["build_machine"]["system"] == "linux"
    assert acceptance["autotools"]["cross_compiling"] is True
    assert acceptance["builder_roles"]["msys_semantics"] is False
    assert acceptance["mixed_role"]["target_object"] == "COFF-x86-64"
    assert acceptance["cargo"]["target"] == "x86_64-pc-windows-gnullvm"
    assert acceptance["cargo"]["staticlib"] == "COFF-x86-64"
    assert acceptance["cargo"]["c_harness"] == "PE32+ x86-64"
    assert "-lc++" in acceptance["transitive_cpp_link"]["transitive_requirements"]
    assert acceptance["transitive_cpp_link"]["positive_link"] == "PE32+ x86-64"
    cuda_root = Path("/opt/cuda-12.4")
    if (cuda_root / "bin" / "nvcc").is_file():
        cuda_config = BuildConfig(
            target_backend=TARGET_BACKEND_CROSS_MINGW,
            cuda_build_mode="required",
            external_prerequisite_paths={"cuda": str(cuda_root)},
            cuda_compute_capability="compute_70",
        )
        cuda_inputs = resolve_wsl_mingw_cuda_inputs(
            cuda_config,
            tool_registry,
            store,
            required=True,
            environment={"CUDA_HOME": str(cuda_root)},
        )
        assert cuda_inputs is not None
        cuda_inputs = validate_cuda_ptx_compatibility(cuda_inputs, None, architecture="compute_70")
        cuda_inputs = validate_cuda_output_modes(cuda_inputs)
        cuda_preparation = SimpleNamespace(
            project_toolchains=toolchains,
            identity_summary={
                "target_input_materialization_id": identity_manifest["materialization_id"]
            },
            cuda=cuda_inputs,
            vulkan=None,
            diagnostic_only=preparation.diagnostic_only,
        )
        cuda_environment = construct_cross_environment(
            cuda_preparation,
            cuda_config,
            workspace,
            PROJECT_ROOT / "third_party" / "sources",
            parent_environment=parent,
            registry=components,
        )
        acceptance["cuda"] = cuda_environment.cuda_acceptance
        assert cuda_environment.cuda_host_tools.device_mode_flags("fatbin")[0] == "--fatbin"
        assert "--ptx" not in cuda_environment.cuda_host_tools.device_mode_flags("fatbin")
        assert "CUDA_HOME" not in cuda_environment.compiler_environment
        assert acceptance["cuda"]["target_object"] == "COFF-x86-64"
        assert acceptance["cuda"]["target_executable"] == "PE32+ x86-64"
    else:
        assert acceptance["cuda"]["configured"] is False
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


def test_cargo_c_resolves_only_from_verified_store_prefix(tmp_path):
    from ffmpeg_builder.cross_environment import _resolve_cargo_c

    prefix = tmp_path / "store/cargo-c-0.10.25"
    prefix.mkdir(parents=True)
    tool = prefix / "cargo-cinstall"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    stale = tmp_path / "usr/bin/cargo-cinstall"
    stale.parent.mkdir(parents=True)
    stale.write_text("#!/bin/sh\n")
    stale.chmod(0o755)

    def preparation(path):
        profile = {"prefix": str(prefix), "paths": {"CARGO_CINSTALL": str(path)}}
        return SimpleNamespace(project_toolchains={"cargo-c": profile})

    assert _resolve_cargo_c(preparation(tool)) == tool
    with pytest.raises(BuildError) as missing:
        _resolve_cargo_c(SimpleNamespace(project_toolchains={}))
    assert missing.value.code == "wsl_mingw_cargo_c_toolchain_missing"
    for path in (stale, prefix / "absent"):
        with pytest.raises(BuildError) as invalid:
            _resolve_cargo_c(preparation(path))
        assert invalid.value.code == "wsl_mingw_cargo_c_toolchain_invalid"
