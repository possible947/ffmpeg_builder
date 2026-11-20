from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.codecs import vorbis
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.cross_environment import _write_pkg_config_wrapper
from ffmpeg_builder.g4_wave3b import (
    G4_WAVE3B_COMPONENTS,
    Wave3BTargetValidator,
    build_or_reuse,
    order_wave_components,
    run_wave_components,
    verify_wave_sources,
)
from ffmpeg_builder.state import ComponentStatus


def test_wave3b_graph_is_exact_and_requires_accepted_ogg():
    registry = ComponentRegistry()
    selected = [registry.get_by_name(name) for name in (*G4_WAVE3B_COMPONENTS, "zlib")]
    assert registry.get_by_name("libvorbis").depends_on == ["libogg"]
    assert registry.get_by_name("libtheora").depends_on == ["libogg", "libvorbis"]
    with pytest.raises(BuildError, match="libogg.*not been accepted"):
        order_wave_components(registry, selected, [])
    for _ in range(2):
        assert [
            item.name for item in order_wave_components(registry, selected, ["libogg"])
        ] == list(G4_WAVE3B_COMPONENTS)
    with pytest.raises(BuildError, match="not configured"):
        order_wave_components(registry, [registry.get_by_name("libtheora")], ["libogg"])
    registry.get_by_name("libvorbis").depends_on.append("libtheora")
    with pytest.raises(BuildError, match="cycle"):
        order_wave_components(registry, selected, ["libogg"])


def test_wave3b_registered_sources_verify():
    assert set(verify_wave_sources(ComponentRegistry(), Path("third_party/sources"))) == set(
        G4_WAVE3B_COMPONENTS
    )


def _pc(directory: Path, name: str, prefix: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.pc").write_text(
        f"prefix={prefix}\nName: {name}\nDescription: fixture\nVersion: 1.0\n"
        f"Libs: -L${{prefix}}/lib -l{name}\nCflags: -I${{prefix}}/include\n"
    )


@pytest.mark.parametrize(
    "component,available,missing",
    [
        ("libvorbis", (), "ogg"),
        ("libtheora", ("ogg",), "vorbis"),
        ("libtheora", ("vorbis",), "ogg"),
    ],
)
def test_target_dependency_preflight_rejects_host_fallback(tmp_path, component, available, missing):
    pkg_config = shutil.which("pkg-config")
    assert pkg_config
    prefix = tmp_path / "target"
    host = tmp_path / "host"
    for name in ("ogg", "vorbis"):
        _pc(host, name, tmp_path / "host-prefix")
    for name in available:
        _pc(prefix / "lib/pkgconfig", name, prefix)
        header = prefix / ("include/ogg/ogg.h" if name == "ogg" else "include/vorbis/codec.h")
        archive = prefix / f"lib/lib{name}.a"
        header.parent.mkdir(parents=True, exist_ok=True)
        archive.parent.mkdir(parents=True, exist_ok=True)
        header.touch()
        archive.touch()
    wrapper = tmp_path / "pkg-config"
    _write_pkg_config_wrapper(
        wrapper,
        {"python": sys.executable, "pkg-config": pkg_config},
        SimpleNamespace(target_prefix=prefix),
        [prefix],
    )
    cross = SimpleNamespace(
        paths=SimpleNamespace(target_prefix=prefix),
        pkg_config_wrapper=wrapper,
        autotools=SimpleNamespace(
            environment={"PATH": str(Path(pkg_config).parent), "PKG_CONFIG_PATH": str(host)}
        ),
    )
    builder = SimpleNamespace(platform_context=SimpleNamespace(cross_environment=cross))
    with pytest.raises(BuildError, match=missing):
        vorbis._require_target_packages(
            builder,
            ComponentRegistry().get_by_name(component),
            ("ogg",) if component == "libvorbis" else ("ogg", "vorbis"),
        )


