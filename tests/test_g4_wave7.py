import json
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.graphics.cross_install import promote_staged
from ffmpeg_builder.builders.graphics.glslang_sources import (
    DEPENDENCIES,
    install_spirv_headers,
    prepare_glslang_sources,
)
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave7 import Wave7Validator, select_components


def selection(registry):
    return SimpleNamespace(
        target_inputs=[registry.get_by_name("vulkan-headers")],
        target_components_to_build=[
            registry.get_by_name(name) for name in ("glslang", "fast-float", "libplacebo")
        ],
    )


def test_wave7_uses_real_registry_dependency_order():
    registry = ComponentRegistry()
    components = select_components(registry, selection(registry))
    names = [component.name for component in components]
    assert set(names) == {"vulkan-headers", "glslang", "fast-float", "libplacebo"}
    for dependency in registry.get_by_name("libplacebo").depends_on:
        assert names.index(dependency) < names.index("libplacebo")
    incomplete = selection(registry)
    incomplete.target_components_to_build.pop(0)
    with pytest.raises(BuildError, match="unconfigured"):
        select_components(registry, incomplete)


def test_target_glslang_finishes_header_inventory_before_acceptance(tmp_path, monkeypatch):
    from ffmpeg_builder.builders.graphics import glslang

    source, prefix = tmp_path / "source", tmp_path / "prefix"
    cmake = source / "External/spirv-tools/source/CMakeLists.txt"
    cmake.parent.mkdir(parents=True)
    cmake.write_text(
        "add_library(${SPIRV_TOOLS}-shared SHARED ${SPIRV_SOURCES})\n"
        "set(SPIRV_TOOLS_TARGETS ${SPIRV_TOOLS} ${SPIRV_TOOLS}-shared)\n"
    )
    component = ComponentRegistry().get_by_name("glslang")
    cross = SimpleNamespace(
        cmake_toolchain_file=tmp_path / "toolchain.cmake",
        paths=SimpleNamespace(generated_files_root=tmp_path),
    )
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=cross),
        target_prefix=prefix,
        num_jobs=2,
        get_build_env=lambda component: {},
        _run_step=lambda *args: None,
    )

    def install_partial(*args):
        header = prefix / "include/spirv/unified1/spirv.hpp"
        header.parent.mkdir(parents=True)
        header.write_text("cmake-installed header")

    def install_complete(builder):
        header = prefix / "include/spirv/unified1/spirv.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text("pinned FFmpeg consumer header")

    monkeypatch.setattr(glslang, "prepare_glslang_sources", lambda *args: None)
    monkeypatch.setattr(glslang, "promote_staged", install_partial)
    monkeypatch.setattr(glslang, "install_spirv_headers", install_complete)
    glslang.build_glslang(builder, component, source)
    accepted = {p.relative_to(prefix) for p in prefix.rglob("*") if p.is_file()}
    assert (prefix / "include/spirv/unified1/spirv.h").is_file()
    install_complete(builder)
    assert {p.relative_to(prefix) for p in prefix.rglob("*") if p.is_file()} == accepted


def test_glslang_pins_must_match_upstream_and_use_project_downloader(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    known = {
        "commits": [
            {"name": "spirv-tools", "commit": DEPENDENCIES[0].revision},
            {"name": "spirv-tools/external/spirv-headers", "commit": DEPENDENCIES[1].revision},
        ]
    }
    (source / "known_good.json").write_text(json.dumps(known))
    calls = []

    class Downloader:
        def download(self, url, filename, **kwargs):
            calls.append((url, filename, kwargs))
            return tmp_path / filename

    extracted = []
    monkeypatch.setattr(
        "ffmpeg_builder.builders.graphics.glslang_sources._extract_dependency",
        lambda archive, destination: extracted.append((archive, destination)),
    )
    prepare_glslang_sources(SimpleNamespace(downloader=Downloader()), source)
    assert [call[1] for call in calls] == [dep.filename for dep in DEPENDENCIES]
    assert [call[2]["expected_sha256"] for call in calls] == [dep.sha256 for dep in DEPENDENCIES]
    assert extracted[1][1] == source / "External/spirv-tools/external/spirv-headers"
    known["commits"][0]["commit"] = "different"
    (source / "known_good.json").write_text(json.dumps(known))
    with pytest.raises(BuildError, match="known_good"):
        prepare_glslang_sources(SimpleNamespace(downloader=Downloader()), source)
    assert len(calls) == 2


def test_spirv_headers_use_pinned_source_and_refuse_conflicts(tmp_path, monkeypatch):
    calls = []

    class Downloader:
        def download(self, url, filename, **kwargs):
            calls.append((filename, kwargs["expected_sha256"]))
            return tmp_path / filename

    def extract(archive, destination):
        header = destination / "include/spirv/unified1/spirv.h"
        header.parent.mkdir(parents=True)
        header.write_text("pinned")

    monkeypatch.setattr(
        "ffmpeg_builder.builders.graphics.glslang_sources._extract_dependency", extract
    )
    builder = SimpleNamespace(
        downloader=Downloader(),
        target_prefix=tmp_path / "prefix",
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(paths=SimpleNamespace(generated_files_root=tmp_path))
        ),
    )
    install_spirv_headers(builder)
    install_spirv_headers(builder)
    assert calls == [(DEPENDENCIES[1].filename, DEPENDENCIES[1].sha256)] * 2
    header = builder.target_prefix / "include/spirv/unified1/spirv.h"
    assert header.read_text() == "pinned"
    header.write_text("changed")
    with pytest.raises(BuildError, match="Conflicting target header"):
        install_spirv_headers(builder)


def test_staged_install_refuses_overwrite_before_promoting_any_files(tmp_path):
    stage = tmp_path / "stage"
    prefix = tmp_path / "prefix"
    for root in (stage, prefix):
        (root / "lib").mkdir(parents=True)
    (stage / "lib/a.a").write_bytes(b"new")
    (stage / "lib/b.a").write_bytes(b"new")
    (prefix / "lib/b.a").write_bytes(b"prior")
    builder = SimpleNamespace(_wave7_created_outputs=[])
    with pytest.raises(BuildError, match="Refusing unsafe"):
        promote_staged("glslang", stage, prefix, builder)
    assert not (prefix / "lib/a.a").exists()
    assert (prefix / "lib/b.a").read_bytes() == b"prior"
    assert not builder._wave7_created_outputs
    (prefix / "lib/b.a").unlink()
    (stage / "lib/unwanted.dll").write_bytes(b"shared")
    with pytest.raises(BuildError, match="non-static"):
        promote_staged("glslang", stage, prefix, builder)
    assert not (prefix / "lib/a.a").exists()


def test_wave7_reuse_requires_source_and_output_identity(tmp_path):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    environment = SimpleNamespace(
        paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace),
    )
    validator = Wave7Validator(environment, "key", {"glslang": "source"})
    component = ComponentRegistry().get_by_name("fast-float")
    header = prefix / "include/fast_float/fast_float.h"
    header.parent.mkdir(parents=True)
    header.write_text("original")
    validator.record_component(component, validator._owned_outputs(component))
    assert validator.manifest_matches(component)
    header.write_text("changed")
    assert not validator.manifest_matches(component)
    header.write_text("original")
    validator.sources = {"glslang": "changed"}
    assert not validator.manifest_matches(component)
