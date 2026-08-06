"""Component registry for FFmpeg builder."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

WINDOWS_UCRT64_HW_ACCEL_COMPONENTS = {
    "nv-codec",
    "vulkan-headers",
    "glslang",
    "libplacebo",
    "opencl-headers",
    "opencl-icd-loader",
    "onevpl",
}


class BuildSystem(str, Enum):
    """Build system type."""

    AUTOTOOLS = "autotools"
    CMAKE = "cmake"
    MESON = "meson"
    CUSTOM = "custom"
    HEADERS_ONLY = "headers_only"
    MAKE_ONLY = "make_only"
    CARGO = "cargo"


class ComponentCategory(str, Enum):
    """Component category."""

    BUILD_TOOL = "build_tool"
    CRYPTO = "crypto"
    VIDEO_CODEC = "video_codec"
    AUDIO_CODEC = "audio_codec"
    IMAGE_CODEC = "image_codec"
    OTHER_LIB = "other_lib"
    HW_ACCEL = "hw_accel"
    TARGET = "target"


@dataclass
class PlatformOverride:
    """Platform-specific overrides."""

    extra_env: Dict[str, str] = field(default_factory=dict)
    extra_cflags: str = ""
    extra_cxxflags: str = ""
    extra_ldflags: str = ""
    patches: List[str] = field(default_factory=list)
    configure_args_override: Optional[List[str]] = None


@dataclass
class Component:
    """Component definition."""

    name: str
    version: str
    url: str
    category: ComponentCategory
    build_system: BuildSystem
    configure_args: List[str] = field(default_factory=list)
    build_args: List[str] = field(default_factory=list)
    install_args: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    requires_tools: List[str] = field(default_factory=list)
    gpl_only: bool = False
    non_gpl_only: bool = False
    linux_only: bool = False
    macos_only: bool = False
    windows_ucrt64_supported: bool = False
    system_component: bool = False
    system_tool_name: Optional[str] = None
    archive_strip_components: int = 1
    archive_dirname: Optional[str] = None
    archive_filename: Optional[str] = None
    workdir: Optional[str] = None
    platform_overrides: Dict[str, PlatformOverride] = field(default_factory=dict)
    extra_env: Dict[str, str] = field(default_factory=dict)
    post_install: Optional[str] = ""
    custom_build_fn: Optional[str] = None
    ffmpeg_configure_flag: Optional[str] = None
    skip_condition: Optional[str] = None
    extra_libs: str = ""
    sed_patches: Dict[str, str] = field(default_factory=dict)

    def get_url(self) -> str:
        """Get download URL with version substituted."""
        return self.url.replace("{version}", self.version)

    def get_archive_filename(self) -> str:
        """Get archive filename."""
        if self.archive_filename:
            return self.archive_filename.replace("{version}", self.version)
        url = self.get_url()
        return url.split("/")[-1]

    def get_target_dir(self) -> str:
        """Get target directory name."""
        if self.archive_dirname:
            return self.archive_dirname.replace("{version}", self.version)
        fname = self.get_archive_filename()
        name = fname
        for ext in (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz"):
            if name.endswith(ext):
                name = name[: -len(ext)]
                break
        return name

    def is_available(
        self,
        gpl_enabled: bool,
        platform: str,
        tools: Dict,
        platform_info: Optional[Any] = None,
    ) -> bool:
        """Check if component should be built.

        Args:
            gpl_enabled: Whether GPL is enabled.
            platform: Platform name ("linux", "darwin", "windows").
            tools: Available tools dict.

        Returns:
            True if component should be built.
        """
        if self.gpl_only and not gpl_enabled:
            return False
        if self.non_gpl_only and gpl_enabled:
            return False
        if self.linux_only and platform != "linux":
            is_windows_ucrt64 = (
                platform == "windows"
                and self.windows_ucrt64_supported
                and platform_info is not None
                and getattr(platform_info, "is_msys2", False)
                and getattr(platform_info, "is_ucrt64", False)
            )
            if not is_windows_ucrt64:
                return False
        if self.macos_only and platform != "darwin":
            return False
        return True


class ComponentRegistry:
    """Registry of all build components, loaded from components.yaml."""

    # Path to the YAML registry file (next to this module)
    _YAML_PATH = Path(__file__).parent / "components.yaml"

    def __init__(self, yaml_path: Optional[Path] = None):
        """Initialize component registry by loading from YAML.

        Args:
            yaml_path: Optional override path to components YAML file.
                       Defaults to ``components.yaml`` next to this module.
        """
        self._components: List[Component] = []
        self._load_components(yaml_path or self._YAML_PATH)

    # ------------------------------------------------------------------
    # YAML loader
    # ------------------------------------------------------------------

    @staticmethod
    def _platform_overrides_from_dict(
        po_data: Dict[str, Any],
    ) -> Dict[str, PlatformOverride]:
        """Convert platform-overrides dict from YAML into PlatformOverride objects."""
        result: Dict[str, PlatformOverride] = {}
        for platform, entry in (po_data or {}).items():
            cfg_ov = entry.get("configure_args_override")
            if cfg_ov is not None and not isinstance(cfg_ov, list):
                cfg_ov = None
            result[platform] = PlatformOverride(
                extra_env=entry.get("extra_env", {}),
                extra_cflags=entry.get("extra_cflags", ""),
                extra_cxxflags=entry.get("extra_cxxflags", ""),
                extra_ldflags=entry.get("extra_ldflags", ""),
                patches=entry.get("patches") or [],
                configure_args_override=cfg_ov,
            )
        return result

    @classmethod
    def _component_from_dict(cls, data: Dict[str, Any]) -> Component:
        """Build a Component dataclass from one YAML entry dict."""
        return Component(
            name=data["name"],
            version=str(data["version"]),
            url=data["url"],
            category=ComponentCategory(data["category"]),
            build_system=BuildSystem(data["build_system"]),
            configure_args=list(data.get("configure_args") or []),
            build_args=list(data.get("build_args") or []),
            install_args=list(data.get("install_args") or []),
            depends_on=list(data.get("depends_on") or []),
            requires_tools=list(data.get("requires_tools") or []),
            gpl_only=bool(data.get("gpl_only", False)),
            non_gpl_only=bool(data.get("non_gpl_only", False)),
            linux_only=bool(data.get("linux_only", False)),
            macos_only=bool(data.get("macos_only", False)),
            windows_ucrt64_supported=bool(data.get("windows_ucrt64_supported", False)),
            system_component=bool(data.get("system_component", False)),
            system_tool_name=data.get("system_tool_name"),
            archive_strip_components=int(data.get("archive_strip_components", 1)),
            archive_filename=data.get("archive_filename"),
            archive_dirname=data.get("archive_dirname"),
            workdir=data.get("workdir"),
            platform_overrides=cls._platform_overrides_from_dict(data.get("platform_overrides")),
            extra_env=dict(data.get("extra_env") or {}),
            post_install=data.get("post_install") or None,
            custom_build_fn=data.get("custom_build_fn"),
            ffmpeg_configure_flag=data.get("ffmpeg_configure_flag"),
            skip_condition=data.get("skip_condition"),
            extra_libs=data.get("extra_libs") or "",
            sed_patches=dict(data.get("sed_patches") or {}),
        )

    def _load_components(self, yaml_path: Path) -> None:
        """Load component definitions from a YAML file."""
        if not yaml_path.exists():
            raise FileNotFoundError(
                f"Component registry file not found: {yaml_path}\n"
                "Run `python _gen_components_yaml.py` to (re-)generate it."
            )
        with open(yaml_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if not isinstance(data, list):
            raise ValueError(
                f"Expected top-level YAML list in {yaml_path}, got {type(data).__name__}"
            )

        for idx, entry in enumerate(data):
            try:
                comp = self._component_from_dict(entry)
                self._components.append(comp)
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(
                    f"Invalid component definition at index {idx} "
                    f"(name={entry.get('name', '<missing>')!r}): {exc}"
                ) from exc

    # ------------------------------------------------------------------
    # Query API  (unchanged public interface)
    # ------------------------------------------------------------------

    def get_all(self) -> List[Component]:
        """Get all components in build order."""
        return list(self._components)

    def get_by_name(self, name: str) -> Optional[Component]:
        """Get component by name."""
        for comp in self._components:
            if comp.name == name:
                return comp
        return None

    def get_by_category(self, category: ComponentCategory) -> List[Component]:
        """Get components by category."""
        return [c for c in self._components if c.category == category]

    def get_buildable(
        self,
        gpl_enabled: bool,
        platform: str,
        tools: Dict,
        disable_lv2: bool = False,
        enable_libvmaf: bool = True,
        platform_info: Optional[Any] = None,
        enable_libplacebo: bool = False,
        full_static: bool = False,
    ) -> List[Component]:
        """Get list of components that should be built.

        Args:
            gpl_enabled: Whether GPL is enabled.
            platform: Platform name ("linux", "darwin", "windows").
            tools: Available tools dict.
            disable_lv2: Whether LV2 is disabled.
            enable_libvmaf: Whether libvmaf is enabled.
            platform_info: PlatformInfo for HW acceleration filtering.

        Returns:
            List of components to build.
        """
        result = []

        for comp in self._components:
            if not comp.is_available(gpl_enabled, platform, tools, platform_info):
                continue

            if not self._is_component_allowed_for_platform_policy(comp, platform, platform_info):
                continue

            if comp.skip_condition == "disable_lv2" and disable_lv2:
                continue

            if comp.name == "libvmaf" and not enable_libvmaf:
                continue

            if comp.requires_tools:
                has_tools = all(
                    tools.get(tool, type("", (), {"available": False})).available
                    for tool in comp.requires_tools
                )
                if not has_tools and comp.name not in ("rav1e",):
                    continue

            # Filter HW acceleration components by availability
            if platform_info is not None:
                if comp.name == "nv-codec" and not platform_info.cuda_available:
                    continue
                if (
                    comp.name in ("vulkan-headers", "glslang")
                    and not platform_info.vulkan_available
                ):
                    continue
                if comp.name == "amf" and not platform_info.amf_available:
                    continue
                if comp.name in ("opencl-headers", "opencl-icd-loader"):
                    if not platform_info.opencl_available:
                        continue
                if comp.name == "onevpl" and not platform_info.qsv_available:
                    continue

            # libplacebo: opt-in, requires Vulkan, disabled on full_static Linux
            if comp.name == "libplacebo":
                if not enable_libplacebo:
                    continue
                vulkan_ok = platform_info is not None and platform_info.vulkan_available
                if not vulkan_ok:
                    continue
                if full_static and platform == "linux":
                    continue

            result.append(comp)

        return result

    def _is_component_allowed_for_platform_policy(
        self,
        component: Component,
        platform: str,
        platform_info: Optional[Any],
    ) -> bool:
        """Apply explicit platform/backend policy constraints."""
        if platform != "windows":
            return True

        if component.category != ComponentCategory.HW_ACCEL:
            return True

        if platform_info is None:
            return False

        backend = getattr(platform_info, "build_backend", "")
        if backend != "windows-msys2-ucrt64":
            return False

        if component.name not in WINDOWS_UCRT64_HW_ACCEL_COMPONENTS:
            return False

        return True

    def get_system_components(self) -> List[Component]:
        """Get components that can be provided by the system."""
        return [c for c in self._components if c.system_component]

    def get_source_components(self) -> List[Component]:
        """Get components that must be built from source."""
        return [c for c in self._components if not c.system_component]

    def get_ffmpeg_configure_flags(
        self,
        built_components: List[str],
        gpl_enabled: bool,
        platform: str,
    ) -> List[str]:
        """Get FFmpeg configure flags based on built components.

        Args:
            built_components: List of successfully built component names.
            gpl_enabled: Whether GPL is enabled.
            platform: Platform name.

        Returns:
            List of configure flags.
        """
        flags = []

        for comp in self._components:
            if comp.name in built_components and comp.ffmpeg_configure_flag:
                flags.extend(comp.ffmpeg_configure_flag.split())

        if platform == "darwin":
            flags.append("--enable-videotoolbox")
            flags.append("--enable-opencl")

        return flags
