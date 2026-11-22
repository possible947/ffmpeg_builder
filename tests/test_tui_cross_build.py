"""Exercise the shared TUI entry point without running Windows builds."""

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from ffmpeg_builder import app as app_module, cross_build
from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.config import BuildConfig, ConfigManager
from ffmpeg_builder.platform_detect import PlatformDetector, PlatformInfo, SystemInfo
from ffmpeg_builder.scripts import run_build
from ffmpeg_builder.state import BuildState, ComponentStatus, StateManager
from ffmpeg_builder.ui import screens
from ffmpeg_builder.windows_probe import FeatureState


@pytest.fixture
def tui(tmp_path, monkeypatch):
    detections = []

    def detect(detector):
        detections.append(detector.config.target_backend)
        detector.platform_info = PlatformInfo(
            platform="linux",
            is_linux=True,
            is_wsl2=True,
            build_backend="linux-wsl2",
            gcc_major_version=16,
            vulkan_available=detector.config.target_backend == "native",
        )
        return SystemInfo(), detector.platform_info, {}

    monkeypatch.setattr(PlatformDetector, "detect_all", detect)
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("FFMPEG_BUILDER_TOOLCHAINS", str(tmp_path / "store"))
    ConfigManager(tmp_path / "build_config.yaml").save(BuildConfig(ffmpeg_version="9.0"))
    app = app_module.FFmpegBuilderApp()
    app.console = Console(file=io.StringIO(), width=120)
    app.final_screen.console = app.console
    monkeypatch.setattr(app.console, "input", lambda *args: "")
    return app, detections


def _cross_config(version="9.0"):
    return BuildConfig(ffmpeg_version=version, target_backend="linux-cross-mingw")


def _state(workspace, config, build_id):
    StateManager(workspace / "build_state.json").save(
        BuildState(build_id=build_id, config=config.to_dict())
    )


def _target():
    return SimpleNamespace(
        os=SimpleNamespace(product_name="Windows 11", version="10.0", architecture="x86_64"),
        nvidia=SimpleNamespace(state=FeatureState.PRESENT, api={}),
        vulkan=SimpleNamespace(state=FeatureState.PRESENT),
        opencl=SimpleNamespace(state=FeatureState.ABSENT),
    )


def test_tui_selects_state_and_orchestrator_and_redetects_both_directions(tui, monkeypatch):
    app, detections = tui
    root = app.workspace.parent
    native = app.config_manager.get()
    windows = _cross_config()
    windows.make_release = True
    win_root = windows.get_workspace_path(root)
    win_target = win_root / "targets/linux-cross-mingw-matching"
    _state(app.workspace, native, "native-run")
    _state(win_root, native, "not-a-Windows-target")
    _state(win_target, windows, "windows-run")
    target = _target()
    probes = []

    def probe(*args, **kwargs):
        assert app.wsl_mingw_preparation is None
        assert app.cross_environment is None
        probes.append(app.platform_info)
        return target

    app.windows_probe = probe
    actions = iter(["resume", "config", "build", "resume", "config", "exit"])
    displays = []

    def show(report, config, state, count, policy, **kwargs):
        displays.append((config.target_backend, state.build_id, kwargs["previous_workspace"]))
        if config.target_backend == windows.target_backend:
            assert kwargs["windows_target"] is target
        else:
            assert app.cross_environment is None
            assert kwargs["windows_target"] is None
            assert report.platform_info.vulkan_available
        return next(actions)

    monkeypatch.setattr(app.system_screen, "show", show)
    configs = iter([windows, native])
    monkeypatch.setattr(app.config_screen, "show", lambda *args: next(configs))
    calls = []

    def cross(actual_app, config, *, resume, **kwargs):
        assert actual_app is app
        assert app.base_workspace == win_root
        assert app.state_manager.state_path == win_root / "build_state.json"
        assert not app.platform_info.vulkan_available
        calls.append((config.target_backend, resume))
        app.cross_environment = object()
        app.workspace = win_target
        (win_target / "release").mkdir(exist_ok=True)
        return 0

    def native_prepare(config, resume):
        calls.append((config.target_backend, resume))
        assert app.state_manager.get().build_id == "native-run"
        raise BuildError("test", "stop before native components")

    monkeypatch.setattr(cross_build, "run_cross_build", cross)
    monkeypatch.setattr(app, "prepare_target", native_prepare)

    assert app.run() == 0
    assert calls == [("native", True), ("linux-cross-mingw", False), ("linux-cross-mingw", True)]
    assert displays == [
        ("native", "native-run", None),
        ("native", "native-run", None),
        ("linux-cross-mingw", "windows-run", win_target),
        ("linux-cross-mingw", "windows-run", win_target),
        ("linux-cross-mingw", "windows-run", win_target),
        ("native", "native-run", None),
    ]
    assert detections == ["native", "linux-cross-mingw", "native"]
    assert len(probes) == 1 and probes[0] is not app.platform_info
    assert str(win_target / "bin") in app.console.file.getvalue()
    assert str(win_target / "release") in app.console.file.getvalue()


