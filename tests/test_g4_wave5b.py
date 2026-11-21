from pathlib import Path
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.network import zmq
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave5b import (
    G4_WAVE5B_COMPONENTS,
    Wave2TargetValidator,
    Wave5BTargetValidator,
    build_or_reuse,
    select_components,
    verify_source,
)
from ffmpeg_builder.state import ComponentStatus


def test_cross_libzmq_uses_shared_autotools_projection(tmp_path, monkeypatch):
    calls = []
    header = tmp_path / "src" / "polling_util.hpp"
    header.parent.mkdir()
    header.write_text("#include <vector>\n")
    projection = {
        "CC": "/target/clang",
        "CXX": "/target/clang++",
        "AR": "/target/ar",
        "RANLIB": "/target/ranlib",
        "STRIP": "/target/strip",
        "PKG_CONFIG": "/target/pkg-config",
        "PKG_CONFIG_PATH": "",
        "CFLAGS": "-march=native -mtune=native",
        "CXXFLAGS": "-march=native -mtune=native",
    }
    cross = SimpleNamespace(
        autotools=SimpleNamespace(
            arguments=["--build=x86_64-linux-gnu", "--host=x86_64-w64-mingw32"],
            environment=projection,
        )
    )
    builder = SimpleNamespace(
        get_build_env=lambda component: {"PKG_CONFIG_PATH": "/host-only"},
        platform_context=SimpleNamespace(cross_environment=cross),
        _ws_str=lambda: "/target",
        _run_step=lambda *args: calls.append(args),
        _run_make=lambda *args: calls.append(args),
        _assert_patch_absent=lambda *args: None,
        num_jobs=2,
    )
    monkeypatch.setattr(zmq, "_normalize_cross_metadata", lambda *args: None)
    zmq.build_libzmq(builder, ComponentRegistry().get_by_name("libzmq"), tmp_path)
    configure = calls[0][4]
    assert "--host=x86_64-w64-mingw32" in configure
    assert "--disable-shared" in configure and "--enable-static" in configure
    assert "--without-libsodium" in configure and "--disable-libunwind" in configure
    assert "--disable-libbsd" in configure
    assert "#include <new>" in header.read_text()
    assert all(calls[0][7][key] == value for key, value in projection.items())
    assert calls[-1][4] == [
        "make",
        "install-libLTLIBRARIES",
        "install-includeHEADERS",
        "install-pkgconfigDATA",
    ]


def test_source_and_selection_are_bounded(tmp_path):
    registry = ComponentRegistry()
    component = registry.get_by_name("libzmq")
    assert G4_WAVE5B_COMPONENTS == ("libzmq",)
    assert verify_source(registry, Path("third_party/sources"))["libzmq"] == component.sha256
    assert select_components(registry, [registry.get_by_name("srt"), component]) == [component]
    with pytest.raises(BuildError, match="exactly once"):
        select_components(registry, [component, component])
    with pytest.raises(BuildError, match="unavailable"):
        verify_source(registry, tmp_path)
    archive = tmp_path / component.get_archive_filename()
    archive.write_bytes(b"bad")
    with pytest.raises(BuildError, match="checksum"):
        verify_source(registry, tmp_path)
    archive.write_text("version https://git-lfs.github.com/spec/v1\n")
    with pytest.raises(BuildError, match="LFS"):
        verify_source(registry, tmp_path)


def test_target_projection_hides_host_only_optional_pkg_config(tmp_path):
    pkg_config = shutil.which("pkg-config")
    if not pkg_config:
        pytest.skip("pkg-config unavailable")
    host = tmp_path / "host"
    target = tmp_path / "target"
    host.mkdir()
    target.mkdir()
    (host / "libsodium.pc").write_text(
        "Name: libsodium\nDescription: host only\nVersion: 1.0\nLibs: -lsodium\n"
    )
    env = dict(os.environ, PKG_CONFIG_PATH=str(host), PKG_CONFIG_LIBDIR=str(target))
    assert subprocess.run([pkg_config, "--exists", "libsodium"], env=env).returncode == 0
    env.update(PKG_CONFIG_PATH="", PKG_CONFIG_LIBDIR=str(target))
    assert subprocess.run([pkg_config, "--exists", "libsodium"], env=env).returncode != 0


