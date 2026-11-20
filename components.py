"""Component registry for FFmpeg builder."""

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional

import yaml

from .cross_build_contracts import (
    RuntimeDependency,
    TargetLinkMetadata,
    compose_target_link_metadata,
)
from .config import (
    SUPPORTED_TARGET_BACKENDS,
    TARGET_BACKEND_CROSS_MINGW,
    TARGET_BACKEND_NATIVE,
)

# Per-component toolchain gate used by get_buildable(): returns True when the
# component's resolved toolchain is satisfied (system, or an installed store
# profile), False when the component must be excluded. ``rav1e`` is exempt by
# policy (missing toolchain must raise a build error, never skip silently).
ToolchainChecker = Callable[["Component"], bool]

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


class BuildRole(str, Enum):
    """Execution domain used by the cross backend."""

    HOST_TOOL = "host"
    TARGET_COMPONENT = "target"
    MIXED = "mixed"
    HOST = "host"
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


@dataclass(frozen=True)
class ComponentVersion:
    """Version-specific source metadata for a component."""

    sha256: Optional[str] = None
    url: Optional[str] = None
    archive_filename: Optional[str] = None


@dataclass(frozen=True)
class TargetHeaderTree:
    """Source header tree materialised into a target prefix."""

    source: str
    destination: str


@dataclass(frozen=True)
class TargetImportInterface:
    """Recipe for a target-only import library backed by a project source."""

    dll: str
    definition_source_component: str
    definition_path: str
    definition_format: str
    output: str
    symbols: List[str] = field(default_factory=list)


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
    sha256: Optional[str] = None
    workdir: Optional[str] = None
    platform_overrides: Dict[str, PlatformOverride] = field(default_factory=dict)
    extra_env: Dict[str, str] = field(default_factory=dict)
    post_install: Optional[str] = ""
    custom_build_fn: Optional[str] = None
    ffmpeg_configure_flag: Optional[str] = None
    ffmpeg_configure_flags_by_version: Dict[str, List[str]] = field(default_factory=dict)
    skip_condition: Optional[str] = None
    extra_libs: str = ""
    sed_patches: Dict[str, str] = field(default_factory=dict)
    versions: Dict[str, ComponentVersion] = field(default_factory=dict)
    # Toolchain profile this component requires. ``"n/a"`` means "inherit the
    # platform profile's effective toolchain"; any other value names a profile
    # declared in ``toolchains.yaml`` (resolved in later phases).
    toolchain: str = "n/a"
    # NVENC API major version (as reported by the installed driver) -> declared
    # nv-codec component version. Replaces the previous driver-based heuristics.
    driver_nvenc_api_map: Dict[str, str] = field(default_factory=dict)
    # Target eligibility is independent of host-platform/tool eligibility.
    target_backends: List[str] = field(default_factory=list)
    # Backends on which this component is part of the build graph. This is
    # distinct from target_backends, which declares G2 target-input sources.
    build_backends: List[str] = field(default_factory=lambda: list(SUPPORTED_TARGET_BACKENDS))
    target_headers: List[TargetHeaderTree] = field(default_factory=list)
    target_import_interface: Optional[TargetImportInterface] = None
    build_role: BuildRole = BuildRole.TARGET
    target_link_metadata: TargetLinkMetadata = field(default_factory=TargetLinkMetadata)

    def with_version(self, version: str) -> "Component":
        """Return this component resolved to a declared source version."""
        source = self.versions.get(version)
        if source is None:
            versions = ", ".join(sorted(self.versions))
            raise ValueError(
                f"Unsupported {self.name} version {version!r}; supported versions: {versions}"
            )
        return replace(
            self,
            version=version,
            sha256=source.sha256 or self.sha256,
            url=source.url or self.url,
            archive_filename=source.archive_filename or self.archive_filename,
        )

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


