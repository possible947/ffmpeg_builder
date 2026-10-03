"""Tests for FFmpegBuilderApp version-specific workspace logic."""

from pathlib import Path

from ffmpeg_builder.app import FFmpegBuilderApp
from ffmpeg_builder.config import BuildConfig, ConfigManager


def test_app_workspace_defaults_to_version_specific(tmp_path: Path, monkeypatch):
    """FFmpegBuilderApp defaults workspace to workspace_81 when ffmpeg_version is 8.1."""
    config_file = tmp_path / "build_config.yaml"
    ConfigManager(config_file).save(BuildConfig(ffmpeg_version="8.1"))

    app = FFmpegBuilderApp()
    app.config_manager = ConfigManager(config_file)
    app._sync_workspace(app.config_manager.load())

    assert app.workspace.name == "workspace_81"
    assert app.packages == app.workspace / "packages"
    assert app.state_manager.state_path == app.workspace / "build_state.json"


def test_app_workspace_switches_on_version_change(tmp_path: Path):
    """FFmpegBuilderApp dynamically updates workspace when switching FFmpeg version."""
    config_file = tmp_path / "build_config.yaml"
    mgr = ConfigManager(config_file)
    mgr.save(BuildConfig(ffmpeg_version="8.1"))

    app = FFmpegBuilderApp()
    app.config_manager = mgr

    cfg81 = mgr.load()
    app._sync_workspace(cfg81)
    assert app.workspace.name == "workspace_81"
    assert app.state_manager.state_path == app.workspace / "build_state.json"

    cfg90 = BuildConfig(ffmpeg_version="9.0")
    mgr.save(cfg90)
    app._sync_workspace(cfg90)
    assert app.workspace.name == "workspace_90"
    assert app.state_manager.state_path == app.workspace / "build_state.json"


def test_app_custom_workspace_preserved_on_sync(tmp_path: Path):
    """Explicitly passed custom workspace is preserved across config syncs."""
    custom_ws = tmp_path / "custom_ws"
    app = FFmpegBuilderApp(workspace=custom_ws)

    assert app.workspace == custom_ws
    assert app.state_manager.state_path == custom_ws / "build_state.json"

    app._sync_workspace(BuildConfig(ffmpeg_version="9.0"))
    assert app.workspace == custom_ws
    assert app.state_manager.state_path == custom_ws / "build_state.json"


def test_app_cleanup_removes_current_and_legacy_workspaces(tmp_path: Path):
    """_cleanup() removes the active versioned workspace and legacy workspace if present."""
    ws_dir = tmp_path / "workspace_81"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "test.txt").write_text("data", encoding="utf-8")

    legacy_dir = tmp_path / "workspace"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "old.txt").write_text("old", encoding="utf-8")

    app = FFmpegBuilderApp(workspace=ws_dir)

    # Monkeypatch PROJECT_ROOT in app module for legacy cleanup check
    import ffmpeg_builder.app as app_mod

    orig_root = app_mod.PROJECT_ROOT
    try:
        app_mod.PROJECT_ROOT = tmp_path
        app._cleanup()
    finally:
        app_mod.PROJECT_ROOT = orig_root

    assert not ws_dir.exists()
    assert not legacy_dir.exists()
