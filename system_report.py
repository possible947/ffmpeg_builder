"""System report generation."""

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from dataclasses import dataclass, field

from .platform_detect import PlatformInfo, SystemInfo, ToolInfo


@dataclass
class SystemReport:
    """System report containing all detected information."""

    system_info: SystemInfo = field(default_factory=SystemInfo)
    platform_info: PlatformInfo = field(default_factory=PlatformInfo)
    tools: Dict[str, ToolInfo] = field(default_factory=dict)
    build_environment: Dict[str, str] = field(default_factory=dict)
    # Configured compiler name from build_config.yaml (e.g. "macports-clang-17").
    # Set by SystemReportGenerator when a BuildConfig is provided so the start
    # screen displays the compiler that will actually be used, not the highest
    # auto-detected one.
    configured_clang: Optional[str] = field(default=None)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "system_info": self.system_info.to_dict(),
            "platform_info": self.platform_info.to_dict(),
            "tools": {name: tool.to_dict() for name, tool in self.tools.items()},
            "build_environment": self.build_environment,
        }

    def get_available_tools_summary(self) -> Dict[str, bool]:
        """Get summary of available tools.

        Returns:
            Dictionary mapping tool name to availability.
        """
        return {name: tool.available for name, tool in self.tools.items()}

    def get_missing_required_tools(self) -> list:
        """Get list of missing required tools.

        Returns:
            List of missing tool names.
        """
        required = ["make", "pkg-config", "curl"]

        # Platform-specific requirements
        if self.platform_info.is_macos:
            required.append("clang++")
        else:
            required.append("g++")

        return [
            tool for tool in required if not self.tools.get(tool, ToolInfo(name=tool)).available
        ]

    def get_optional_tools_status(self) -> Dict[str, bool]:
        """Get status of optional tools.

        Returns:
            Dictionary mapping optional tool name to availability.
        """
        optional = {
            "nasm": "NASM assembler (for x264, x265, etc.)",
            "yasm": "YASM assembler (alternative to NASM)",
            "cmake": "CMake (for various components)",
            "python3": "Python 3 (for dav1d, libvmaf, etc.)",
            "meson": "Meson build system (for dav1d, libvmaf)",
            "ninja": "Ninja build system (for dav1d, libvmaf)",
            "cargo": "Cargo (for rav1e)",
            "rustc": "Rust compiler (for rav1e)",
        }

        return {
            name: self.tools.get(name, ToolInfo(name=name)).available for name in optional.keys()
        }

    def get_hardware_acceleration_status(self) -> Dict[str, bool]:
        """Get hardware acceleration availability.

        Returns:
            Dictionary mapping acceleration type to availability.
        """
        status = {}

        if self.platform_info.is_macos:
            status["VideoToolbox"] = True  # Always available on macOS
            status["Vulkan"] = self.platform_info.vulkan_available
            status["OpenCL"] = self.platform_info.opencl_available
        elif self.platform_info.is_linux or self.platform_info.is_windows:
            status["CUDA"] = self.platform_info.cuda_available
            status["libvmaf_cuda"] = self.platform_info.libvmaf_cuda_supported
            if self.platform_info.is_linux:
                status["VAAPI"] = self.platform_info.vaapi_available
            status["Intel QSV"] = self.platform_info.qsv_available
            status["AMF"] = self.platform_info.amf_available
            status["Vulkan"] = self.platform_info.vulkan_available
            status["OpenCL"] = self.platform_info.opencl_available

        return status

    def get_opencl_diagnostics(self) -> Dict[str, Any]:
        """Get OpenCL detection diagnostics."""
        return {
            "effective_available": self.platform_info.opencl_effective_available,
            "effective_reason": self.platform_info.opencl_effective_reason,
            "runtime_available": self.platform_info.opencl_runtime_available,
            "runtime_reason": self.platform_info.opencl_runtime_reason,
            "dev_available": self.platform_info.opencl_dev_available,
            "dev_reason": self.platform_info.opencl_dev_reason,
            "pkg_config_name": self.platform_info.opencl_pkg_config_name,
            "header_paths": self.platform_info.opencl_detected_header_paths,
            "loader_paths": self.platform_info.opencl_detected_loader_paths,
            "icd_files": self.platform_info.opencl_detected_icd_files,
        }

    def get_sdk_status(self) -> Dict[str, Dict[str, Any]]:
        """Get detected SDK roots for diagnostics."""
        return {
            "ROCm": {
                "available": self.platform_info.rocm_available,
                "path": self.platform_info.rocm_path,
            },
            "CUDA": {
                "available": self.platform_info.cuda_available,
                "path": self.platform_info.cuda_path,
            },
            "Vulkan SDK": {
                "available": self.platform_info.vulkan_sdk_available,
                "path": self.platform_info.vulkan_sdk_path,
            },
        }

    def get_compiler_info(self) -> Dict[str, str]:
        """Get compiler information.

        On macOS, ``configured_clang`` (populated from build_config.yaml by
        SystemReportGenerator) is resolved first so the start screen shows the
        compiler that will actually be used for the build, not just the highest
        auto-detected MacPorts version.

        Returns:
            Dictionary with compiler name and version.
        """
        return self._resolve_compiler_info(self.configured_clang)

    def _resolve_compiler_info(self, configured_clang: Optional[str] = None) -> Dict[str, str]:
        """Resolve and return compiler display info.

        Args:
            configured_clang: Value of ``macos.clang`` from BuildConfig, e.g.
                ``"macports-clang-17"``.  If None, falls back to auto-detect.

        Returns:
            Dictionary with compiler name and version.
        """
        if self.platform_info.is_macos:
            # Try to resolve the configured compiler first (mirrors builder.py logic)
            if configured_clang:
                resolved = shutil.which(configured_clang)
                if not resolved and configured_clang.startswith("macports-clang-"):
                    ver = configured_clang.removeprefix("macports-clang-")
                    resolved = shutil.which(f"clang-mp-{ver}")
                if resolved:
                    return {
                        "compiler": "Macports Clang",
                        "version": configured_clang.removeprefix("macports-clang-"),
                        "path": resolved,
                    }

            if self.platform_info.macports_clang and self.platform_info.macports_clang.available:
                return {
                    "compiler": "Macports Clang",
                    "version": self.platform_info.macports_clang.version,
                    "path": self.platform_info.macports_clang.path,
                }
            elif self.tools.get("clang++", ToolInfo(name="clang++")).available:
                clang = self.tools["clang++"]
                return {"compiler": "System Clang", "version": clang.version, "path": clang.path}
        elif self.platform_info.is_linux or self.platform_info.is_windows:
            if self.tools.get("g++", ToolInfo(name="g++")).available:
                gcc = self.tools["g++"]
                return {"compiler": "GCC", "version": gcc.version, "path": gcc.path}

        return {"compiler": "Unknown", "version": "Unknown", "path": "Unknown"}


