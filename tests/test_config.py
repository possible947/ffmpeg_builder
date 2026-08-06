"""Tests for config parsing and round-trip serialization."""

from pathlib import Path

import pytest

from ffmpeg_builder.config import (
    BuildConfig,
    ConfigManager,
    LinuxConfig,
    MacOSConfig,
    WindowsConfig,
)


class TestBuildConfigDefaults:
    """Test default configuration values."""

    def test_default_config(self):
        cfg = BuildConfig()
        assert cfg.ffmpeg_version == "8.1"
        assert cfg.gpl_enabled is False
        assert cfg.full_static is False
        assert cfg.enable_libvmaf is True
        assert cfg.enable_libvmaf_cuda is True
        assert cfg.enable_libplacebo is False
        assert cfg.disable_lv2 is False
        assert cfg.openmp is True
        assert cfg.num_jobs == "auto"
        assert cfg.async_downloads is True
        assert cfg.download_workers == 4
        assert cfg.source_archives_dir == "third_party/sources"
        assert cfg.allow_network_downloads is False

    def test_nested_config_defaults(self):
        cfg = BuildConfig()
        assert isinstance(cfg.linux, LinuxConfig)
        assert cfg.linux.c_standard == "c11"
        assert cfg.linux.cxx_standard == "c++17"
        assert isinstance(cfg.macos, MacOSConfig)
        assert cfg.macos.clang == "macports-clang-17"
        assert isinstance(cfg.windows, WindowsConfig)
        assert cfg.windows.backend == "msys2-ucrt64"
        assert cfg.windows.command_mode == "posix"
        assert cfg.windows.prefer_system_packages is True


class TestBuildConfigRoundTrip:
    """Test to_dict / from_dict round-trip."""

    def test_roundtrip_defaults(self, sample_config_dict):
        cfg = BuildConfig.from_dict(sample_config_dict)
        out = cfg.to_dict()
        # Top-level keys match input
        for k in ("ffmpeg_version", "gpl_enabled", "full_static"):
            assert out[k] == sample_config_dict[k], f"Mismatch for {k}"

    def test_roundtrip_gpl_true(self, sample_config_dict):
        sample_config_dict["gpl_enabled"] = True
        cfg = BuildConfig.from_dict(sample_config_dict)
        assert cfg.gpl_enabled is True
        out = cfg.to_dict()
        assert out["gpl_enabled"] is True

    def test_roundtrip_nested_configs(self, sample_config_dict):
        sample_config_dict["macos"] = {"clang": "apple-clang"}
        sample_config_dict["linux"] = {"c_standard": "c17", "cxx_standard": "c++20"}
        sample_config_dict["windows"] = {
            "backend": "msys2-mingw64",
            "command_mode": "cmd",
            "msys2_root": "D:\\msys64",
            "prefer_system_packages": False,
        }
        cfg = BuildConfig.from_dict(sample_config_dict)
        assert cfg.macos.clang == "apple-clang"
        assert cfg.linux.c_standard == "c17"
        assert cfg.linux.cxx_standard == "c++20"
        assert cfg.windows.backend == "msys2-mingw64"
        assert cfg.windows.command_mode == "cmd"
        assert cfg.windows.msys2_root == "D:\\msys64"
        assert cfg.windows.prefer_system_packages is False

        out = cfg.to_dict()
        assert out["macos"]["clang"] == "apple-clang"
        assert out["linux"]["c_standard"] == "c17"
        assert out["windows"]["prefer_system_packages"] is False

    def test_partial_input(self):
        """Only ffmpeg_version and gpl_enabled supplied — rest should default."""
        data = {"ffmpeg_version": "8.1", "gpl_enabled": True}
        cfg = BuildConfig.from_dict(data)
        assert cfg.ffmpeg_version == "8.1"
        assert cfg.gpl_enabled is True
        assert cfg.full_static is False  # default


class TestConfigManager:
    """Test file-based config management."""

    def test_load_from_file(self, sample_config_file):
        mgr = ConfigManager(sample_config_file)
        cfg = mgr.load()
        assert isinstance(cfg, BuildConfig)
        assert cfg.ffmpeg_version == "8.1"
        assert cfg.gpl_enabled is True

    def test_get_loads_once(self, sample_config_file):
        mgr = ConfigManager(sample_config_file)
        cfg = mgr.get()
        assert cfg is mgr.config  # cached

    def test_save_and_reload(self, tmp_path):
        path = tmp_path / "config.yaml"
        mgr = ConfigManager(path)
        cfg = BuildConfig(gpl_enabled=True, full_static=True)
        mgr.save(cfg)

        mgr2 = ConfigManager(path)
        loaded = mgr2.load()
        assert loaded.gpl_enabled is True
        assert loaded.full_static is True

    def test_save_without_config_raises(self, tmp_path):
        path = tmp_path / "config.yaml"
        mgr = ConfigManager(path)
        with pytest.raises(ValueError, match="No configuration to save"):
            mgr.save()

    def test_missing_file_returns_defaults(self, tmp_path):
        mgr = ConfigManager(tmp_path / "nonexistent.yaml")
        cfg = mgr.load()
        assert cfg.ffmpeg_version == "8.1"