def test_vorbis_cross_adapter_uses_shared_autotools(monkeypatch, tmp_path):
    component = ComponentRegistry().get_by_name("libvorbis")
    cross = object()
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=cross),
        platform_strategy=None,
        executor=None,
        state_manager=None,
    )
    calls = []
    monkeypatch.setattr(vorbis, "_require_target_packages", lambda *args: calls.append(args[2]))
    (tmp_path / "Makefile.in").write_text("SUBDIRS = m4 include vq lib test doc $(am__append_1)\n")
    monkeypatch.setattr(
        vorbis,
        "build_autotools",
        lambda context, item, source, **kw: calls.append((item, source, kw)),
    )
    vorbis.build_libvorbis(builder, component, tmp_path)
    assert calls[0] == ("ogg",)
    assert "--disable-shared" in calls[1][0].configure_args
    assert "--disable-examples" in calls[1][0].configure_args
    assert calls[1][1] == tmp_path
    assert "OGG_CFLAGS" in calls[1][2]["unset_environment"]
    assert (tmp_path / "Makefile.in").read_text() == "SUBDIRS = m4 include lib $(am__append_1)\n"


def test_theora_native_uses_generic_without_cross_preflight(monkeypatch, tmp_path):
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=None),
        platform_strategy=None,
        executor=None,
        state_manager=None,
    )
    calls = []
    monkeypatch.setattr(vorbis, "build_autotools", lambda *args: calls.append(args))
    monkeypatch.setattr(
        vorbis, "_require_target_packages", lambda *args: pytest.fail("native preflight")
    )
    component = ComponentRegistry().get_by_name("libtheora")
    vorbis.build_libtheora(builder, component, tmp_path)
    assert calls[0][1] is component


def test_theora_cross_excludes_documentation_and_uses_shared_autotools(monkeypatch, tmp_path):
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=object()),
        platform_strategy=None,
        executor=None,
        state_manager=None,
    )
    makefile = tmp_path / "Makefile.in"
    makefile.write_text("SUBDIRS = lib include doc tests m4 $(EXAMPLES_DIR)\n")
    calls = []
    monkeypatch.setattr(vorbis, "_require_target_packages", lambda *args: calls.append(args[2]))
    monkeypatch.setattr(
        vorbis, "build_autotools", lambda context, item, source, **kw: calls.append((item, kw))
    )
    vorbis.build_libtheora(builder, ComponentRegistry().get_by_name("libtheora"), tmp_path)
    assert calls[0] == ("ogg", "vorbis")
    assert "--disable-doc" in calls[1][0].configure_args
    assert "VORBIS_LIBS" in calls[1][1]["unset_environment"]
    assert makefile.read_text() == "SUBDIRS = lib include m4 $(EXAMPLES_DIR)\n"


def test_cross_adapter_rejects_unexpected_source_layout(tmp_path):
    makefile = tmp_path / "Makefile.in"
    makefile.write_text("unexpected source\n")
    with pytest.raises(BuildError, match="Unexpected pinned source layout"):
        vorbis._restrict_subdirectories(
            ComponentRegistry().get_by_name("libtheora"), tmp_path, "absent marker", "replacement"
        )
    assert makefile.read_text() == "unexpected source\n"


def test_autotools_projection_discards_host_package_overrides(monkeypatch, tmp_path):
    from ffmpeg_builder.builders import base

    calls = []
    cross = SimpleNamespace(
        backend="linux-cross-mingw",
        autotools=SimpleNamespace(
            arguments=("--build=host", "--host=target"),
            environment={"CC": "/target/clang", "PKG_CONFIG": "/target/pkg-config"},
        ),
    )
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=cross),
        platform="linux",
        num_jobs=2,
        get_build_env=lambda component: {"OGG_CFLAGS": "-I/usr/include", "CC": "host-cc"},
        _ws_str=lambda: "/target",
    )
    context = SimpleNamespace(builder=builder)
    monkeypatch.setattr(base, "run_step", lambda *args: calls.append(args))
    monkeypatch.setattr(base, "run_make", lambda *args: None)
    monkeypatch.setattr(base, "run_install", lambda *args: None)
    base.build_autotools(
        context,
        ComponentRegistry().get_by_name("libtheora"),
        tmp_path,
        unset_environment=vorbis._DEPENDENCY_ENVIRONMENT,
    )
    assert calls[0][5][1:3] == ["--build=host", "--host=target"]
    assert calls[0][8]["CC"] == "/target/clang"
    assert calls[0][8]["PKG_CONFIG"] == "/target/pkg-config"
    assert "OGG_CFLAGS" not in calls[0][8]


