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
    assert args.g4_wave6 is False


def test_parse_args_accepts_all_options():
    args = run_build.parse_args(
        ["--resume", "--ffmpeg-version", "9.0", "--dry-run", "--no-release"]
    )
    assert args.resume is True
    assert args.ffmpeg_version == "9.0"
    assert args.dry_run is True
    assert args.no_release is True


def test_parse_args_accepts_g4_wave6():
    assert run_build.parse_args(["--g4-wave6"]).g4_wave6 is True
    assert run_build.parse_args(["--g4-wave7"]).g4_wave7 is True
    assert run_build.parse_args(["--g4-wave8"]).g4_wave8 is True
    assert run_build.parse_args(["--g4-wave9"]).g4_wave9 is True


def test_main_g4_wave6_configures_fixed_cross_target(monkeypatch):
    seen = {}

    class FakeConfigManager:
        def get(self):
            return _config()

    class FakeApp:
        def __init__(self):
            self.config_manager = FakeConfigManager()

        def _sync_workspace(self, config):
            seen["workspace_version"] = config.ffmpeg_version

    class FakeDetector:
        def __init__(self, config):
            seen["backend"] = config.target_backend

        def detect_all(self):
            return ("system", "platform", "tools")

    monkeypatch.setattr(run_build, "FFmpegBuilderApp", FakeApp)
    monkeypatch.setattr(run_build, "PlatformDetector", FakeDetector)
    monkeypatch.setattr(
        run_build,
        "run_g4_wave6",
        lambda app, config: seen.update(
            version=config.ffmpeg_version,
            backend=config.target_backend,
            release=config.make_release,
        )
        or 0,
    )
    assert run_build.main(["--g4-wave6"]) == 0
    assert seen == {
        "workspace_version": "9.0",
        "backend": run_build.TARGET_BACKEND_CROSS_MINGW,
        "version": "9.0",
        "release": False,
    }


def test_main_g4_wave7_routes_to_fixed_cross_target(monkeypatch):
    seen = {}

    class FakeApp:
        def __init__(self):
            self.config_manager = SimpleNamespace(get=lambda: _config())

        def _sync_workspace(self, config):
            seen["version"] = config.ffmpeg_version

    class FakeDetector:
        def __init__(self, config):
            pass

        def detect_all(self):
            return (None, None, None)

    monkeypatch.setattr(run_build, "FFmpegBuilderApp", FakeApp)
    monkeypatch.setattr(run_build, "PlatformDetector", FakeDetector)
    monkeypatch.setattr(
        run_build,
        "run_g4_wave7",
        lambda app, config: seen.update(backend=config.target_backend, release=config.make_release)
        or 0,
    )
    assert run_build.main(["--g4-wave7"]) == 0
    assert seen == {
        "version": "9.0",
        "backend": run_build.TARGET_BACKEND_CROSS_MINGW,
        "release": False,
    }


def test_main_g4_wave8_routes_to_fixed_cross_target(monkeypatch):
    seen = {}

    class FakeApp:
        def __init__(self):
            self.config_manager = SimpleNamespace(get=lambda: _config())

        def _sync_workspace(self, config):
            seen["version"] = config.ffmpeg_version

    class FakeDetector:
        def __init__(self, config):
            pass

        def detect_all(self):
            return (None, None, None)

    monkeypatch.setattr(run_build, "FFmpegBuilderApp", FakeApp)
    monkeypatch.setattr(run_build, "PlatformDetector", FakeDetector)
    monkeypatch.setattr(
        run_build,
        "run_g4_wave8",
        lambda app, config: seen.update(backend=config.target_backend, release=config.make_release)
        or 0,
    )
    assert run_build.main(["--g4-wave8"]) == 0
    assert seen == {
        "version": "9.0",
        "backend": run_build.TARGET_BACKEND_CROSS_MINGW,
        "release": False,
    }


