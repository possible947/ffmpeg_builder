"""Configuration management for FFmpeg builder."""

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


@dataclass
class MacOSConfig:
    """macOS-specific configuration."""

    clang: str = "macports-clang-17"


@dataclass
class LinuxConfig:
    """Linux-specific configuration."""

    c_standard: str = "c11"
    cxx_standard: str = "c++17"


@dataclass
class WindowsConfig:
    """Windows-specific configuration."""

    backend: str = "msys2-ucrt64"
    command_mode: str = "posix"
    msys2_root: str = "C:\\msys64"
    prefer_system_packages: bool = True


@dataclass
class BuildConfig:
    """Build configuration."""

    ffmpeg_version: str = "8.1"
    gpl_enabled: bool = False
    make_release: bool = False
    native_build: bool = False
    full_static: bool = False
    enable_libvmaf: bool = True
    enable_libvmaf_cuda: bool = True
    enable_libplacebo_vulkan: bool = False
    disable_lv2: bool = False
    openmp: bool = True
    num_jobs: str = "auto"
    make_timeout_seconds: int = 0
    install_timeout_seconds: int = 0
    async_downloads: bool = True
    download_workers: int = 4
    source_archives_dir: str = "third_party/sources"
    allow_network_downloads: bool = False
    # H1 integrity policy: refuse network downloads that have no sha256.
    require_sha256_for_network: bool = True
    macos: MacOSConfig = field(default_factory=MacOSConfig)
    linux: LinuxConfig = field(default_factory=LinuxConfig)
    windows: WindowsConfig = field(default_factory=WindowsConfig)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "BuildConfig":
        """Create from dictionary."""
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"Expected config mapping, got {type(data).__name__}")

        macos_data = data.get("macos") or {}
        linux_data = data.get("linux") or {}
        windows_data = data.get("windows") or {}
        if not isinstance(macos_data, dict):
            raise ValueError("Expected 'macos' to be a mapping")
        if not isinstance(linux_data, dict):
            raise ValueError("Expected 'linux' to be a mapping")
        if not isinstance(windows_data, dict):
            raise ValueError("Expected 'windows' to be a mapping")

        # Filter out nested config dicts before passing to constructor
        nested_keys = {"macos", "linux", "windows"}
        build_fields = {item.name for item in fields(cls)}
        config_data = {k: v for k, v in data.items() if k in build_fields and k not in nested_keys}

        macos_fields = {item.name for item in fields(MacOSConfig)}
        linux_fields = {item.name for item in fields(LinuxConfig)}
        windows_fields = {item.name for item in fields(WindowsConfig)}

        config = cls(**config_data)
        config.macos = MacOSConfig(**{k: v for k, v in macos_data.items() if k in macos_fields})
        config.linux = LinuxConfig(**{k: v for k, v in linux_data.items() if k in linux_fields})
        config.windows = WindowsConfig(
            **{k: v for k, v in windows_data.items() if k in windows_fields}
        )

        return config


class ConfigManager:
    """Manages build configuration."""

    def __init__(self, config_path: Optional[Path] = None):
        """Initialize config manager.

        Args:
            config_path: Path to configuration file. If None, uses default.
        """
        self.config_path = config_path or Path("build_config.yaml")
        self.config: Optional[BuildConfig] = None

    def load(self) -> BuildConfig:
        """Load configuration from file.

        Returns:
            BuildConfig instance.
        """
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            self.config = BuildConfig.from_dict(data)
        else:
            self.config = BuildConfig()

        return self.config

    def save(self, config: Optional[BuildConfig] = None) -> None:
        """Save configuration to file.

        Args:
            config: Configuration to save. If None, saves current config.
        """
        if config is not None:
            self.config = config

        if self.config is None:
            raise ValueError("No configuration to save")

        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(self.config.to_dict(), f, default_flow_style=False, sort_keys=False)

    def get(self) -> BuildConfig:
        """Get current configuration.

        Returns:
            Current BuildConfig instance.
        """
        if self.config is None:
            return self.load()
        return self.config
