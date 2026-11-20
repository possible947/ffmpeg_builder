from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ffmpeg_builder.g4_wave3a as wave3a
from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.codecs import lv2 as lv2_builder
from ffmpeg_builder.components import BuildRole, ComponentRegistry
from ffmpeg_builder.config import TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.g4_wave3a import (
    G4_WAVE3A_COMPONENTS,
    Wave3ATargetValidator,
    build_or_reuse_component,
    order_wave_components,
    reject_host_pkg_config_fallback,
    run_wave_components,
    verify_wave_sources,
)


def _selected(registry: ComponentRegistry, *extra: str):
    return [
        component
        for name in (*G4_WAVE3A_COMPONENTS, *extra)
        if (component := registry.get_by_name(name)) is not None
    ]


def _fake_validator(tmp_path: Path) -> Wave3ATargetValidator:
    prefix = tmp_path / "prefix"
    workspace = tmp_path / "workspace"
    generated = workspace / "generated"
    prefix.mkdir()
    generated.mkdir(parents=True)
    environment = SimpleNamespace(
        paths=SimpleNamespace(
            target_prefix=prefix,
            workspace_root=workspace,
            generated_files_root=generated,
        ),
        target_tools=SimpleNamespace(prefix=Path("/toolchains/llvm-mingw-20261104")),
    )
    return Wave3ATargetValidator(environment, "identity-test")


def test_wave3a_registry_graph_and_mesons_are_declared():
    registry = ComponentRegistry()
    expected = {
        "lv2": [],
        "serd": [],
        "zix": [],
        "sord": ["zix", "serd"],
        "sratom": ["serd", "sord", "lv2"],
        "lilv": ["zix", "serd", "sord", "lv2", "sratom"],
    }
    for name in G4_WAVE3A_COMPONENTS:
        component = registry.get_by_name(name)
        assert component is not None
        assert component.depends_on == expected[name]
        assert component.build_role == BuildRole.TARGET_COMPONENT
        assert component.build_system.value == "meson"
        assert "--wrap-mode=nofallback" in component.configure_args


def test_wave3a_topological_order_is_exact_and_deterministic():
    registry = ComponentRegistry()
    selected = _selected(registry, "libvorbis", "libtheora")
    ordered = order_wave_components(registry, selected)
    names = [component.name for component in ordered]
    assert names == ["lv2", "serd", "zix", "sord", "sratom", "lilv"]
    assert names == [component.name for component in order_wave_components(registry, selected)]


def test_wave3a_rejects_missing_component_and_unregistered_dependency():
    registry = ComponentRegistry()
    selected = _selected(registry)
    with pytest.raises(BuildError, match="not in the configured cross target selection"):
        order_wave_components(registry, selected[:-1])

    sord = registry.get_by_name("sord")
    assert sord is not None
    sord.depends_on = ["missing-component"]
    with pytest.raises(BuildError, match="missing from the registry"):
        order_wave_components(registry, selected)


def test_wave3a_rejects_dependency_cycle():
    registry = ComponentRegistry()
    selected = _selected(registry)
    sord = registry.get_by_name("sord")
    assert sord is not None
    sord.depends_on = ["lilv"]
    with pytest.raises(BuildError, match="Dependency cycle detected"):
        order_wave_components(registry, selected)


def test_wave3a_source_archives_are_local_and_checksum_verified():
    verified = verify_wave_sources(ComponentRegistry(), Path("third_party/sources"))
    assert set(verified) == set(G4_WAVE3A_COMPONENTS)
    assert all(len(checksum) == 64 for checksum in verified.values())


