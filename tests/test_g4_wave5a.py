from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.crypto import openssl
from ffmpeg_builder.builders.network import srt
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave5a import (
    G4_WAVE5A_COMPONENTS,
    Wave5ATargetValidator,
    build_or_reuse,
    run_wave_components,
    select_components,
    verify_sources,
)
from ffmpeg_builder.state import ComponentStatus


def test_registry_graph_and_exact_wave_selection():
    registry = ComponentRegistry()
    components = {item.name: item for item in registry.get_all()}
    assert components["openssl"].depends_on == ["zlib"]
    assert components["srt"].depends_on == ["openssl"]
    seen, active = set(), set()

    def visit(name):
        assert name not in active
        if name in seen:
            return
        active.add(name)
        for dependency in components[name].depends_on:
            assert dependency in components
            visit(dependency)
        active.remove(name)
        seen.add(name)

    for name in components:
        visit(name)
    selected = [components["srt"], components["x264"], components["openssl"]]
    assert G4_WAVE5A_COMPONENTS == ("openssl", "srt")
    assert [item.name for item in select_components(registry, selected)] == ["openssl", "srt"]
    with pytest.raises(BuildError, match="not configured"):
        select_components(registry, [components["srt"]])


def test_registered_sources_match_and_missing_sources_fail(tmp_path):
    registry = ComponentRegistry()
    assert set(verify_sources(registry, Path("third_party/sources"))) == {"openssl", "srt"}
    with pytest.raises(BuildError, match="unavailable"):
        verify_sources(registry, tmp_path)
    (tmp_path / "openssl-3.6.4.tar.gz").write_bytes(b"bad")
    with pytest.raises(BuildError, match="checksum mismatch"):
        verify_sources(registry, tmp_path)


@pytest.mark.parametrize("module,name", [(openssl, "openssl"), (srt, "srt")])
def test_target_dependency_preflight_rejects_host_only(tmp_path, module, name):
    cross = SimpleNamespace(paths=SimpleNamespace(target_prefix=tmp_path))
    builder = SimpleNamespace(
        get_build_env=lambda item: {"PATH": "/usr/bin"},
        platform_context=SimpleNamespace(cross_environment=cross),
    )
    with pytest.raises(BuildError, match="Required target"):
        getattr(module, "build_" + name)(builder, ComponentRegistry().get_by_name(name), tmp_path)


def test_openssl_cross_projection_and_native_untouched(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    for name in ("include/zlib.h", "include/zconf.h", "lib/libz.a", "lib/pkgconfig/zlib.pc"):
        path = prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    calls = []
    cross = SimpleNamespace(
        paths=SimpleNamespace(target_prefix=prefix),
        host_tools={"perl": "/usr/bin/perl"},
        autotools=SimpleNamespace(
            environment={"CC": "/target/cc", "CFLAGS": "-march=native -mtune=native"}
        ),
        target_tools=SimpleNamespace(windres="/target/windres"),
    )
    builder = SimpleNamespace(
        get_build_env=lambda item: {"PATH": "/usr/bin"},
        platform_context=SimpleNamespace(cross_environment=cross),
        _run_step=lambda *args: calls.append(args),
        _run_make=lambda *args: calls.append(args),
        num_jobs=2,
    )
    openssl.build_openssl(builder, ComponentRegistry().get_by_name("openssl"), tmp_path)
    configure = calls[0][4]
    assert configure[:3] == ["/usr/bin/perl", "Configure", "mingw64"]
    assert "zlib" in configure and "no-shared" in configure and "no-module" in configure
    assert "no-asm" not in configure
    assert f"--with-zlib-lib={prefix}/lib/libz.a" in configure
    assert calls[0][7]["CC"] == "/target/cc"
    assert calls[0][7]["RC"] == "/target/windres"
    assert "-march=native -mtune=native" == calls[0][7]["CFLAGS"]


def test_srt_cross_uses_target_toolchain_and_excludes_script(tmp_path, monkeypatch):
    prefix, source = tmp_path / "prefix", tmp_path / "source"
    source.mkdir()
    for name in (
        "include/openssl/ssl.h",
        "lib/libssl.a",
        "lib/libcrypto.a",
        "lib/pkgconfig/openssl.pc",
        "lib/pkgconfig/libcrypto.pc",
    ):
        path = prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    cmake = source / "CMakeLists.txt"
    cmake.write_text(
        "if(NOT NEED_DESTINATION)\n\tinstall(PROGRAMS scripts/srt-ffplay TYPE BIN)\n"
        "elseif (DEFINED CMAKE_INSTALL_BINDIR)\n\tinstall(PROGRAMS scripts/srt-ffplay DESTINATION ${CMAKE_INSTALL_BINDIR})\n"
        'else()\n\tmessage(WARNING "No location to install scripts/srt-ffplay")\nendif()\n'
    )
    calls = []
    cross = SimpleNamespace(
        paths=SimpleNamespace(target_prefix=prefix),
        compiler_environment={"CC": "/target/cc"},
        cmake_toolchain_file="/target/toolchain.cmake",
    )
    builder = SimpleNamespace(
        get_build_env=lambda item: {},
        platform_context=SimpleNamespace(cross_environment=cross),
        num_jobs=2,
        _run_step=lambda *args: calls.append(args),
    )
    monkeypatch.setattr(srt, "_normalize_cross_metadata", lambda *args: None)
    srt.build_srt(builder, ComponentRegistry().get_by_name("srt"), source)
    command = calls[0][4]
    assert "-DCMAKE_TOOLCHAIN_FILE=/target/toolchain.cmake" in command
    assert "-DENABLE_ENCRYPTION=ON" in command and "-DUSE_ENCLIB=openssl-evp" in command
    assert "-DENABLE_SHARED=OFF" in command and "-DUSE_STATIC_LIBSTDCXX=OFF" in command
    assert "srt-ffplay" not in cmake.read_text()
    assert calls[0][7]["CC"] == "/target/cc"
    srt.build_srt(builder, ComponentRegistry().get_by_name("srt"), source)


def test_srt_metadata_removes_only_unreferenced_openmp(tmp_path, monkeypatch):
    prefix = tmp_path
    directory = prefix / "lib/pkgconfig"
    directory.mkdir(parents=True)
    for name in ("srt.pc", "haisrt.pc"):
        (directory / name).write_text("Libs.private: -lpthread -lc++ -lomp -lunwind\n")
    builder = SimpleNamespace(
        target_prefix=prefix,
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(target_tools=SimpleNamespace(nm="/target/nm"))
        ),
    )
    monkeypatch.setattr(
        srt.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="")
    )
    srt._normalize_cross_metadata(builder, ComponentRegistry().get_by_name("srt"))
    assert (directory / "srt.pc").read_text() == "Libs.private: -lpthread -lc++ -lunwind\n"
    monkeypatch.setattr(
        srt.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=" U __kmpc_fork_call"),
    )
    with pytest.raises(BuildError, match="OpenMP"):
        srt._normalize_cross_metadata(builder, ComponentRegistry().get_by_name("srt"))


