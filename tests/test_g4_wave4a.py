from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.codecs import av1_cmake
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave4a import (
    G4_WAVE4A_COMPONENTS,
    Wave4ATargetValidator,
    build_or_reuse,
    run_wave_components,
    select_components,
    verify_sources,
)
from ffmpeg_builder.state import ComponentStatus


def test_registered_sources_and_exact_selection():
    registry = ComponentRegistry()
    assert set(verify_sources(registry, Path("third_party/sources"))) == set(G4_WAVE4A_COMPONENTS)
    selected = [registry.get_by_name(name) for name in ("av1", "x264", "svtav1")]
    assert [item.name for item in select_components(registry, selected)] == ["svtav1", "av1"]
    with pytest.raises(BuildError, match="not configured"):
        select_components(registry, selected[:-1])
    with pytest.raises(BuildError, match="only svtav1 and av1"):
        run_wave_components(selected, object(), object(), object())


@pytest.mark.parametrize("name", ["svtav1", "av1"])
def test_cross_codec_uses_shared_cmake_and_host_nasm(monkeypatch, tmp_path, name):
    cross = SimpleNamespace(compiler_environment={"PATH": "/usr/bin"})
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=cross),
        platform_strategy=None,
        executor=None,
        state_manager=None,
    )
    captured = []
    monkeypatch.setattr(av1_cmake.shutil, "which", lambda name, path=None: "/host/bin/nasm")
    monkeypatch.setattr(av1_cmake, "build_cmake", lambda *args: captured.append(args))
    component = ComponentRegistry().get_by_name(name)
    av1_cmake.build_av1_cmake(builder, component, tmp_path)
    args = captured[0][1].configure_args
    assert "-DCMAKE_ASM_NASM_COMPILER=/host/bin/nasm" in args
    assert "-DCMAKE_ASM_NASM_OBJECT_FORMAT=win64" in args
    assert "-DFETCHCONTENT_FULLY_DISCONNECTED=ON" in args
    assert "-DBUILD_SHARED_LIBS=OFF" in args
    assert ("-DBUILD_APPS=OFF" if name == "svtav1" else "-DENABLE_TOOLS=OFF") in args
    assert component.configure_args != args


def test_native_codec_does_not_get_cross_options(monkeypatch, tmp_path):
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=None),
        platform_strategy=None,
        executor=None,
        state_manager=None,
    )
    captured = []
    monkeypatch.setattr(av1_cmake, "build_cmake", lambda *args: captured.append(args))
    component = ComponentRegistry().get_by_name("av1")
    av1_cmake.build_av1_cmake(builder, component, tmp_path)
    assert captured[0][1] is component


@pytest.mark.parametrize("name", ["svtav1", "av1"])
def test_shared_cmake_uses_target_toolchain_and_environment(monkeypatch, tmp_path, name):
    from ffmpeg_builder.builders import base

    cross = SimpleNamespace(
        cmake_toolchain_file=tmp_path / "cmake-toolchain.cmake",
        compiler_environment={"PATH": "/usr/bin"},
    )
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=cross),
        platform_strategy=None,
        executor=None,
        state_manager=None,
        config=SimpleNamespace(openmp=True),
        num_jobs=2,
        _ws_str=lambda: "/target",
        _is_windows_ucrt64_backend=lambda: False,
        get_build_env=lambda component: {
            "CC": "/target/clang",
            "CFLAGS": "-march=native -mtune=native",
        },
    )
    calls = []
    monkeypatch.setattr(av1_cmake.shutil, "which", lambda *args, **kwargs: "/usr/bin/nasm")
    monkeypatch.setattr(base, "run_step", lambda *args: calls.append(args))
    av1_cmake.build_av1_cmake(builder, ComponentRegistry().get_by_name(name), tmp_path)
    command, env = calls[0][5], calls[0][8]
    assert f"-DCMAKE_TOOLCHAIN_FILE={cross.cmake_toolchain_file}" in command
    assert "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY" in command
    assert "-DCMAKE_ASM_NASM_OBJECT_FORMAT=win64" in command
    assert env["CC"] == "/target/clang"
    assert env["CFLAGS"] == "-march=native -mtune=native"


def test_aom_static_runtime_contract():
    metadata = ComponentRegistry().get_target_link_metadata(["av1"])
    assert set(metadata.consumer_link_requirements) == {"-lm", "-lpthread"}
    assert [(item.name, item.classification) for item in metadata.runtime_dependencies] == [
        ("libwinpthread-1.dll", "toolchain-runtime-dll")
    ]