def test_failure_invalidates_theora_and_stops_before_it_is_built(tmp_path, monkeypatch):
    registry = ComponentRegistry()
    ordered = [registry.get_by_name(name) for name in G4_WAVE3B_COMPONENTS]
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    validator = Wave3BTargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )

    class Manager:
        def __init__(self):
            self.state = SimpleNamespace(
                components={
                    "libvorbis": SimpleNamespace(status=ComponentStatus.COMPLETED),
                    "libtheora": SimpleNamespace(status=ComponentStatus.COMPLETED),
                }
            )

        def get(self):
            return self.state

        def mark_component_status(self, name, status, version, *args, **kwargs):
            self.state.components[name] = SimpleNamespace(status=status)

    manager = Manager()
    called = []

    def fail(component):
        called.append(component.name)
        raise BuildError(component.name, "build failure")

    with pytest.raises(BuildError, match="build failure"):
        run_wave_components(ordered, SimpleNamespace(build_component=fail), manager, validator)
    assert called == ["libvorbis"]
    assert manager.state.components["libvorbis"].status == ComponentStatus.FAILED
    assert manager.state.components["libtheora"].status == ComponentStatus.PENDING


def test_execution_rejects_reversed_or_outside_subset():
    registry = ComponentRegistry()
    components = [registry.get_by_name(name) for name in ("libtheora", "libvorbis", "zlib")]
    for selected in (components[:2], components):
        with pytest.raises(BuildError, match="only libvorbis then libtheora"):
            run_wave_components(selected, object(), object(), object())


def test_archive_validator_rejects_elf(tmp_path, monkeypatch):
    validator = Wave3BTargetValidator(
        SimpleNamespace(
            paths=SimpleNamespace(target_prefix=tmp_path, workspace_root=tmp_path),
            target_tools=SimpleNamespace(readobj="/target/readobj"),
            compiler_environment={},
        ),
        "identity",
    )
    monkeypatch.setattr(
        "ffmpeg_builder.g4_wave3a.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Format: COFF-x86-64\nFormat: elf64-x86-64\n", stderr=""
        ),
    )
    with pytest.raises(BuildError, match="not all x86-64 Windows COFF"):
        validator._validate_archive("libvorbis", tmp_path / "host.a")


def test_manifest_corruption_rejects_reuse_and_rebuilds(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    validator = Wave3BTargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )
    component = ComponentRegistry().get_by_name("libvorbis")
    archive = prefix / "lib/libvorbis.a"
    archive.parent.mkdir()
    archive.write_bytes(b"accepted")
    validator.record_component(component, ["lib/libvorbis.a"])
    assert validator.manifest_matches(component)
    archive.write_bytes(b"corrupt")
    assert not validator.manifest_matches(component)

    class Manager:
        def __init__(self):
            self.state = SimpleNamespace(
                components={"libvorbis": SimpleNamespace(status=ComponentStatus.COMPLETED)}
            )
            self.transitions = []

        def get(self):
            return self.state

        def mark_component_status(self, name, status, version, *args, **kwargs):
            self.transitions.append(status)
            self.state.components[name] = SimpleNamespace(status=status)

    manager = Manager()
    builder = SimpleNamespace(build_component=lambda item: archive.write_bytes(b"rebuilt"))
    monkeypatch.setattr(validator, "validate_component", lambda item: ["lib/libvorbis.a"])
    monkeypatch.setattr(validator, "validate_prefix", lambda: None)
    assert build_or_reuse(component, builder, manager, validator)
    assert validator.manifest_matches(component)
    assert manager.transitions == [ComponentStatus.PENDING, ComponentStatus.COMPLETED]


def test_corrupt_manifest_cannot_remove_prior_wave_output(tmp_path):
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    (prefix / "lib").mkdir(parents=True)
    workspace.mkdir()
    validator = Wave3BTargetValidator(
        SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace)),
        "identity",
    )
    component = ComponentRegistry().get_by_name("libvorbis")
    vorbis_archive = prefix / "lib/libvorbis.a"
    ogg_archive = prefix / "lib/libogg.a"
    vorbis_archive.write_bytes(b"partial")
    ogg_archive.write_bytes(b"accepted")
    validator._write_manifest(
        {
            "identity_key": "identity",
            "components": {"libvorbis": {"outputs": ["lib/libogg.a", "lib/libvorbis.a"]}},
        }
    )
    validator.remove_component_outputs(component)
    assert not vorbis_archive.exists()
    assert ogg_archive.read_bytes() == b"accepted"