def test_cross_metadata_excludes_host_and_unused_openmp(tmp_path, monkeypatch):
    pkg = tmp_path / "lib/pkgconfig"
    pkg.mkdir(parents=True)
    pc = pkg / "libzmq.pc"
    la = tmp_path / "lib/libzmq.la"
    pc.write_text("Libs.private: -lstdc++ -lpthread\nCflags: -I${includedir}\n")
    la.write_text(
        "inherited_linker_flags=' -fopenmp'\ndependency_libs=' -lpthread -lrpcrt4 -lws2_32'\n"
    )
    monkeypatch.setattr(
        zmq.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="")
    )
    builder = SimpleNamespace(
        target_prefix=tmp_path,
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(target_tools=SimpleNamespace(nm="/target/nm"))
        ),
    )
    component = ComponentRegistry().get_by_name("libzmq")
    zmq._normalize_cross_metadata(builder, component)
    zmq._normalize_cross_metadata(builder, component)
    assert pc.read_text().count("-DZMQ_STATIC") == 1
    assert "-lstdc++" not in pc.read_text()
    assert all(
        flag in pc.read_text()
        for flag in ("-lc++", "-lunwind", "-lpthread", "-liphlpapi", "-lws2_32")
    )
    assert "-lrpcrt4" not in la.read_text() and "-fopenmp" not in la.read_text()
    monkeypatch.setattr(
        zmq.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="U __kmpc_fork_call"),
    )
    with pytest.raises(BuildError, match="OpenMP"):
        zmq._normalize_cross_metadata(builder, component)


def test_native_recipe_does_not_use_cross_normalization(tmp_path, monkeypatch):
    calls = []
    builder = SimpleNamespace(
        get_build_env=lambda item: {},
        platform_context=SimpleNamespace(cross_environment=None),
        platform="linux",
        _ws_str=lambda: "/native",
        num_jobs=1,
        _run_step=lambda *args: calls.append(args),
        _run_make=lambda *args: calls.append(args),
        _run_install=lambda *args: calls.append(args),
        _is_windows_ucrt64_backend=lambda: False,
    )
    monkeypatch.setattr(
        zmq, "_normalize_cross_metadata", lambda *args: pytest.fail("cross normalization")
    )
    zmq.build_libzmq(builder, ComponentRegistry().get_by_name("libzmq"), tmp_path)
    assert calls[0][4] == ["./configure", "--prefix=/native", "--disable-shared", "--enable-static"]


class Manager:
    def __init__(self):
        self.state = SimpleNamespace(components={})
        self.statuses = []

    def get(self):
        return self.state

    def mark_component_status(self, name, status, version, *args, **kwargs):
        self.statuses.append(status)
        self.state.components[name] = SimpleNamespace(status=status)


def validator_at(tmp_path):
    prefix, workspace = tmp_path / "prefix", tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    return Wave5BTargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )


