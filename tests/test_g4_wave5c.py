import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.base import install_headers_only
from ffmpeg_builder.builders.codecs import jxl
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave5c import (
    G4_WAVE5C_COMPONENTS,
    Wave5CTargetValidator,
    build_or_reuse,
    select_components,
    validate_prior_manifests,
)
from ffmpeg_builder.state import ComponentStatus


def _stage_jxl(prefix):
    for name in ("jxl", "hwy", "brotli"):
        path = prefix / "include" / name
        path.mkdir(parents=True)
        (path / "header.h").write_text("header")
    for name in jxl._LIBRARIES:
        path = prefix / "lib" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"archive")
    for name in (
        "libjxl",
        "libjxl_cms",
        "libjxl_threads",
        "libhwy",
        "libbrotlicommon",
        "libbrotlidec",
        "libbrotlienc",
    ):
        path = prefix / "lib/pkgconfig" / f"{name}.pc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Name: test\nLibs.private: -lm\n")
    man = prefix / "share/man/man1"
    man.mkdir(parents=True)
    (man / "brotli.1").write_text("unwanted")


def test_cross_builder_uses_toolchain_and_bounded_install(tmp_path, monkeypatch):
    component = ComponentRegistry().get_by_name("libjxl")
    source = tmp_path / "source"
    source.mkdir()
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()
    calls = []

    def run_step(*args):
        calls.append(args)
        if args[5] == "install":
            stage = Path(args[7]["DESTDIR"])
            _stage_jxl(stage / prefix.relative_to("/"))

    builder = SimpleNamespace(
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(
                cmake_toolchain_file=Path("/target/cmake-toolchain.cmake"),
                paths=SimpleNamespace(generated_files_root=generated),
            )
        ),
        target_prefix=prefix,
        num_jobs=2,
        get_build_env=lambda component: {"CFLAGS": "-march=native -mtune=native"},
        _run_step=run_step,
    )
    monkeypatch.setattr(jxl, "prepare_libjxl_sources", lambda *args: {})
    jxl.build_libjxl(builder, component, source)
    configure = calls[0][4]
    assert "-DCMAKE_TOOLCHAIN_FILE=/target/cmake-toolchain.cmake" in configure
    assert "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY" in configure
    assert "-DJPEGXL_ENABLE_SKCMS=OFF" in configure
    assert "-DJPEGXL_FORCE_SYSTEM_LCMS2=ON" in configure
    assert all("HWY_DISABLED_TARGETS" not in arg for arg in configure)
    assert calls[0][7]["CFLAGS"] == "-march=native -mtune=native"
    assert (prefix / "lib/libjxl.a").is_file()
    assert not (prefix / "share/man/man1/brotli.1").exists()
    pc = (prefix / "lib/pkgconfig/libjxl_threads.pc").read_text()
    assert "-lc++" in pc and "-lunwind" in pc
    assert not list(generated.iterdir())


def test_cross_promotion_rejects_existing_file_without_overwrite(tmp_path):
    stage = tmp_path / "stage"
    prefix = tmp_path / "prefix"
    _stage_jxl(stage)
    (prefix / "lib").mkdir(parents=True)
    existing = prefix / "lib/libjxl.a"
    existing.write_text("existing")
    with pytest.raises(BuildError, match="Refusing to overwrite"):
        jxl._promote_cross_outputs(
            ComponentRegistry().get_by_name("libjxl"), stage, prefix, SimpleNamespace()
        )
    assert existing.read_text() == "existing"
    assert not (prefix / "include/jxl/header.h").exists()


def test_metadata_normalization_is_idempotent(tmp_path):
    _stage_jxl(tmp_path)
    assert jxl.normalize_cross_metadata(tmp_path)
    assert not jxl.normalize_cross_metadata(tmp_path)
    assert "-lunwind" in (tmp_path / "lib/pkgconfig/libjxl.pc").read_text()
    assert "-lc++" in (tmp_path / "lib/pkgconfig/libjxl_threads.pc").read_text()


def test_vapoursynth_cross_installer_probes_headers(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "include").mkdir(parents=True)
    for name in ("VSScript4.h", "VapourSynth4.h"):
        (source / "include" / name).write_text("header")
    calls = []
    monkeypatch.setattr(
        "ffmpeg_builder.builders.base.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(returncode=0, stderr=""),
    )
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(target_tools=SimpleNamespace(cc="/target/clang"))
        ),
        _component_prefix=lambda component: tmp_path / "prefix",
        get_build_env=lambda component: {"CC": "/target/clang"},
        _wave5c_created_outputs=[],
    )
    context = SimpleNamespace(builder=builder)
    component = ComponentRegistry().get_by_name("VapourSynth")
    install_headers_only(context, component, source)
    assert calls[0][0][0:2] == ["/target/clang", "-E"]
    assert "#include <vapoursynth/VSScript4.h>" in calls[0][1]["input"]
    assert len(builder._wave5c_created_outputs) == 2
    assert all(path.is_file() for path in builder._wave5c_created_outputs)