def test_windows_cleanup_removes_whole_selected_root_only(tui, monkeypatch):
    app, _ = tui
    root = app.workspace.parent
    native_root = app.workspace
    windows = _cross_config()
    win_root = windows.get_workspace_path(root)
    assert (native_root.name, win_root.name) == ("workspace_90", "workspace_90_windows")
    _state(native_root, app.config_manager.get(), "native-run")
    for identity in ("one", "two"):
        _state(win_root / "targets" / f"linux-cross-mingw-{identity}", windows, identity)
    (win_root / "all-artifacts").touch()
    (root / "workspace").mkdir()

    app.config_manager.save(windows)
    app.windows_probe = lambda *args, **kwargs: _target()
    actions = iter(["cleanup", "exit"])
    monkeypatch.setattr(app.system_screen, "show", lambda *args, **kwargs: next(actions))

    assert app.run() == 0
    assert not win_root.exists()
    assert native_root.is_dir()
    assert (root / "workspace").is_dir()


def test_cleanup_refuses_symlinked_workspace_without_touching_target(tui):
    app, _ = tui
    root = app.workspace.parent
    actual = root / "other-workspace"
    (actual / "packages").mkdir(parents=True)
    artifact = actual / "packages" / "artifact"
    artifact.touch()
    mode = actual.stat().st_mode
    config = _cross_config()
    workspace = config.get_workspace_path(root)
    workspace.symlink_to(actual, target_is_directory=True)
    app._sync_workspace(config)

    app._cleanup(remove_legacy=False)

    assert workspace.is_symlink() and artifact.exists()
    assert actual.stat().st_mode == mode
    assert "Cleanup refused" in app.console.file.getvalue()
    assert "Removed" not in app.console.file.getvalue()


def test_cleanup_removes_workspace_with_read_only_entries(tui):
    app, _ = tui
    config = _cross_config()
    workspace = config.get_workspace_path(app.workspace.parent)
    locked = workspace / "packages" / "locked"
    locked.mkdir(parents=True)
    (locked / "file").touch()
    (locked / "file").chmod(0o444)
    locked.chmod(0o555)
    app._sync_workspace(config)

    try:
        app._cleanup(remove_legacy=False)
    finally:
        if locked.exists():
            locked.chmod(0o755)

    assert not workspace.exists()
    assert f"Removed {workspace}" in app.console.file.getvalue()


def test_manually_configured_81_cross_blocks_tui_cli_and_target_preparation(
    tui, monkeypatch, capsys
):
    app, _ = tui
    app.config_manager.save(_cross_config("8.1"))
    config = app.config_manager.load()
    app.windows_probe = lambda *args, **kwargs: pytest.fail("unsupported target was probed")
    monkeypatch.setattr(
        app, "_get_component_selection", lambda *args: pytest.fail("components were resolved")
    )
    actions = iter(["build", "exit"])
    monkeypatch.setattr(app.system_screen, "show", lambda *args, **kwargs: next(actions))

    assert app.run() == 0
    assert "supports FFmpeg 9.0 only" in app.console.file.getvalue()
    with pytest.raises(BuildError, match="supports FFmpeg 9.0 only"):
        app.prepare_target(config, resume=False)
    assert run_build.run_build(app, config, resume=False, no_release=False) == 2
    assert "supports FFmpeg 9.0 only" in capsys.readouterr().out
    assert not app.workspace.exists()