def test_archive_rejects_non_coff_member(tmp_path, monkeypatch):
    validator = _validator(tmp_path)
    validator.environment.target_tools = SimpleNamespace(readobj="/target/readobj")
    validator.environment.compiler_environment = {}
    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave3a.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Format: COFF-x86-64\nFormat: elf64-x86-64\n", stderr=""
        ),
    )
    with pytest.raises(BuildError, match="not all x86-64 Windows COFF"):
        validator._validate_archive("av1", tmp_path / "mixed.a")


def _validator(tmp_path):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    return Wave4ATargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )


class Manager:
    def __init__(self):
        self.state = SimpleNamespace(components={})
        self.transitions = []

    def get(self):
        return self.state

    def mark_component_status(self, name, status, version, *args, **kwargs):
        self.state.components[name] = SimpleNamespace(status=status)
        self.transitions.append((name, status))


def test_corrupt_output_rebuild_is_isolated(tmp_path, monkeypatch):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("av1")
    path = validator.prefix / "lib/libaom.a"
    prior = validator.prefix / "lib/libSvtAv1Enc.a"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"accepted")
    prior.write_bytes(b"other codec")
    validator.record_component(component, ["lib/libaom.a"])
    path.write_bytes(b"corrupt")
    assert not validator.manifest_matches(component)
    manager = Manager()
    manager.state.components["av1"] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    builder = SimpleNamespace(build_component=lambda item: path.write_bytes(b"rebuilt"))
    monkeypatch.setattr(validator, "validate_component", lambda item: ["lib/libaom.a"])
    monkeypatch.setattr(validator, "validate_prefix", lambda: None)
    assert build_or_reuse(component, builder, manager, validator)
    assert validator.manifest_matches(component)
    assert prior.read_bytes() == b"other codec"
    assert manager.transitions == [
        ("av1", ComponentStatus.PENDING),
        ("av1", ComponentStatus.COMPLETED),
    ]


def test_failed_build_preserves_other_components(tmp_path, monkeypatch):
    validator = _validator(tmp_path)
    registry = ComponentRegistry()
    components = [registry.get_by_name(name) for name in G4_WAVE4A_COMPONENTS]
    prior = validator.prefix / "lib/libogg.a"
    prior.parent.mkdir()
    prior.write_bytes(b"prior accepted")
    manager = Manager()
    called = []

    def fail(component):
        called.append(component.name)
        raise BuildError(component.name, "build failed")

    with pytest.raises(BuildError, match="build failed"):
        run_wave_components(components, SimpleNamespace(build_component=fail), manager, validator)
    assert called == ["svtav1"]
    assert manager.state.components["svtav1"].status == ComponentStatus.FAILED
    assert "av1" not in manager.state.components
    assert prior.read_bytes() == b"prior accepted"


def test_aom_failure_does_not_invalidate_accepted_svt(tmp_path, monkeypatch):
    validator = _validator(tmp_path)
    registry = ComponentRegistry()
    components = [registry.get_by_name(name) for name in G4_WAVE4A_COMPONENTS]
    archive = validator.prefix / "lib/libSvtAv1Enc.a"
    archive.parent.mkdir()
    archive.write_bytes(b"accepted svt")
    validator.record_component(components[0], ["lib/libSvtAv1Enc.a"])
    manager = Manager()
    manager.state.components["svtav1"] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    monkeypatch.setattr(validator, "validate_component", lambda item: ["lib/libSvtAv1Enc.a"])
    monkeypatch.setattr(validator, "validate_prefix", lambda: None)

    def fail(component):
        raise BuildError(component.name, "aom build failed")

    with pytest.raises(BuildError, match="aom build failed"):
        run_wave_components(components, SimpleNamespace(build_component=fail), manager, validator)
    assert manager.state.components["svtav1"].status == ComponentStatus.COMPLETED
    assert manager.state.components["av1"].status == ComponentStatus.FAILED
    assert archive.read_bytes() == b"accepted svt"


def test_reuse_revalidates_outputs_and_flags_failures(tmp_path, monkeypatch):
    validator = _validator(tmp_path)
    component = ComponentRegistry().get_by_name("svtav1")
    archive = validator.prefix / "lib/libSvtAv1Enc.a"
    archive.parent.mkdir()
    archive.write_bytes(b"accepted")
    validator.record_component(component, ["lib/libSvtAv1Enc.a"])
    manager = Manager()
    manager.state.components[component.name] = SimpleNamespace(status=ComponentStatus.COMPLETED)
    monkeypatch.setattr(validator, "validate_prefix", lambda: None)
    monkeypatch.setattr(
        validator,
        "validate_component",
        lambda item: (_ for _ in ()).throw(BuildError(item.name, "bad format")),
    )
    with pytest.raises(BuildError, match="bad format"):
        build_or_reuse(component, object(), manager, validator)
    assert manager.state.components[component.name].status == ComponentStatus.FAILED
    assert archive.exists()
