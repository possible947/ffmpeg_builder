"""Shared pytest fixtures."""

import json
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from ffmpeg_builder.config import BuildConfig, ConfigManager


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Provide a temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def sample_config_dict() -> Dict[str, Any]:
    """Minimal config dict for testing."""
    return {
        "ffmpeg_version": "8.1",
        "gpl_enabled": True,
        "make_release": False,
        "native_build": False,
        "full_static": False,
        "enable_libvmaf": True,
        "enable_libvmaf_cuda": True,
        "enable_libplacebo_vulkan": False,
        "disable_lv2": False,
        "openmp": True,
        "num_jobs": "auto",
        "async_downloads": True,
        "download_workers": 4,
        "source_archives_dir": "third_party/sources",
        "allow_network_downloads": False,
    }


@pytest.fixture
def sample_config_file(tmp_path: Path, sample_config_dict: dict) -> Path:
    """Write a YAML config to temp path."""
    import yaml

    cfg = tmp_path / "build_config.yaml"
    with open(cfg, "w", encoding="utf-8") as f:
        yaml.dump(sample_config_dict, f, default_flow_style=False)
    return cfg


@pytest.fixture
def sample_build_state_dict() -> Dict[str, Any]:
    """Sample serialized build state."""
    return {
        "build_id": "test-build-001",
        "started_at": "2026-08-06T00:00:00",
        "config": {"ffmpeg_version": "8.1", "gpl_enabled": True},
        "components": {
            "pkg-config": {
                "status": "completed",
                "version": "0.29.2",
                "built_at": "2026-08-06T01:00:00",
                "error_message": None,
                "log_file": None,
            },
            "x264": {
                "status": "failed",
                "version": "0480cb05",
                "built_at": None,
                "error_message": "configure failed",
                "log_file": "workspace/logs/x264.log",
            },
            "x265": {
                "status": "building",
                "version": "8be7dbf",
                "built_at": None,
                "error_message": None,
                "log_file": "workspace/logs/x265.log",
            },
        },
        "current_step": 3,
        "total_steps": 10,
    }


@pytest.fixture
def state_file(tmp_path: Path, sample_build_state_dict: dict) -> Path:
    """Write a JSON build-state file to temp path."""
    sp = tmp_path / "workspace" / "build_state.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(sample_build_state_dict, f)
    return sp


@pytest.fixture
def empty_state_file(tmp_path: Path) -> Path:
    """Empty build-state file (minimal valid state)."""
    sp = tmp_path / "workspace" / "build_state.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "build_id": "",
                "started_at": "",
                "config": {},
                "components": {},
                "current_step": 0,
                "total_steps": 0,
            },
            f,
        )
    return sp


@pytest.fixture
def mock_platform_info():
    """Mock PlatformInfo object for component filtering tests."""

    class _PlatformInfo:
        cuda_available = True
        vulkan_available = True
        amf_available = False
        opencl_available = True
        qsv_available = True
        is_msys2 = False
        is_ucrt64 = False
        build_backend = "linux-native"

    return _PlatformInfo()


@pytest.fixture
def mock_platform_info_windows():
    """Mock PlatformInfo for Windows MSYS2 UCRT64."""

    class _PlatformInfo:
        cuda_available = False
        vulkan_available = True
        amf_available = True
        opencl_available = True
        qsv_available = True
        is_msys2 = True
        is_ucrt64 = True
        build_backend = "windows-msys2-ucrt64"

    return _PlatformInfo()


@pytest.fixture
def mock_tools():
    """Mock tools dict with available system tools."""

    class Tool:
        available = True

    return {
        "python3": Tool(),
        "meson": Tool(),
        "ninja": Tool(),
        "cargo": Tool(),
        "rustc": Tool(),
        "cmake": Tool(),
        "pkg-config": Tool(),
        "yasm": Tool(),
        "nasm": Tool(),
    }


@pytest.fixture
def mock_tools_without_cargo():
    """Mock tools dict without cargo/rustc."""

    class Tool:
        available = True

    return {
        "python3": Tool(),
        "meson": Tool(),
        "ninja": Tool(),
        "cmake": Tool(),
        "pkg-config": Tool(),
        "yasm": Tool(),
        "nasm": Tool(),
    }