def test_wave3a_uses_production_meson_cross_projection(monkeypatch, tmp_path):
    from ffmpeg_builder.builders import base as shared_builders
    from ffmpeg_builder.builders.base import ComponentBuildContext, build_meson

    component = ComponentRegistry().get_by_name("lv2")
    assert component is not None
    cross = SimpleNamespace(meson_cross_file=tmp_path / "meson-cross.ini")
    calls = []

    class Builder:
        platform_context = SimpleNamespace(cross_environment=cross)
        platform_strategy = None
        executor = None
        state_manager = None

        @staticmethod
        def _is_windows_ucrt64_backend():
            return False

        @staticmethod
        def _ws_str(_component=None):
            return "/target/prefix"

        @staticmethod
        def get_build_env(_component):
            return {"PKG_CONFIG": "/identity/pkg-config"}

    monkeypatch.setattr(shared_builders, "run_step", lambda *args: calls.append(args))
    build_meson(ComponentBuildContext.from_builder(Builder()), component, tmp_path / "lv2-source")
    configure = calls[0][5]
    assert configure[:3] == ["meson", "setup", "build"]
    assert configure[3:5] == ["--cross-file", str(cross.meson_cross_file)]
    assert "--wrap-mode=nofallback" in configure
    assert "-Dplugins=disabled" in configure


def test_lv2_target_adapter_omits_nonrequired_validator_utility(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    meson_file = source / "meson.build"
    meson_file.write_text("project('lv2')\nsubdir('util')\n", encoding="utf-8")
    component = ComponentRegistry().get_by_name("lv2")
    assert component is not None
    builder = SimpleNamespace(
        config=SimpleNamespace(target_backend=TARGET_BACKEND_CROSS_MINGW),
        platform_context=SimpleNamespace(cross_environment=object()),
        platform_strategy=None,
        executor=None,
        state_manager=None,
    )
    called = []
    monkeypatch.setattr(lv2_builder, "build_meson", lambda *_args: called.append(True))

    lv2_builder.build_lv2_target(builder, component, source)

    assert "subdir('util')" not in meson_file.read_text(encoding="utf-8")
    assert called == [True]


def test_wave3a_manifest_rejects_corruption_and_schedules_rebuild(tmp_path, monkeypatch):
    validator = _fake_validator(tmp_path)
    component = ComponentRegistry().get_by_name("serd")
    assert component is not None
    archive = validator.prefix / "lib/libserd-0.a"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"accepted archive")
    validator.record_component(component, ["lib/libserd-0.a"])
    assert validator.manifest_matches(component)
    archive.write_bytes(b"corrupted archive")
    assert not validator.manifest_matches(component)

    class State:
        def __init__(self):
            self.components = {"serd": SimpleNamespace(status=wave3a.ComponentStatus.COMPLETED)}

    class Manager:
        def __init__(self):
            self.state = State()
            self.transitions = []

        def get(self):
            return self.state

        def mark_component_status(self, name, status, version, *args, **kwargs):
            self.transitions.append((name, status))
            self.state.components[name] = SimpleNamespace(status=status, version=version)

    class Builder:
        def __init__(self):
            self.calls = []

        def build_component(self, item):
            self.calls.append(item.name)
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"rebuilt archive")

    manager = Manager()
    builder = Builder()
    monkeypatch.setattr(validator, "validate_component", lambda _component: ["lib/libserd-0.a"])
    monkeypatch.setattr(validator, "validate_prefix", lambda: None)
    assert build_or_reuse_component(component, builder, manager, validator)
    assert builder.calls == ["serd"]
    assert validator.manifest_matches(component)


def test_wave3a_dependency_failure_stops_consumer_progression(monkeypatch):
    registry = ComponentRegistry()
    ordered = order_wave_components(registry, _selected(registry))
    attempted = []

    def build(component, *_args):
        attempted.append(component.name)
        if component.name == "sord":
            raise BuildError("sord", "fixture failure")
        return False

    monkeypatch.setattr(wave3a, "build_or_reuse_component", build)
    with pytest.raises(BuildError, match="fixture failure"):
        run_wave_components(ordered, object(), object(), object())
    assert attempted == ["lv2", "serd", "zix", "sord"]


def test_host_only_pkg_config_package_cannot_satisfy_target_lookup(tmp_path):
    host = tmp_path / "host-pkg-config"
    host.write_text(
        "#!/bin/sh\n" 'if [ -n "$PKG_CONFIG_PATH" ]; then exit 1; fi\n' "exit 0\n",
        encoding="utf-8",
    )
    host.chmod(host.stat().st_mode | 0o111)
    environment = SimpleNamespace(
        paths=SimpleNamespace(generated_files_root=tmp_path),
        compiler_environment={},
        pkg_config_wrapper=host,
    )
    reject_host_pkg_config_fallback(environment)