class Manager:
    def __init__(self):
        self.state = SimpleNamespace(components={})
        self.statuses = []

    def get(self):
        return self.state

    def mark_component_status(self, name, status, version, *args, **kwargs):
        self.statuses.append((name, status))
        self.state.components[name] = SimpleNamespace(status=status)


def test_dependency_failure_does_not_attempt_srt(monkeypatch):
    registry = ComponentRegistry()
    ordered = [registry.get_by_name("openssl"), registry.get_by_name("srt")]
    calls = []

    def fail(component, builder, manager, validator):
        calls.append(component.name)
        raise BuildError(component.name, "failed")

    monkeypatch.setattr("ffmpeg_builder.g4_wave5a.build_or_reuse", fail)
    with pytest.raises(BuildError, match="failed"):
        run_wave_components(ordered, None, None, None)
    assert calls == ["openssl"]
    with pytest.raises(BuildError, match="only ordered"):
        run_wave_components(ordered + [registry.get_by_name("zlib")], None, None, None)


def validator_at(tmp_path):
    prefix, workspace = tmp_path / "prefix", tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    return Wave5ATargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )


@pytest.mark.parametrize(
    "name,relative",
    [
        ("openssl", "lib/libcrypto.a"),
        ("openssl", "lib/pkgconfig/libcrypto.pc"),
        ("srt", "lib/libsrt.a"),
        ("srt", "lib/pkgconfig/srt.pc"),
    ],
)
@pytest.mark.parametrize("corrupt", [True, False])
def test_missing_or_corrupt_output_fails_safely(tmp_path, name, relative, corrupt):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name(name)
    if name == "srt":
        crypto = validator.prefix / "lib/libcrypto.a"
        crypto.parent.mkdir(parents=True)
        crypto.write_bytes(b"accepted crypto")
        validator.record_component(ComponentRegistry().get_by_name("openssl"), ["lib/libcrypto.a"])
    output = validator.prefix / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"accepted")
    validator.record_component(component, [relative])
    if corrupt:
        output.write_bytes(b"corrupt")
    else:
        output.unlink()
    manager = Manager()
    manager.state.components[name] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    with pytest.raises(BuildError, match="refusing reuse"):
        build_or_reuse(
            component,
            SimpleNamespace(build_component=lambda item: pytest.fail("build")),
            manager,
            validator,
        )
    assert manager.statuses == []


def test_srt_dependency_digest_rejects_changed_openssl(tmp_path):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name("srt")
    lib = validator.prefix / "lib"
    lib.mkdir()
    for name in ("libssl.a", "libcrypto.a", "libsrt.a"):
        (lib / name).write_bytes(name.encode())
    validator.record_component(
        ComponentRegistry().get_by_name("openssl"), ["lib/libssl.a", "lib/libcrypto.a"]
    )
    validator.record_component(component, ["lib/libsrt.a"])
    assert validator.manifest_matches(component)
    (lib / "libcrypto.a").write_bytes(b"changed")
    assert not validator.manifest_matches(component)


@pytest.mark.parametrize("name", ["openssl", "srt"])
def test_validation_failure_cleans_only_component_outputs(tmp_path, monkeypatch, name):
    validator = validator_at(tmp_path)
    component = ComponentRegistry().get_by_name(name)
    lib = validator.prefix / "lib"
    lib.mkdir()
    other = lib / ("libsrt.a" if name == "openssl" else "libcrypto.a")
    other.write_bytes(b"preserved")
    own = lib / ("libcrypto.a" if name == "openssl" else "libsrt.a")
    manager = Manager()
    monkeypatch.setattr(
        validator,
        "validate_component",
        lambda item: (_ for _ in ()).throw(BuildError(item.name, "validation")),
    )
    with pytest.raises(BuildError, match="validation"):
        build_or_reuse(
            component,
            SimpleNamespace(build_component=lambda item: own.write_bytes(b"partial")),
            manager,
            validator,
        )
    assert manager.statuses[-1] == (name, ComponentStatus.FAILED)
    assert other.read_bytes() == b"preserved" and not own.exists()