@dataclass
class ComponentSelection:
    """Role-aware selection for the Linux-to-Windows build graph."""

    host_tools: List[Component] = field(default_factory=list)
    target_components: List[Component] = field(default_factory=list)
    mixed_components: List[Component] = field(default_factory=list)
    system_satisfied_host_requirements: List[str] = field(default_factory=list)
    missing_host_tools: List[str] = field(default_factory=list)
    target_components_to_build: List[Component] = field(default_factory=list)
    conditional_components: List[Component] = field(default_factory=list)
    target_inputs: List[Component] = field(default_factory=list)
    skipped_by_feature: List[Component] = field(default_factory=list)
    skipped_by_backend: List[Component] = field(default_factory=list)


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
        versions_data = data.get("versions") or {}
        versions = {
            str(version): ComponentVersion(
                sha256=source.get("sha256"),
                url=source.get("url"),
                archive_filename=source.get("archive_filename"),
            )
            for version, source in versions_data.items()
        }
        driver_nvenc_api_map = {
            str(api): str(version)
            for api, version in (data.get("driver_nvenc_api_map") or {}).items()
        }
        for api, target_version in driver_nvenc_api_map.items():
            if target_version not in versions:
                declared = ", ".join(sorted(versions)) or "<none>"
                raise ValueError(
                    f"driver_nvenc_api_map[{api!r}] references undeclared version "
                    f"{target_version!r}; declared versions: {declared}"
                )
        target_backends = data.get("target_backends") or []
        if not isinstance(target_backends, list) or not all(
            isinstance(backend, str) for backend in target_backends
        ):
            raise ValueError("target_backends must be a list of backend names")
        unknown_backends = sorted(set(target_backends) - set(SUPPORTED_TARGET_BACKENDS))
        if unknown_backends:
            raise ValueError(f"unknown target backend(s): {', '.join(unknown_backends)}")
        build_backends = data.get("build_backends", list(SUPPORTED_TARGET_BACKENDS))
        if not isinstance(build_backends, list) or not all(
            isinstance(backend, str) and backend for backend in build_backends
        ):
            raise ValueError("build_backends must be a list of backend names")
        unknown_build_backends = sorted(set(build_backends) - set(SUPPORTED_TARGET_BACKENDS))
        if unknown_build_backends:
            raise ValueError(f"unknown build backend(s): {', '.join(unknown_build_backends)}")
        requires_tools = data.get("requires_tools") or []
        if not isinstance(requires_tools, list) or not all(
            isinstance(tool, str) and tool.strip() for tool in requires_tools
        ):
            raise ValueError("requires_tools must be a list of non-empty tool names")

        def _relative_target_path(value: Any, field_name: str) -> str:
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty relative path")
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "." in path.parts
                or not path.parts
                or "\\" in value
                or ":" in value
            ):
                raise ValueError(f"{field_name} must be a safe relative path")
            return path.as_posix()

        target_headers_data = data.get("target_headers") or []
        if not isinstance(target_headers_data, list):
            raise ValueError("target_headers must be a list")
        target_headers = []
        for entry in target_headers_data:
            if not isinstance(entry, dict):
                raise ValueError("each target_headers entry must be a mapping")
            source = _relative_target_path(entry.get("source"), "target_headers.source")
            destination = _relative_target_path(
                entry.get("destination"), "target_headers.destination"
            )
            if PurePosixPath(destination).parts[0] != "include":
                raise ValueError("target_headers.destination must be under include/")
            target_headers.append(TargetHeaderTree(source=source, destination=destination))

        target_interface_data = data.get("target_import_interface")
        target_import_interface = None
        if target_interface_data is not None:
            if not isinstance(target_interface_data, dict):
                raise ValueError("target_import_interface must be a mapping")
            dll = target_interface_data.get("dll")
            source_component = target_interface_data.get("definition_source_component")
            if (
                not isinstance(dll, str)
                or not dll.lower().endswith(".dll")
                or "/" in dll
                or "\\" in dll
            ):
                raise ValueError("target_import_interface.dll must be a DLL basename")
            if not isinstance(source_component, str) or not source_component:
                raise ValueError("target_import_interface.definition_source_component is required")
            definition_format = target_interface_data.get("definition_format", "def")
            if definition_format not in ("def", "vulkan_registry"):
                raise ValueError(
                    "target_import_interface.definition_format must be def or vulkan_registry"
                )
            symbols = target_interface_data.get("symbols") or []
            if not isinstance(symbols, list) or not all(
                isinstance(symbol, str) and symbol for symbol in symbols
            ):
                raise ValueError("target_import_interface.symbols must be a list of names")
            if definition_format == "vulkan_registry" and not symbols:
                raise ValueError("vulkan_registry import interfaces must declare symbols")
            output = _relative_target_path(
                target_interface_data.get("output"), "target_import_interface.output"
            )
            if PurePosixPath(output).parts[0] != "lib":
                raise ValueError("target_import_interface.output must be under lib/")
            target_import_interface = TargetImportInterface(
                dll=dll,
                definition_source_component=source_component,
                definition_path=_relative_target_path(
                    target_interface_data.get("definition_path"),
                    "target_import_interface.definition_path",
                ),
                definition_format=definition_format,
                output=output,
                symbols=symbols,
            )
        try:
            build_role = BuildRole(data.get("build_role", BuildRole.TARGET.value))
        except ValueError as exc:
            raise ValueError(f"unknown build_role {data.get('build_role')!r}") from exc
        if build_role == BuildRole.HOST_TOOL and (
            target_backends or target_headers or target_import_interface
        ):
            raise ValueError(
                "HOST_TOOL components cannot declare target_backends, target_headers, "
                "or target_import_interface"
            )
        system_component = data.get("system_component", False)
        if not isinstance(system_component, bool):
            raise ValueError("system_component must be a boolean")
        system_tool_name = data.get("system_tool_name")
        if system_tool_name is not None and (
            not isinstance(system_tool_name, str) or not system_tool_name.strip()
        ):
            raise ValueError("system_tool_name must be a non-empty string when provided")
        link_data = data.get("target_link_metadata") or {}
        if not isinstance(link_data, dict):
            raise ValueError("target_link_metadata must be a mapping")
        if build_role == BuildRole.HOST_TOOL and link_data:
            raise ValueError("HOST_TOOL components cannot declare target_link_metadata")

        def _link_values(key: str) -> tuple[str, ...]:
            values = link_data.get(key) or []
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ValueError(f"target_link_metadata.{key} must be a list of strings")
            return tuple(values)

        runtime_data = link_data.get("runtime_dependencies") or []
        if not isinstance(runtime_data, list):
            raise ValueError("target_link_metadata.runtime_dependencies must be a list")
        runtime_dependencies = []
        for item in runtime_data:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not item["name"]
                or not isinstance(item.get("classification"), str)
                or not item["classification"]
                or not isinstance(item.get("source_identity"), str)
                or not item["source_identity"]
            ):
                raise ValueError(
                    "each target runtime dependency needs name, classification, "
                    "and source_identity"
                )
            runtime_dependencies.append(
                RuntimeDependency(
                    name=item["name"],
                    classification=item["classification"],
                    source_identity=item["source_identity"],
                )
            )
        target_link_metadata = TargetLinkMetadata(
            include_roots=_link_values("include_roots"),
            library_roots=_link_values("library_roots"),
            static_libraries=_link_values("static_libraries"),
            import_interfaces=_link_values("import_interfaces"),
            public_link_requirements=_link_values("public_link_requirements"),
            private_transitive_link_requirements=_link_values(
                "private_transitive_link_requirements"
            ),
            runtime_dependencies=tuple(runtime_dependencies),
            provenance=_link_values("provenance"),
        )
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
            requires_tools=requires_tools,
            gpl_only=bool(data.get("gpl_only", False)),
            non_gpl_only=bool(data.get("non_gpl_only", False)),
            linux_only=bool(data.get("linux_only", False)),
            macos_only=bool(data.get("macos_only", False)),
            windows_ucrt64_supported=bool(data.get("windows_ucrt64_supported", False)),
            system_component=system_component,
            system_tool_name=system_tool_name,
            archive_strip_components=int(data.get("archive_strip_components", 1)),
            archive_filename=data.get("archive_filename"),
            archive_dirname=data.get("archive_dirname"),
            sha256=data.get("sha256"),
            workdir=data.get("workdir"),
            platform_overrides=cls._platform_overrides_from_dict(
                data.get("platform_overrides") or {}
            ),
            extra_env=dict(data.get("extra_env") or {}),
            post_install=data.get("post_install") or None,
            custom_build_fn=data.get("custom_build_fn"),
            ffmpeg_configure_flag=data.get("ffmpeg_configure_flag"),
            ffmpeg_configure_flags_by_version={
                str(version): list(flags)
                for version, flags in (data.get("ffmpeg_configure_flags_by_version") or {}).items()
            },
            skip_condition=data.get("skip_condition"),
            extra_libs=data.get("extra_libs") or "",
            sed_patches=dict(data.get("sed_patches") or {}),
            versions=versions,
            toolchain=str(data.get("toolchain", "n/a")),
            driver_nvenc_api_map=driver_nvenc_api_map,
            target_backends=target_backends,
            build_backends=build_backends,
            target_headers=target_headers,
            target_import_interface=target_import_interface,
            build_role=build_role,
            target_link_metadata=target_link_metadata,
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
        component_names = {component.name for component in self._components}
        for component in self._components:
            interface = component.target_import_interface
            if interface and interface.definition_source_component not in component_names:
                raise ValueError(
                    f"Component {component.name!r} target import interface references missing "
                    f"definition source {interface.definition_source_component!r}"
                )

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

    def get_ffmpeg_component(self, version: str) -> Component:
        """Get the FFmpeg target resolved to a supported source version."""
        component = next(
            (item for item in self._components if item.category == ComponentCategory.TARGET), None
        )
        if component is None:
            raise ValueError("FFmpeg target component is missing from the registry")
        return component.with_version(version)

    def get_nv_codec_component(self, platform_info: Optional[Any] = None) -> Component:
        """Get the nv-codec-headers component resolved to a driver-compatible version.

        ``platform_info.nvenc_api_version`` ("<major>.<minor>") is the maximum
        NVENC API version reported by the installed NVIDIA driver (see
        ``PlatformDetector._detect_nvenc_api_version``). The registry declares an
        explicit ``driver_nvenc_api_map`` (NVENC API major -> declared version);
        the matching entry is selected. Without a mapped match (or without
        detection) the declared default version is kept unchanged. There is no
        automatic version search or best-effort fallback.
        """
        component = self.get_by_name("nv-codec")
        if component is None:
            raise ValueError("nv-codec component is missing from the registry")

        detected = getattr(platform_info, "nvenc_api_version", None) if platform_info else None
        if not detected:
            return component

        major = str(detected).split(".")[0].strip()
        target = component.driver_nvenc_api_map.get(major)
        if target and target in component.versions:
            return component.with_version(target)
        return component

    def get_buildable(
        self,
        gpl_enabled: bool,
        platform: str,
        tools: Dict,
        disable_lv2: bool = False,
        enable_libvmaf: bool = True,
        platform_info: Optional[Any] = None,
        full_static: bool = False,
        ffmpeg_version: str = "8.1",
        toolchain_checker: Optional[ToolchainChecker] = None,
    ) -> List[Component]:
        """Get list of components that should be built.

        Tool requirements are evaluated against the *effective* toolset the
        build will actually have at each point, not just the pre-build system
        tools. Some tools (``meson``, ``ninja``, ``cmake``, ...) are themselves
        components earlier in the build order; when absent from the system they
        are built from source into the workspace and become available for every
        later component. A component is therefore included when each of its
        ``requires_tools`` is either present on the system or provided by an
        earlier component that is itself buildable.

        The toolchain gate runs before the tool gate: an unsatisfied explicit
        lower toolchain profile excludes the component regardless of tool
        availability (``rav1e`` excepted — it is always kept so a
        missing-but-required toolchain surfaces as a build error).

        Args:
            gpl_enabled: Whether GPL is enabled.
            platform: Platform name ("linux", "darwin", "windows").
            tools: Available system tools dict.
            disable_lv2: Whether LV2 is disabled.
            enable_libvmaf: Whether libvmaf is enabled.
            platform_info: PlatformInfo for HW acceleration filtering.
            full_static: Whether this is a fully-static build.
            ffmpeg_version: FFmpeg target version to resolve.
            toolchain_checker: Optional per-component toolchain gate.

        Returns:
            List of components to build.
        """
        # Pass 1: everything except the tool gate, preserving registry order.
        candidates = [
            (
                self.get_ffmpeg_component(ffmpeg_version)
                if comp.category == ComponentCategory.TARGET
                else self.get_nv_codec_component(platform_info) if comp.name == "nv-codec" else comp
            )
            for comp in self._components
            if TARGET_BACKEND_NATIVE in comp.build_backends
            if self._is_eligible(
                comp, gpl_enabled, platform, tools, platform_info, disable_lv2, enable_libvmaf
            )
        ]

        # Tools already available on the system before the build starts.
        available_tools = {
            name for name, info in tools.items() if getattr(info, "available", False)
        }

        result = []
        for comp in candidates:
            if (
                comp.name != "rav1e"
                and toolchain_checker is not None
                and not toolchain_checker(comp)
            ):
                continue
            if comp.requires_tools:
                missing = [t for t in comp.requires_tools if t not in available_tools]
                if missing and comp.name not in ("rav1e",):
                    continue

            result.append(comp)

            # Once accepted, any tool this component provides becomes available
            # for subsequent components in the build order.
            provided = self._tool_provided_by(comp)
            if provided is not None:
                available_tools.add(provided)

        return result

    def get_buildable_selection(
        self,
        gpl_enabled: bool,
        platform: str,
        tools: Dict,
        *,
        disable_lv2: bool = False,
        enable_libvmaf: bool = True,
        platform_info: Optional[Any] = None,
        ffmpeg_version: str = "8.1",
        toolchain_checker: Optional[ToolchainChecker] = None,
        target_backend: str = TARGET_BACKEND_CROSS_MINGW,
        host_tool_checker: Optional[Callable[[str], bool]] = None,
        target_platform_info: Optional[Any] = None,
    ) -> ComponentSelection:
        """Select cross-build components without using WSL hardware as a gate.

        Required target components remain selected when host capabilities are
        absent; they are reported as conditional instead. Target inputs staged
        by G2 are kept separate from source-built target components, and
        HOST_TOOL entries are never returned in the target build list.
        """
        if target_backend != TARGET_BACKEND_CROSS_MINGW:
            raise ValueError("get_buildable_selection is only defined for linux-cross-mingw")

        candidates: List[Component] = []
        skipped_by_feature: List[Component] = []
        skipped_by_backend: List[Component] = []
        for component in self._components:
            selected = (
                self.get_ffmpeg_component(ffmpeg_version)
                if component.category == ComponentCategory.TARGET
                else (
                    self.get_nv_codec_component(platform_info)
                    if component.name == "nv-codec"
                    else component
                )
            )
            if self._feature_excludes(component, gpl_enabled, disable_lv2, enable_libvmaf):
                skipped_by_feature.append(selected)
                continue
            if target_backend not in component.build_backends:
                if component.is_available(gpl_enabled, platform, tools, platform_info):
                    skipped_by_backend.append(selected)
                continue
            if not self._is_eligible(
                component,
                gpl_enabled,
                platform,
                tools,
                platform_info,
                disable_lv2,
                enable_libvmaf,
                check_capabilities=False,
            ):
                continue
            if (
                component.build_role != BuildRole.HOST_TOOL
                and component.name != "rav1e"
                and toolchain_checker is not None
                and not toolchain_checker(selected)
            ):
                continue
            candidates.append(selected)

        target_inputs_by_name = {
            component.name: component for component in self.get_target_inputs(target_backend)
        }
        target_inputs: List[Component] = []
        if "nv-codec" in target_inputs_by_name:
            target_inputs_by_name["nv-codec"] = self.get_nv_codec_component(platform_info)
        for component in candidates:
            if component.name in target_inputs_by_name:
                target_inputs.append(component)

        host_tools = [c for c in candidates if c.build_role == BuildRole.HOST_TOOL]
        target_components = [
            c
            for c in candidates
            if c.build_role == BuildRole.TARGET_COMPONENT and c.name not in target_inputs_by_name
        ]
        mixed_components = [c for c in candidates if c.build_role == BuildRole.MIXED]

        def tool_available(name: str) -> bool:
            info = tools.get(name)
            if info is not None and getattr(info, "available", False):
                return True
            return bool(host_tool_checker and host_tool_checker(name))

        system_satisfied = sorted(
            {
                component.system_tool_name or component.name
                for component in host_tools
                if tool_available(component.system_tool_name or component.name)
            }
        )
        missing_host_tools = {
            component.system_tool_name or component.name
            for component in host_tools
            if not tool_available(component.system_tool_name or component.name)
        }
        required_tools = {tool for component in candidates for tool in component.requires_tools}
        missing_host_tools.update(tool for tool in required_tools if not tool_available(tool))

        conditional = [
            component
            for component in candidates
            if component.build_role != BuildRole.HOST_TOOL
            and not self._is_eligible(
                component,
                gpl_enabled,
                platform,
                tools,
                target_platform_info or platform_info,
                disable_lv2,
                enable_libvmaf,
                check_capabilities=True,
            )
        ]
        conditional_names = {component.name for component in conditional}
        target_components_to_build = [
            component for component in target_components if component.name not in conditional_names
        ]
        # Mixed recipes require explicit host/target dispatch and are not
        # silently folded into the target-only build sequence.
        return ComponentSelection(
            host_tools=host_tools,
            target_components=target_components,
            mixed_components=mixed_components,
            system_satisfied_host_requirements=system_satisfied,
            missing_host_tools=sorted(missing_host_tools),
            target_components_to_build=target_components_to_build,
            conditional_components=conditional,
            target_inputs=target_inputs,
            skipped_by_feature=skipped_by_feature,
            skipped_by_backend=skipped_by_backend,
        )

    @staticmethod
    def _feature_excludes(
        component: Component,
        gpl_enabled: bool,
        disable_lv2: bool,
        enable_libvmaf: bool,
    ) -> bool:
        return bool(
            (component.gpl_only and not gpl_enabled)
            or (component.non_gpl_only and gpl_enabled)
            or (component.skip_condition == "disable_lv2" and disable_lv2)
            or (component.name == "libvmaf" and not enable_libvmaf)
        )

    def _is_eligible(
        self,
        comp: Component,
        gpl_enabled: bool,
        platform: str,
        tools: Dict,
        platform_info: Optional[Any],
        disable_lv2: bool,
        enable_libvmaf: bool,
        check_capabilities: bool = True,
    ) -> bool:
        """Return whether a component passes every filter except ``requires_tools``.

        The tool gate is applied separately (see :meth:`get_buildable`) so that
        tools provided by earlier build steps can satisfy later requirements.
        """
        if not comp.is_available(gpl_enabled, platform, tools, platform_info):
            return False

        if not self._is_component_allowed_for_platform_policy(comp, platform, platform_info):
            return False

        if comp.skip_condition == "disable_lv2" and disable_lv2:
            return False

        if comp.name == "libvmaf" and not enable_libvmaf:
            return False

        # Filter HW acceleration components by availability
        if check_capabilities and platform_info is not None:
            if (
                comp.name == "nv-codec"
                and not platform_info.cuda_available
                and not getattr(platform_info, "nvenc_api_version", None)
            ):
                return False
            if comp.name == "glslang" and not platform_info.vulkan_available:
                return False
            if comp.name == "amf" and not platform_info.amf_available:
                return False
            if comp.name in ("opencl-headers", "opencl-icd-loader"):
                opencl_runtime_available = getattr(platform_info, "opencl_runtime_available", False)
                if not (platform_info.opencl_available or opencl_runtime_available):
                    return False
            if comp.name == "onevpl" and not platform_info.qsv_available:
                return False

        return True

    @staticmethod
    def _tool_provided_by(component: Component) -> Optional[str]:
        """Return the tool name a component makes available once built.

        System-provided build-tool components (``system_component`` with a
        ``system_tool_name``) install their tool into the workspace when the
        system copy is missing, making it usable by later components. Returns
        ``None`` for components that do not provide a reusable tool.
        """
        if not component.system_component:
            return None
        return component.system_tool_name or component.name

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

    def get_target_inputs(self, target_backend: str) -> List[Component]:
        """Return components explicitly declared as inputs for a target backend."""
        if target_backend not in SUPPORTED_TARGET_BACKENDS:
            backends = ", ".join(SUPPORTED_TARGET_BACKENDS)
            raise ValueError(
                f"Unsupported target backend {target_backend!r}; supported: {backends}"
            )
        return [
            component
            for component in self._components
            if target_backend in component.target_backends and component.target_headers
        ]

    def get_target_link_metadata(self, component_names: List[str]) -> TargetLinkMetadata:
        """Compose target link metadata for selected components and dependencies."""
        selected = {component.name: component for component in self._components}
        ordered: List[Component] = []
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited or name not in selected:
                return
            visited.add(name)
            component = selected[name]
            for dependency in component.depends_on:
                visit(dependency)
            ordered.append(component)

        for name in component_names:
            visit(name)
        return compose_target_link_metadata(
            *(component.target_link_metadata for component in ordered)
        )

    def get_ffmpeg_configure_flags(
        self,
        built_components: List[str],
        gpl_enabled: bool,
        platform: str,
        platform_info: Optional[Any] = None,
        ffmpeg_version: str = "8.1",
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
            if comp.name in built_components:
                flags.extend(comp.ffmpeg_configure_flags_by_version.get(ffmpeg_version, []))

        if platform == "darwin":
            flags.append("--enable-videotoolbox")

        opencl_components_built = (
            "opencl-headers" in built_components and "opencl-icd-loader" in built_components
        )
        opencl_system_ready = False
        if platform_info is not None:
            opencl_system_ready = bool(
                getattr(platform_info, "opencl_available", False)
                or (
                    getattr(platform_info, "opencl_runtime_available", False)
                    and getattr(platform_info, "opencl_dev_available", False)
                )
            )

        if platform == "darwin" or opencl_components_built or opencl_system_ready:
            if "--enable-opencl" not in flags:
                flags.append("--enable-opencl")

        return flags