def test_tui_store_passes_required_profiles_even_if_currently_installed(tui, monkeypatch):
    app, _ = tui
    config = _cross_config()
    config.enable_libvmaf_cuda = False
    store = SimpleNamespace(
        list_installed=lambda: [SimpleNamespace(profile="llvm-mingw")],
        installed_status=lambda *args: ("compatible", ""),
    )
    monkeypatch.setattr(app, "_toolchain_store", lambda config: store)
    displayed = {}
    monkeypatch.setattr(app.store_screen, "show", lambda *args, **kwargs: displayed.update(kwargs))

    app._show_toolchain_store(config)

    assert {name for name, _ in displayed["recommended"]} >= {"llvm-mingw", "rust", "cargo-c"}


@pytest.mark.parametrize("cuda_version", ["12.4", "12.8"])
def test_tui_windows_status_components_toolchain_plan_and_store(tui, monkeypatch, cuda_version):
    app, _ = tui
    root = app.workspace.parent
    config = _cross_config()
    cuda_root = root / "cuda-sdk"
    nvcc = cuda_root / "bin" / "nvcc"
    nvcc.parent.mkdir(parents=True)
    nvcc.write_text(
        f"#!/bin/sh\nprintf 'Cuda compilation tools, release {cuda_version}, V{cuda_version}.99\\n'\n"
    )
    nvcc.chmod(0o755)
    config.external_prerequisite_paths["cuda"] = str(cuda_root)
    app.config_manager.save(config)
    win_target = config.get_workspace_path(root) / "targets/linux-cross-mingw-probe"
    _state(win_target, config, "windows-run")
    app.windows_probe = lambda *args, **kwargs: _target()
    for screen in (app.system_screen, app.info_screen, app.toolchain_screen, app.store_screen):
        screen.console = app.console
    monkeypatch.setattr(app, "_host_tool_available", lambda name: name not in ("nasm", "yasm"))
    monkeypatch.setenv("PATH", str(root / "empty-path"))
    actions = iter(["i", "q", "t", "", "p", "q", "q"])
    choices = []

    def ask(prompt, **kwargs):
        choices.append(kwargs.get("choices", []))
        return next(actions)

    monkeypatch.setattr(screens.Prompt, "ask", staticmethod(ask))

    assert app.run() == 0
    output = app.console.file.getvalue()
    assert "linux-wsl2" in output and "Windows 11" in output
    host_info = output.split("Target toolchains", 1)[0].split("Required host tools", 1)[1]
    assert "cc," in host_info
    assert all(tool not in host_info for tool in ("rustc", "cargo", "nasm", "yasm"))
    assert "nasm" in output and "yasm" in output
    assert "VAAPI detect" not in output
    assert "Previous Build" in output and "windows-run" in output
    assert "r" in choices[0]
    assert "conditional on target capabilities" in output or "not supported by target" in output
    assert "deploy: llvm-mingw" in output
    assert "Required target profiles" in output
    assert "cargo-c" in output and "not installed" in output
    if cuda_version == "12.4":
        assert "CUDA 12.4 host compiler" in output
        assert any("gcc12" in line and "absent" in line for line in output.splitlines())
    else:
        assert output.count("CUDA 12.8 has no project-managed host compiler mapping") == 2


