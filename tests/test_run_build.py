"""Tests for scripts/run_build.py (headless FFmpeg build driver)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("run_build", ROOT / "scripts" / "run_build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_build = _load_module()


def test_parse_args_defaults():
    args = run_build.parse_args([])
    assert args.resume is False
    assert args.ffmpeg_version is None
    assert args.dry_run is False
    assert args.no_release is False


def test_parse_args_accepts_all_options():
    args = run_build.parse_args(
        ["--resume", "--ffmpeg-version", "9.0", "--dry-run", "--no-release"]
    )
    assert args.resume is True
    assert args.ffmpeg_version == "9.0"
    assert args.dry_run is True
    assert args.no_release is True


def _config(**overrides):
    values = {"ffmpeg_version": "8.1", "make_release": False}
    values.update(overrides)
    config = SimpleNamespace(**values)
    config.to_dict = lambda: dict(values)
    return config


def test_describe_plan_lists_components_with_toolchain_cc(capsys):
    gcc = "/store/installed/gcc12-12.5.0/bin/gcc"
    rust_bin = "/store/installed/rust-1.99.0/bin"
    components = [
        SimpleNamespace(name="x264", version="git"),
        SimpleNamespace(name="rav1e", version="0.8.1"),
    ]

    class FakeApp:
        def _get_buildable_components(self, config):
            assert config.ffmpeg_version == "8.1"
            return components

        def _component_toolchain_env(self, config, all_components):
            return {
                "x264": {"CC": gcc},
                "rav1e": {"PATH_PREPEND": rust_bin},
            }

    count = run_build.describe_plan(FakeApp(), _config())
    assert count == 2
    output = capsys.readouterr().out
    assert "FFmpeg 8.1: 2 buildable components" in output
    assert "x264" in output and gcc in output
    assert "rav1e" in output and rust_bin in output


def test_main_dry_run_does_not_build(monkeypatch, capsys):
    seen = {}

    class FakeConfigManager:
        def get(self):
            return _config()

    class FakeApp:
        def __init__(self):
            seen["created"] = True
            self.config_manager = FakeConfigManager()

        def _sync_workspace(self, config):
            seen["workspace"] = True

        def _get_buildable_components(self, config):
            return [SimpleNamespace(name="x264", version="git")]

        def _component_toolchain_env(self, config, components):
            return {}

    monkeypatch.setattr(run_build, "FFmpegBuilderApp", FakeApp)
    assert run_build.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "1 buildable components" in output
    assert seen == {"created": True, "workspace": True}


def test_run_build_stops_at_first_failure(monkeypatch):
    import ffmpeg_builder.app as app_module

    good = SimpleNamespace(name="good", version="1")
    bad = SimpleNamespace(name="bad", version="2")

    class FakeState:
        config = None
        total_steps = 0
        current_step = 0
        components = {}

    class FakeStateManager:
        def __init__(self):
            self.marks = []
            self.state = FakeState()

        def reset(self):
            self.marks.append("reset")

        def get(self):
            return self.state

        def save(self):
            pass

        def get_resume_point(self):
            return None

        def mark_component_status(self, *args):
            self.marks.append(args)

    class FakeBuilder:
        def __init__(self, *args, **kwargs):
            pass

        def prefetch_downloads(self, components):
            pass

        def build_component(self, component):
            if component.name == "bad":
                raise app_module.BuildError("bad", "boom")

        def shutdown_downloads(self, wait=True):
            pass

    monkeypatch.setattr(run_build, "FFmpegBuilder", FakeBuilder)

    class FakeApp:
        workspace = "/tmp/ws"
        packages = "/tmp/ws/packages"
        platform_detector = object()

        def __init__(self):
            self.state_manager = FakeStateManager()

        def _get_buildable_components(self, config):
            return [good, bad]

        def _component_toolchain_env(self, config, components):
            return {}

    config = _config()
    exit_code = run_build.run_build(FakeApp(), config, resume=False, no_release=True)
    assert exit_code == 1