class SystemReportGenerator:
    """Generates system reports."""

    def __init__(
        self,
        system_info: SystemInfo,
        platform_info: PlatformInfo,
        tools: Dict[str, ToolInfo],
        config: Optional[Any] = None,
    ):
        """Initialize report generator.

        Args:
            system_info: System information.
            platform_info: Platform information.
            tools: Dictionary of detected tools.
            config: BuildConfig instance (optional).  When provided, the
                ``macos.clang`` setting is used to display the compiler that
                will actually be used for the build instead of the highest
                auto-detected MacPorts clang version.
        """
        self.system_info = system_info
        self.platform_info = platform_info
        self.tools = tools
        self.config = config

    def generate(self) -> SystemReport:
        """Generate system report.

        Returns:
            SystemReport instance.
        """
        import os

        configured_clang: Optional[str] = None
        if self.config is not None and self.platform_info.is_macos:
            macos_cfg = getattr(self.config, "macos", None)
            if macos_cfg is not None:
                configured_clang = getattr(macos_cfg, "clang", None) or None

        report = SystemReport(
            system_info=self.system_info,
            platform_info=self.platform_info,
            tools=self.tools,
            configured_clang=configured_clang,
        )

        # Add build environment info
        report.build_environment = {
            "PATH": os.environ.get("PATH", ""),
            "PKG_CONFIG_PATH": os.environ.get("PKG_CONFIG_PATH", ""),
            "CFLAGS": os.environ.get("CFLAGS", ""),
            "LDFLAGS": os.environ.get("LDFLAGS", ""),
            "CC": os.environ.get("CC", ""),
            "CXX": os.environ.get("CXX", ""),
        }

        return report