def test_vapoursynth_missing_source_header_is_rejected(tmp_path):
    source = tmp_path / "source"
    (source / "include").mkdir(parents=True)
    (source / "include/VSScript4.h").write_text("header")
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(target_tools=SimpleNamespace(cc="/target/clang"))
        ),
        _component_prefix=lambda component: tmp_path / "prefix",
    )
    with pytest.raises(BuildError, match="Missing source header"):
        install_headers_only(
            SimpleNamespace(builder=builder),
            ComponentRegistry().get_by_name("VapourSynth"),
            source,
        )
    assert not (tmp_path / "prefix/include/vapoursynth/VSScript4.h").exists()


def test_wave_subset_and_registry_contract():
    registry = ComponentRegistry()
    jxl_component = registry.get_by_name("libjxl")
    vs = registry.get_by_name("VapourSynth")
    assert G4_WAVE5C_COMPONENTS == ("libjxl", "VapourSynth")
    assert select_components(registry, [vs, jxl_component]) == [jxl_component, vs]
    assert "lcms2" in jxl_component.depends_on
    assert {"-lc++", "-lunwind"}.issubset(
        jxl_component.target_link_metadata.consumer_link_requirements
    )
    assert any(
        item.name == "VSScript.dll" and item.classification == "external-runtime-prerequisite"
        for item in vs.target_link_metadata.runtime_dependencies
    )
    with pytest.raises(BuildError, match="Duplicate"):
        select_components(registry, [vs, jxl_component, vs])


def test_manifest_reuse_rejects_changed_output_and_dependency(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    header = prefix / "include/jxl/decode.h"
    header.parent.mkdir(parents=True)
    header.write_text("original")
    env = SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace))
    validator = Wave5CTargetValidator(env, "identity")
    component = ComponentRegistry().get_by_name("libjxl")
    monkeypatch.setattr(validator, "dependency_identity", lambda: {"lcms2": "original"})
    outputs = validator._owned_outputs(component)
    validator.record_component(component, outputs)
    assert validator.manifest_matches(component)
    header.write_text("corrupt")
    assert not validator.manifest_matches(component)
    header.write_text("original")
    monkeypatch.setattr(validator, "dependency_identity", lambda: {"lcms2": "changed"})
    assert not validator.manifest_matches(component)


def test_vapoursynth_manifest_rejects_missing_header(tmp_path):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = prefix / "include/vapoursynth"
    root.mkdir(parents=True)
    for name in ("VSScript4.h", "VapourSynth4.h"):
        (root / name).write_text(name)
    env = SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace))
    validator = Wave5CTargetValidator(env, "identity")
    component = ComponentRegistry().get_by_name("VapourSynth")
    validator.record_component(component, validator._owned_outputs(component))
    assert validator.manifest_matches(component)
    (root / "VSScript4.h").unlink()
    assert not validator.manifest_matches(component)


def test_prior_manifest_detects_corruption(tmp_path):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prefix.mkdir()
    item = prefix / "prior.h"
    item.write_bytes(b"original")
    import hashlib

    manifest = {
        "identity_key": "id",
        "target_prefix": str(prefix),
        "components": {"prior": {"sha256": {"prior.h": hashlib.sha256(b"original").hexdigest()}}},
    }
    (workspace / "g4-wave2-acceptance.json").write_text(json.dumps(manifest))
    assert validate_prior_manifests(workspace, prefix, "id") == 1
    item.write_text("changed")
    with pytest.raises(BuildError, match="Prior output changed"):
        validate_prior_manifests(workspace, prefix, "id")


def test_failed_build_removes_only_recorded_outputs(tmp_path):
    component = ComponentRegistry().get_by_name("libjxl")
    target = tmp_path / "ours"
    unrelated = tmp_path / "other"

    class Builder:
        def build_component(self, component):
            target.write_text("partial")
            self._wave5c_created_outputs.append(target)
            unrelated.write_text("unrelated")
            raise BuildError(component.name, "failed")

    statuses = []
    manager = SimpleNamespace(
        get=lambda: SimpleNamespace(components={}),
        mark_component_status=lambda name, status, *args, **kwargs: statuses.append(status),
    )
    validator = SimpleNamespace(prefix=tmp_path, _owned_outputs=lambda component: [])
    with pytest.raises(BuildError, match="failed"):
        build_or_reuse(component, Builder(), manager, validator)
    assert not target.exists()
    assert unrelated.read_text() == "unrelated"
    assert statuses == [ComponentStatus.PENDING, ComponentStatus.FAILED]