@pytest.mark.parametrize("relative", ["lib/libzmq.a", "lib/pkgconfig/libzmq.pc"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_complete_state_does_not_reuse_missing_or_corrupt_output(tmp_path, relative, corrupt):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name("libzmq")
    output = validator.prefix / relative
    output.parent.mkdir(parents=True)
    output.write_bytes(b"accepted")
    validator.record_component(component, [relative])
    if corrupt:
        output.write_bytes(b"corrupt")
    else:
        output.unlink()
    manager = Manager()
    manager.state.components[component.name] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    with pytest.raises(BuildError, match="refusing reuse"):
        build_or_reuse(
            component,
            SimpleNamespace(build_component=lambda item: pytest.fail("rebuild")),
            manager,
            validator,
        )
    assert not manager.statuses


@pytest.mark.parametrize(
    "failure", ["configure", "build", "install", "metadata", "link", "runtime"]
)
def test_failure_never_accepts_and_preserves_prior_outputs(tmp_path, monkeypatch, failure):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name("libzmq")
    prior = validator.prefix / "lib/prior.a"
    prior.parent.mkdir()
    prior.write_bytes(b"accepted prior")
    own = validator.prefix / "lib/libzmq.a"
    manager = Manager()

    def build(item):
        own.write_bytes(b"partial")
        if failure in ("configure", "build", "install"):
            raise BuildError(item.name, failure)

    monkeypatch.setattr(
        validator,
        "validate_component",
        lambda item: (_ for _ in ()).throw(BuildError(item.name, failure)),
    )
    with pytest.raises(BuildError, match=failure):
        build_or_reuse(component, SimpleNamespace(build_component=build), manager, validator)
    assert manager.statuses[-1] == ComponentStatus.FAILED
    assert ComponentStatus.COMPLETED not in manager.statuses
    assert prior.read_bytes() == b"accepted prior" and not own.exists()


def test_valid_reuse_revalidates_without_rebuild(tmp_path, monkeypatch):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name("libzmq")
    target = validator.prefix / "lib/libzmq.a"
    target.parent.mkdir()
    target.write_bytes(b"accepted")
    validator.record_component(component, ["lib/libzmq.a"])
    manager = Manager()
    manager.state.components[component.name] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    checks = []
    monkeypatch.setattr(validator, "validate_component", lambda item: checks.append("component"))
    monkeypatch.setattr(validator, "validate_prefix", lambda: checks.append("prefix"))
    assert not build_or_reuse(
        component,
        SimpleNamespace(build_component=lambda item: pytest.fail("rebuild")),
        manager,
        validator,
    )
    assert checks == ["component", "prefix"]


def test_unowned_existing_output_is_not_overwritten(tmp_path):
    validator = validator_at(tmp_path)
    target = validator.prefix / "lib/libzmq.a"
    target.parent.mkdir()
    target.write_bytes(b"user data")
    with pytest.raises(BuildError, match="refusing to overwrite"):
        build_or_reuse(
            ComponentRegistry().get_by_name("libzmq"), SimpleNamespace(), Manager(), validator
        )
    assert target.read_bytes() == b"user data"


@pytest.mark.parametrize("runtime_status", [0, 1])
def test_target_link_and_windows_runtime_fail_closed(tmp_path, monkeypatch, runtime_status):
    validator = validator_at(tmp_path)
    tools = tmp_path / "toolchain/bin"
    runtime = tmp_path / "toolchain/x86_64-w64-mingw32/bin"
    tools.mkdir(parents=True)
    runtime.mkdir(parents=True)
    for dll in ("libc++.dll", "libunwind.dll", "libwinpthread-1.dll"):
        (runtime / dll).write_bytes(b"runtime")
    validator.environment.target_tools = SimpleNamespace(
        cc=tools / "clang", readobj=tools / "readobj"
    )
    validator.environment.paths.generated_files_root = tmp_path
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "wslpath":
            return SimpleNamespace(
                returncode=0, stdout=r"\\wsl.localhost\test\smoke.exe", stderr=""
            )
        if command[0] == str(tools / "readobj"):
            return SimpleNamespace(
                returncode=0,
                stdout="  Name: libc++.dll\n  Name: libunwind.dll\n  Name: libwinpthread-1.dll\n",
                stderr="",
            )
        required = {"-lc++", "-lpthread", "-liphlpapi", "-lws2_32"}
        missing = required - set(command)
        errors = {
            "-lc++": "undefined symbol: std::test",
            "-lpthread": "undefined symbol: pthread_self",
            "-liphlpapi": "undefined symbol: GetAdaptersAddresses",
            "-lws2_32": "undefined symbol: WSASocketA",
        }
        return SimpleNamespace(
            returncode=bool(missing),
            stdout="",
            stderr=errors[next(iter(missing))] if missing else "",
        )

    monkeypatch.setattr("ffmpeg_builder.g4_wave5b.subprocess.run", run)
    monkeypatch.setattr(Wave2TargetValidator, "_validate_executable", lambda *args: None)
    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave5b.WindowsInterop",
        lambda: SimpleNamespace(
            run_powershell=lambda *args, **kwargs: SimpleNamespace(
                returncode=runtime_status, stderr=b"runtime failure"
            )
        ),
    )
    component = ComponentRegistry().get_by_name("libzmq")
    flags = "-DZMQ_STATIC -lzmq -lc++ -lunwind -lpthread -liphlpapi -lws2_32"
    if runtime_status:
        with pytest.raises(BuildError, match="Windows runtime smoke failed"):
            validator._link_and_runtime(component, flags, {"CFLAGS": "-march=native -fopenmp"})
    else:
        validator._link_and_runtime(component, flags, {"CFLAGS": "-march=native -fopenmp"})
    links = [command for command in commands if command[0] == str(tools / "clang")]
    assert len(links) == 7 and all("-fopenmp" not in command for command in links)
    assert not list(tmp_path.glob("g4-wave5b-*"))


def test_host_archive_is_rejected(tmp_path, monkeypatch):
    validator = validator_at(tmp_path)
    validator.environment.target_tools = SimpleNamespace(readobj="/target/readobj")
    validator.environment.compiler_environment = {}
    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave3a.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Format: COFF-x86-64\nFormat: elf64-x86-64\n", stderr=""
        ),
    )
    with pytest.raises(BuildError, match="not all x86-64 Windows COFF"):
        validator._validate_archive("libzmq", tmp_path / "libzmq.a")


def test_current_windows_strategy_status_is_consistent():
    text = Path("docs/windows_build_strategy.md").read_text()
    assert "G1 **PASS**" in text
    assert "G2.1–G2.8 **PASS**" in text
    assert "G3 **PASS**" in text
    assert "G4 **PASS**" in text
    assert "G5 **PASS**" in text
    assert "G6 **PASS**; G7 CLI **PASS** on the recorded physical target." in text
    assert "Windows TUI integration remains separate and **not yet completed**." in text
    assert "G6 — accepted Windows runtime bundle" in text
    assert "Wave 0 complete; Wave 1 not started" not in text
    assert "G4 component adapters and full-stack builds have not started" not in text