def test_cross_tui_uses_production_reuse_and_existing_lifecycle(tui, tmp_path, monkeypatch):
    import json

    from ffmpeg_builder import cross_component_contracts, g6
    from ffmpeg_builder.components import ComponentSelection
    from ffmpeg_builder.ui.dashboard import BuildDashboard

    app, _ = tui
    config = _cross_config()
    app.console = Console(file=io.StringIO(), width=120, force_terminal=True, color_system=None)
    app.final_screen.console = app.console
    workspace = tmp_path / "target"
    workspace.mkdir()
    app.workspace = workspace
    app.packages = workspace / "packages"
    app.state_manager = StateManager(workspace / "build_state.json")
    manager = app.state_manager
    components = {c.name: c for c in app.registry.get_all()}
    dependency, ffmpeg = components["zlib"], components["ffmpeg"]
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"test source")
    for item in (dependency, ffmpeg):
        manager.mark_component_status(item.name, ComponentStatus.COMPLETED, item.version)
    before = manager.get().to_dict()
    identity = SimpleNamespace(build_key="test-key", to_dict=lambda: {})
    session = SimpleNamespace(
        workspace=workspace, identity=identity, notes=[], stamp=lambda _: None
    )
    environment = SimpleNamespace(paths=SimpleNamespace(target_prefix=workspace / "prefix"))
    selection = ComponentSelection(
        target_components_to_build=[dependency, ffmpeg],
        host_tools=[components["nasm"]],
        conditional_components=[components["opencl-headers"]],
    )
    phases = []

    def prepare(config, resume):
        assert manager.get().to_dict() == before
        phases.append("prepare")
        return session

    monkeypatch.setattr(app, "prepare_target", prepare)
    monkeypatch.setattr(app, "prepare_cross_environment", lambda *args: environment)
    monkeypatch.setattr(app, "_get_component_selection", lambda config: selection)
    monkeypatch.setattr(app, "_component_toolchain_env", lambda *args: {})
    monkeypatch.setattr(cross_build, "implementation_digest", lambda: "implementation")
    monkeypatch.setattr(cross_build, "validate_prior_manifests", lambda *args: 0)
    monkeypatch.setattr(
        cross_build,
        "Wave2TargetValidator",
        lambda *args: SimpleNamespace(validate_prefix=lambda: None),
    )

    class Builder:
        def __init__(self, *args, **kwargs):
            self.downloader = SimpleNamespace(download=lambda *args, **kwargs: archive)

        def prefetch_downloads(self, components):
            pass

        def build_component(self, component):
            pytest.fail("reuse must not rebuild components")

        def shutdown_downloads(self, **kwargs):
            pass

    class Validator:
        def manifest_matches(self, component):
            return True

        def validate_component(self, component):
            return []

        def record_component(self, component, outputs):
            pass

    monkeypatch.setattr(cross_build, "FFmpegBuilder", Builder)
    monkeypatch.setattr(cross_component_contracts, "component_validator", lambda *args: Validator())

    def final(app, config, **kwargs):
        kwargs["on_message"]("Validating FFmpeg integration")
        (workspace / "g4-final-acceptance.json").write_text(json.dumps({"configure_flags": []}))
        manager.mark_component_status(
            "ffmpeg", ComponentStatus.COMPLETED, ffmpeg.version, detail="accepted"
        )
        return 0

    def package(*args, **kwargs):
        kwargs["on_message"]("Final runtime accepted")
        return 0

    monkeypatch.setattr(cross_build, "run_g4_final", final)
    monkeypatch.setattr(g6, "run_g7_package", package)
    dashboard_instances = []

    class Dashboard(BuildDashboard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.updates = []
            dashboard_instances.append(self)

        def update_status(self, name, status, version=None, detail=""):
            self.updates.append((name, status, detail))
            super().update_status(name, status, version, detail)

    lives = []

    class Live:
        def __init__(self, *args, **kwargs):
            self.stopped = False
            lives.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(app_module, "BuildDashboard", Dashboard)
    monkeypatch.setattr(app_module, "Live", Live)
    monkeypatch.setattr(
        "subprocess.Popen", lambda *args, **kwargs: pytest.fail("subprocess launched")
    )
    (workspace / "bin").mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (workspace / "bin" / name).touch()
    original_listener = lambda *args: None
    manager.status_listener = original_listener

    app._run_build(config, resume=True)

    dashboard = dashboard_instances[0]
    assert phases == ["prepare"]
    assert {row.name for row in dashboard.rows} == {"zlib", "ffmpeg"}
    assert (
        "zlib",
        ComponentStatus.COMPLETED,
        "Windows target output contract accepted",
    ) in dashboard.updates
    assert ("ffmpeg", ComponentStatus.COMPLETED, "accepted") in dashboard.updates
    assert all(row.detail == "reused" for row in dashboard.rows)
    assert any("Final runtime accepted" in message for message in dashboard.messages)
    assert lives[0].stopped and manager.status_listener is original_listener
    assert json.loads((workspace / "g7-acceptance.json").read_text())["identical_run_reuse"]
    assert manager.get().build_id == before["build_id"]
    output = app.console.file.getvalue()
    assert (
        "Preparing Windows target" in output and "ffmpeg.exe" in output and "ffprobe.exe" in output
    )
    assert "Install binaries" not in output


@pytest.mark.parametrize("outcome", ["failure", "interrupt"])
def test_cross_dashboard_stops_on_failure_and_interrupt(tui, monkeypatch, outcome):
    app, _ = tui
    app.console = Console(file=io.StringIO(), width=120, force_terminal=True, color_system=None)
    app.final_screen.console = app.console
    component = next(c for c in app.registry.get_all() if c.name == "ffmpeg")
    lives = []

    class Live:
        def __init__(self, *args, **kwargs):
            self.stopped = False
            lives.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    def runner(app, config, **hooks):
        hooks["on_plan"]([component])
        app.state_manager.mark_component_status(
            "ffmpeg", ComponentStatus.FAILED, component.version, "compiler failed", "/tmp/build.log"
        )
        if outcome == "interrupt":
            raise KeyboardInterrupt
        hooks["on_message"]("FAIL: ffmpeg: compiler failed")
        return 1

    monkeypatch.setattr(app_module, "Live", Live)
    monkeypatch.setattr(cross_build, "run_cross_build", runner)
    if outcome == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            app._run_build(_cross_config())
    else:
        app._run_build(_cross_config())
        text = app.console.file.getvalue()
        assert "compiler failed" in text and "/tmp/build.log" in text and "Resume" in text
    assert lives[0].stopped
    assert app.state_manager.status_listener is None
    assert app.state_manager.state_path.exists()


def test_windows_target_display_uses_probe_not_wsl_opencl(tui, monkeypatch, tmp_path):
    app, _ = tui
    config = _cross_config()
    target = _target()
    target.opencl.state = FeatureState.PRESENT
    app.platform_info.opencl_runtime_available = False
    app.windows_target_state = target
    assert app._cross_target_capabilities(config).opencl_runtime_available
    screen = screens.SystemReportScreen(app.console)
    monkeypatch.setattr(screen, "_ask_action", lambda *args: "exit")
    monkeypatch.setattr(app, "prepare_target", lambda *args: pytest.fail("display prepared target"))
    screen.show(app.system_report, config, windows_target=target)
    output = app.console.file.getvalue()
    assert "Windows OpenCL runtime" in output and "present" in output
    assert "OpenCL target build interface" in output and "not prepared" in output
    assert "WSL OpenCL" in output and "not required for Windows" in output
    assert "LLVM-MinGW" in output and "x86_64-w64-mingw32" in output
    app.console.file.seek(0)
    app.console.file.truncate()
    screen.show(app.system_report, config, target_probe_error="interop unavailable")
    output = app.console.file.getvalue()
    assert "unavailable/not probed" in output and "interop unavailable" in output
    headers = tmp_path / "CL"
    headers.mkdir()
    library = tmp_path / "libOpenCL.a"
    library.touch()
    env = SimpleNamespace(
        paths=SimpleNamespace(workspace_root=tmp_path, target_prefix=tmp_path),
        target_interfaces=SimpleNamespace(opencl_headers=headers, opencl_import_library=library),
    )
    app.console.file.seek(0)
    app.console.file.truncate()
    screen.show(app.system_report, config, windows_target=target, cross_environment=env)
    assert "OpenCL target build interface" in app.console.file.getvalue()
    assert "prepared" in app.console.file.getvalue()


def test_windows_component_info_displays_roles(tui, monkeypatch):
    from ffmpeg_builder.components import ComponentSelection

    app, _ = tui
    components = {c.name: c for c in app.registry.get_all()}
    names = ["zlib", "nasm", "opencl-headers", "glslang", "libplacebo", "lv2", "giflib"]
    items = [components[name] for name in names]
    selection = ComponentSelection(
        target_components_to_build=[items[0]],
        host_tools=[items[1]],
        target_inputs=[items[2]],
        mixed_components=[items[3]],
        conditional_components=[items[4]],
        skipped_by_feature=[items[5]],
        skipped_by_backend=[items[6]],
    )
    app.console = Console(file=io.StringIO(), width=180, height=50)
    monkeypatch.setattr(screens.Prompt, "ask", lambda *args, **kwargs: "q")
    screens.InfoScreen(app.console).show(
        selection.production_components, items, selection=selection
    )
    output = app.console.file.getvalue()
    for label in (
        "selected target component",
        "host tool: missing",
        "target input",
        "mixed component",
        "conditional on target capabilities",
        "disabled by configuration",
        "not supported by target",
    ):
        assert label in output


def test_preparation_failure_is_reported_without_mutating_state(tui, monkeypatch):
    app, _ = tui
    app.final_screen.console = app.console
    manager = app.state_manager
    manager.save(BuildState(build_id="preserved"))
    before = manager.state_path.read_bytes()

    def blocked(app, config, **hooks):
        hooks["on_message"]("Preparing target")
        hooks["on_message"]("BLOCKED by identity guard: missing toolchain")
        return 2

    monkeypatch.setattr(cross_build, "run_cross_build", blocked)
    app._run_build(_cross_config(), resume=True)
    output = app.console.file.getvalue()
    assert "missing toolchain" in output and "Resume" in output
    assert manager.state_path.read_bytes() == before


def test_cross_dashboard_receives_live_component_transitions(tui, monkeypatch):
    from ffmpeg_builder.ui.dashboard import BuildDashboard

    app, _ = tui
    component = next(c for c in app.registry.get_all() if c.name == "zlib")
    dashboards = []

    class Dashboard(BuildDashboard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.observed = []
            dashboards.append(self)

        def update_status(self, name, status, version=None, detail=""):
            self.observed.append((name, status, detail))
            super().update_status(name, status, version, detail)

    def runner(app, config, **hooks):
        hooks["on_plan"]([component])
        for status in (
            ComponentStatus.CONFIGURING,
            ComponentStatus.BUILDING,
            ComponentStatus.INSTALLING,
        ):
            app.state_manager.mark_component_status(
                component.name, status, component.version, detail=status.value
            )
        app.state_manager.mark_component_status(
            component.name, ComponentStatus.COMPLETED, component.version, detail="accepted"
        )
        return 0

    monkeypatch.setattr(app_module, "BuildDashboard", Dashboard)
    monkeypatch.setattr(cross_build, "run_cross_build", runner)
    app._run_build(_cross_config())
    assert [item[1] for item in dashboards[0].observed] == [
        ComponentStatus.CONFIGURING,
        ComponentStatus.BUILDING,
        ComponentStatus.INSTALLING,
        ComponentStatus.COMPLETED,
    ]
    assert dashboards[0].status_only
    assert app.state_manager.get().components["zlib"].status == ComponentStatus.COMPLETED


def test_native_final_report_keeps_install_prompt(tui, tmp_path, monkeypatch):
    app, _ = tui
    prompts = []
    monkeypatch.setattr(
        screens.Confirm, "ask", lambda prompt, **kwargs: prompts.append(prompt) or False
    )
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ffmpeg").touch()
    screens.FinalReportScreen(app.console).show(BuildState(), tmp_path, True)
    assert prompts == ["Install binaries to system?"]
    assert "ffmpeg.exe" not in app.console.file.getvalue()


@pytest.mark.parametrize("success", [True, False])
def test_cross_results_close_real_live_before_report_and_return_to_menu(tui, monkeypatch, success):
    from rich.live import Live

    from ffmpeg_builder.components import ComponentSelection

    app, _ = tui
    config = _cross_config()
    config.make_release = True
    app.config_manager.save(config)
    app.windows_probe = lambda *args, **kwargs: _target()
    app.console = Console(file=io.StringIO(), width=160, force_terminal=True, color_system=None)
    app.final_screen.console = app.console
    component = next(c for c in app.registry.get_all() if c.name == "ffmpeg")
    monkeypatch.setattr(
        app,
        "_get_component_selection",
        lambda config: ComponentSelection(target_components_to_build=[component]),
    )
    events = []
    lives = []

    class ObservedLive(Live):
        def start(self, *args, **kwargs):
            super().start(*args, **kwargs)
            lives.append(self)

    monkeypatch.setattr(app_module, "Live", ObservedLive)

    def menu(*args, **kwargs):
        events.append("menu")
        return "build" if len(events) == 1 else "exit"

    monkeypatch.setattr(app.system_screen, "show", menu)

    def runner(app, config, **hooks):
        hooks["on_plan"]([component])
        assert lives[0].is_started
        bin_dir = app.workspace / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
            (bin_dir / name).touch()
        (app.workspace / "release").mkdir()
        app.state_manager.mark_component_status(
            "ffmpeg",
            ComponentStatus.COMPLETED if success else ComponentStatus.FAILED,
            component.version,
            None if success else "acceptance failed",
        )
        hooks["on_message"]("PASS: done" if success else "FAIL: acceptance failed")
        events.append("runner returned")
        return 0 if success else 1

    monkeypatch.setattr(cross_build, "run_cross_build", runner)
    show = app.final_screen.show

    def results(*args, **kwargs):
        assert not lives[0].is_started
        assert kwargs["target_backend"] == "linux-cross-mingw"
        assert app.state_manager.status_listener is None
        events.append("results")
        show(*args, **kwargs)

    monkeypatch.setattr(app.final_screen, "show", results)
    monkeypatch.setattr(
        screens.Confirm, "ask", lambda *args, **kwargs: pytest.fail("native install prompt")
    )

    def dismiss(prompt):
        assert prompt == "Press Enter to continue..."
        assert not lives[0].is_started
        events.append("dismiss")
        return ""

    monkeypatch.setattr(app.console, "input", dismiss)

    assert app.run() == 0
    assert events == ["menu", "runner returned", "results", "dismiss", "menu"]
    output = app.console.file.getvalue()
    title = "Windows Build Completed Successfully!" if success else "Windows Build Failed"
    assert output.rfind("\x1b[?1049l") < output.index(title)
    assert "Component Summary" in output
    if success:
        for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
            assert str(app.workspace / "bin" / name) in output
        assert str(app.workspace / "release") in output
    else:
        assert "acceptance failed" in output and "Resume" in output
    assert "Install binaries to system?" not in output


_VERSION_DUMP = "\n".join(
    ["ffmpeg version 9.0 Copyright (c) 2000-2026 the FFmpeg developers\r"]
    + [
        f"configuration: --enable-feature-{i} " + "--extra-cflags=-I/very/long/path " * 4
        for i in range(40)
    ]
)
_SIZES = [(120, 30), (80, 24), (60, 14), (40, 8), (20, 5), (10, 3), (5, 1)]


def _frames(output, height):
    """Return the alternate-screen frames Rich actually wrote, as text lines."""
    import re

    start, end = output.index("\x1b[?1049h"), output.rindex("\x1b[?1049l")
    frames = []
    for raw in output[start:end].split("\x1b[H")[1:]:
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
        if text.strip():
            frames.append(text.split("\n"))
    return frames


@pytest.mark.parametrize("width,height", _SIZES)
def test_dashboard_never_renders_taller_or_wider_than_viewport(width, height):
    from rich.segment import Segment

    from ffmpeg_builder.ui.dashboard import BuildDashboard

    console = Console(file=io.StringIO(), width=width, height=height, color_system=None)
    dashboard = BuildDashboard(console, "9.0", 4, 0, status_only=True)
    components = [SimpleNamespace(name=f"component-{i}", version="1.0") for i in range(46)]
    dashboard.set_components(components, reveal_all=True)
    dashboard.update_status("component-45", ComponentStatus.BUILDING, detail="x" * 300)
    dashboard.log("\n=== [46/46] ffmpeg 9.0 ===")
    dashboard.log(_VERSION_DUMP)
    dashboard.log("Wave-9 PE imports: " + "'api-ms-win-crt-heap-l1-1-0.dll', " * 20)
    dashboard.log("界" * 200)
    dashboard.log("newest message")
    for size in ((width, height), (max(5, width // 2), max(1, height - 4)), (width, height)):
        console.size = size
        lines = list(Segment.split_lines(console.render(dashboard, console.options)))
        assert len(lines) == size[1]
        assert all(Segment.get_line_length(line) <= size[0] for line in lines)
        text = ["".join(s.text for s in line) for line in lines]
        if size[1] >= 4:
            assert text[-1].startswith("╰") and text[-1].rstrip().endswith("╯")
            assert any("newest" in line for line in text)


def test_native_dashboard_layout_is_unchanged_on_normal_terminal():
    from ffmpeg_builder.ui.dashboard import BuildDashboard

    console = Console(file=io.StringIO(), width=120, height=30, color_system=None)
    dashboard = BuildDashboard(console, "8.1", 4, 2)
    dashboard.set_components([SimpleNamespace(name="zlib", version="1.3")], reveal_all=True)
    dashboard.update_status("zlib", ComponentStatus.BUILDING, detail="make -j4")
    dashboard.log("zlib building")
    console.print(dashboard)
    lines = console.file.getvalue().splitlines()
    assert len(lines) == 30
    assert lines[0].startswith("╭") and "FFmpeg Builder 8.1 - Building" in lines[1]
    assert lines[3].startswith("1/1 zlib") and "██████-" in lines[3] and "65% [build]" in lines[3]
    assert "Messages" in lines[20] and "zlib building" in lines[21]
    assert lines[29].startswith("╰")


@pytest.mark.parametrize("width,height", [(120, 30), (80, 24), (60, 14), (40, 8)])
@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_real_cross_live_lifecycle_fits_viewport_and_returns_to_menu(
    tui, monkeypatch, width, height, outcome
):
    import sys
    import time

    from rich.live import Live

    from ffmpeg_builder.components import ComponentSelection
    from ffmpeg_builder.ui.dashboard import BuildDashboard

    app, _ = tui
    config = _cross_config()
    app.config_manager.save(config)
    app.windows_probe = lambda *args, **kwargs: _target()
    app.console = Console(
        file=io.StringIO(), width=width, height=height, force_terminal=True, color_system=None
    )
    app.final_screen.console = app.console
    selected = [c for c in app.registry.get_all() if c.name in ("zlib", "ffmpeg")]
    monkeypatch.setattr(
        app,
        "_get_component_selection",
        lambda config: ComponentSelection(target_components_to_build=selected),
    )
    lives = []
    monkeypatch.setattr(
        app_module,
        "Live",
        type(
            "ObservedLive",
            (Live,),
            {"start": lambda s, *a, **k: lives.append(s) or Live.start(s, *a, **k)},
        ),
    )
    dashboards = []
    monkeypatch.setattr(
        app_module,
        "BuildDashboard",
        lambda *a, **k: dashboards.append(BuildDashboard(*a, **k)) or dashboards[-1],
    )
    events = []
    monkeypatch.setattr(
        app.system_screen,
        "show",
        lambda *a, **k: events.append("menu") or ("build" if len(events) == 1 else "exit"),
    )
    stdout, stderr = sys.stdout, sys.stderr

    def runner(app, config, **hooks):
        hooks["on_plan"](selected)
        assert lives[0].is_started and not lives[0]._nested
        for component in selected:
            app.state_manager.mark_component_status(
                component.name,
                ComponentStatus.COMPLETED,
                component.version,
                detail="Windows target output contract accepted",
            )
        hooks["on_message"](_VERSION_DUMP)
        print("Wave-9 PE imports: " + "'api-ms-win-crt-heap-l1-1-0.dll', " * 6, flush=True)
        print("stray warning on stderr", file=sys.stderr, flush=True)
        hooks["on_message"]("newest: G7 runtime acceptance running")
        time.sleep(0.4)  # let the real refresh thread draw several frames
        events.append("runner returned")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "failure":
            hooks["on_message"]("FAIL: Windows production workflow: acceptance failed")
            return 1
        (app.workspace / "bin").mkdir(parents=True)
        for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
            (app.workspace / "bin" / name).touch()
        return 0

    monkeypatch.setattr(cross_build, "run_cross_build", runner)
    monkeypatch.setattr(
        screens.Confirm, "ask", lambda *a, **k: pytest.fail("native install prompt")
    )

    def dismiss(prompt):
        assert not lives[0].is_started and sys.stdout is stdout
        events.append("dismiss")
        return ""

    monkeypatch.setattr(app.console, "input", dismiss)

    code = app.run()
    output = app.console.file.getvalue()
    assert not lives[0].is_started and not lives[0]._refresh_thread
    assert (sys.stdout, sys.stderr) == (stdout, stderr)
    assert app.state_manager.status_listener is None
    assert output.count("\x1b[?1049h") == output.count("\x1b[?1049l") == 1
    frames = _frames(output, height)
    assert len(frames) > 2
    for frame in frames:
        assert len(frame) <= height, "frame scrolled the terminal"
        assert frame[-1].startswith("╰"), "messages panel bottom border not visible"
    final_frame = "\n".join(frames[-1])
    assert "acceptance running" in final_frame
    messages = "\n".join(dashboards[0].messages)
    assert "Wave-9 PE imports" in messages and "stray warning on stderr" in messages
    after_live = output[output.rindex("\x1b[?1049l") :]
    assert "Wave-9" not in after_live and "stray warning" not in after_live
    if outcome == "interrupt":
        assert code == 130 and events == ["menu", "runner returned"]
        assert "Windows Build" not in after_live
        return
    assert code == 0
    assert events == ["menu", "runner returned", "dismiss", "menu"]
    if outcome == "success":
        assert "Windows Build Completed Successfully!" in after_live
        for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
            assert name in after_live
    else:
        assert "Windows Build Failed" in after_live and "acceptance failed" in after_live
    assert "Install binaries to system?" not in output