def test_main_g4_wave9_routes_to_fixed_cross_target(monkeypatch):
    seen = {}

    class FakeApp:
        def __init__(self):
            self.config_manager = SimpleNamespace(get=lambda: _config())

        def _sync_workspace(self, config):
            seen["version"] = config.ffmpeg_version

    class FakeDetector:
        def __init__(self, config):
            pass

        def detect_all(self):
            return (None, None, None)

    monkeypatch.setattr(run_build, "FFmpegBuilderApp", FakeApp)
    monkeypatch.setattr(run_build, "PlatformDetector", FakeDetector)
    monkeypatch.setattr(
        run_build,
        "run_g4_wave9",
        lambda app, config: seen.update(backend=config.target_backend, release=config.make_release)
        or 0,
    )
    assert run_build.main(["--g4-wave9"]) == 0
    assert seen == {
        "version": "9.0",
        "backend": run_build.TARGET_BACKEND_CROSS_MINGW,
        "release": False,
    }


def test_main_rejects_g4_wave6_dry_run_and_multiple_waves():
    import pytest

    with pytest.raises(SystemExit, match="cannot be combined with --dry-run"):
        run_build.main(["--g4-wave6", "--dry-run"])
    with pytest.raises(SystemExit, match="mutually exclusive"):
        run_build.main(["--g4-wave6", "--g4-wave5c"])
    with pytest.raises(SystemExit, match="cannot be combined with --dry-run"):
        run_build.main(["--g4-wave7", "--dry-run"])
    with pytest.raises(SystemExit, match="mutually exclusive"):
        run_build.main(["--g4-wave7", "--g4-wave6"])
    with pytest.raises(SystemExit, match="cannot be combined with --dry-run"):
        run_build.main(["--g4-wave8", "--dry-run"])
    with pytest.raises(SystemExit, match="mutually exclusive"):
        run_build.main(["--g4-wave8", "--g4-wave7"])
    with pytest.raises(SystemExit, match="cannot be combined with --dry-run"):
        run_build.main(["--g4-wave9", "--dry-run"])
    with pytest.raises(SystemExit, match="mutually exclusive"):
        run_build.main(["--g4-wave9", "--g4-wave8"])


def _config(**overrides):
    values = {
        "ffmpeg_version": "8.1",
        "make_release": False,
        "enable_libvmaf": True,
        "enable_libvmaf_cuda": True,
        "gpl_enabled": False,
        "allow_nonfree_cuda": False,
        "cuda_build_mode": "auto",
    }
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
        platform_detector = SimpleNamespace(
            platform_info=SimpleNamespace(cuda_build_host_inputs=None)
        )

        def __init__(self):
            self.state_manager = FakeStateManager()

        def _get_buildable_components(self, config):
            return [good, bad]

        def prepare_target(self, config, resume):
            return SimpleNamespace(notes=[], stamp=lambda manager: None)

        def recheck_target_runtime(self, config, session):
            return None

        def _component_toolchain_env(self, config, components):
            return {}

    config = _config()
    exit_code = run_build.run_build(FakeApp(), config, resume=False, no_release=True)
    assert exit_code == 1


def test_icx_partial_required_cuda_build_requires_matching_dependencies():
    from ffmpeg_builder.config import BuildConfig
    from ffmpeg_builder.state import ComponentState, ComponentStatus

    spec = importlib.util.spec_from_file_location("icx_poc", ROOT / "scripts" / "icx_poc.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = BuildConfig(cuda_build_mode="required", gpl_enabled=True)
    components = [
        SimpleNamespace(name="nv-codec", version="13"),
        SimpleNamespace(name="libvmaf", version="3"),
        SimpleNamespace(name="ffmpeg", version="8"),
    ]
    selected = [components[2]]
    assert module.missing_required_cuda_dependencies(config, selected, components, {}) == [
        "libvmaf",
        "nv-codec",
    ]
    states = {
        "nv-codec": ComponentState(status=ComponentStatus.COMPLETED, version="13"),
        "libvmaf": ComponentState(status=ComponentStatus.COMPLETED, version="3"),
    }
    assert not module.missing_required_cuda_dependencies(config, selected, components, states)
    states["libvmaf"].status = ComponentStatus.PENDING
    assert module.missing_required_cuda_dependencies(config, selected, components, states) == [
        "libvmaf"
    ]
