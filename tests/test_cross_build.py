from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.components import BuildRole
from ffmpeg_builder.cross_build import dependency_order


def component(name, *dependencies):
    return SimpleNamespace(name=name, depends_on=list(dependencies))


def test_dependency_order_keeps_ffmpeg_last_and_orders_dependencies():
    selected = [
        component("ffmpeg"),
        component("libvorbis", "libogg"),
        component("libogg"),
    ]
    assert [item.name for item in dependency_order(selected)] == [
        "libogg",
        "libvorbis",
        "ffmpeg",
    ]


@pytest.mark.parametrize(
    "selected",
    [
        [component("ffmpeg"), component("a", "missing")],
        [component("ffmpeg"), component("a", "b"), component("b", "a")],
        [component("ffmpeg"), component("a"), component("a")],
        [component("a")],
    ],
)
def test_dependency_order_rejects_incomplete_or_ambiguous_graph(selected):
    with pytest.raises(BuildError):
        dependency_order(selected)


def test_normal_cross_workflow_builds_clean_then_reuses_without_waves(tmp_path, monkeypatch):
    import json

    from ffmpeg_builder import cross_build, cross_component_contracts, g6
    from ffmpeg_builder.components import ComponentSelection
    from ffmpeg_builder.config import BuildConfig
    from ffmpeg_builder.state import ComponentStatus, StateManager

    dependency, ffmpeg = component("zlib"), component("ffmpeg")
    for item in (dependency, ffmpeg):
        item.version = "test"
        item.build_role = BuildRole.TARGET_COMPONENT
        item.sha256 = None
        item.get_url = lambda: "source"
        item.get_archive_filename = lambda: "source.tar"
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"source")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    manager = StateManager(tmp_path / "build_state.json")
    identity = SimpleNamespace(build_key="identity", to_dict=lambda: {"build_key": "identity"})
    session = SimpleNamespace(
        workspace=tmp_path, identity=identity, stamp=lambda manager: None, notes=[]
    )
    environment = SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix))
    app = SimpleNamespace(
        workspace=tmp_path,
        packages=tmp_path / "packages",
        platform_detector=None,
        windows_target_state=None,
        state_manager=manager,
        config_manager=SimpleNamespace(config_path=tmp_path / "BASE.yaml"),
        prepare_target=lambda config, resume: session,
        prepare_cross_environment=lambda config, session: environment,
        _get_component_selection=lambda config: ComponentSelection(
            target_components_to_build=[dependency, ffmpeg]
        ),
        _component_toolchain_env=lambda config, components: {},
    )
    calls = []

    class Builder:
        def __init__(self, *args, **kwargs):
            self.downloader = SimpleNamespace(download=lambda *args, **kwargs: archive)

        def prefetch_downloads(self, components):
            pass

        def build_component(self, component):
            calls.append(component.name)
            (prefix / "zlib.a").write_bytes(b"target")

        def shutdown_downloads(self, **kwargs):
            pass

    class Validator:
        def manifest_matches(self, component):
            return (prefix / "zlib.a").read_bytes() == b"target"

        def validate_component(self, component):
            return ["zlib.a"]

        def record_component(self, component, outputs):
            pass

    def final(app, config, **kwargs):
        if not manager.is_component_completed("ffmpeg", "test"):
            calls.append("ffmpeg")
        (tmp_path / "g4-final-acceptance.json").write_text(
            json.dumps({"configure_flags": ["--enable-zlib"]})
        )
        manager.mark_component_status(
            "ffmpeg", ComponentStatus.COMPLETED, "test", detail="accepted"
        )
        return 0

    monkeypatch.setattr(cross_build, "FFmpegBuilder", Builder)
    monkeypatch.setattr(cross_component_contracts, "component_validator", lambda *args: Validator())
    monkeypatch.setattr(cross_build, "run_g4_final", final)
    monkeypatch.setattr(cross_build, "validate_prior_manifests", lambda *args: 0)
    monkeypatch.setattr(g6, "run_g7_package", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        cross_build,
        "Wave2TargetValidator",
        lambda *args: SimpleNamespace(validate_prefix=lambda: None),
    )
    config = BuildConfig(ffmpeg_version="9.0", target_backend="linux-cross-mingw")
    assert cross_build.run_cross_build(app, config) == 0
    first = json.loads((tmp_path / "g7-acceptance.json").read_text())
    assert first["built"] == ["zlib", "ffmpeg"]
    assert first["clean_full_build"]
    assert calls == ["zlib", "ffmpeg"]
    assert cross_build.run_cross_build(app, config) == 0
    second = json.loads((tmp_path / "g7-acceptance.json").read_text())
    assert second["identical_run_reuse"]
    assert not second["clean_full_build"]
    assert second["reused"] == ["zlib", "ffmpeg"]
    assert calls == ["zlib", "ffmpeg"]
    (prefix / "zlib.a").write_bytes(b"corrupted")
    assert cross_build.run_cross_build(app, config) == 1
    assert json.loads((tmp_path / "g7-acceptance.json").read_text())["status"] == "RUNNING"
