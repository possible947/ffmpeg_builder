"""Toolchain registry: profiles, platform profiles, and explicit mapping tables.

The registry is loaded from ``toolchains.yaml`` (next to this module) and is the
single source of truth for build toolchains. Loading performs fail-fast
structural and reference validation, so a misconfigured registry surfaces at
load time rather than in the middle of a build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

import yaml

ALLOWED_KINDS = frozenset({"compiler", "build_tool", "rust", "rust_tool", "bootstrap_lib"})
ALLOWED_PLATFORMS = frozenset({"linux", "darwin", "windows"})
ALLOWED_SOURCE_TYPES = frozenset({"build", "binary", "download", "local"})
ALLOWED_RECIPES = frozenset({"gcc", "autotools"})

_SHA256_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_RANGE_OP_RE = re.compile(r"\A(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)\Z")


@dataclass(frozen=True)
class BootstrapSpec:
    """How a source-built toolchain profile is bootstrapped."""

    compiler: str = "system"
    requires: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolchainSource:
    """One acquisition variant for a toolchain profile."""

    type: str
    url: Optional[str] = None
    archive_filename: Optional[str] = None
    sha256: Optional[str] = None
    path: Optional[str] = None
    bootstrap: Optional[BootstrapSpec] = None
    recipe: Optional[str] = None
    configure_args: List[str] = field(default_factory=list)
    build_args: List[str] = field(default_factory=list)
    install_args: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolchainProfile:
    """A named toolchain profile resolved to a pinned version."""

    name: str
    kind: str
    platforms: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    version: Optional[str] = None
    materialize: Dict[str, Optional[str]] = field(default_factory=dict)
    patches: List[Dict[str, str]] = field(default_factory=list)
    sources: Dict[str, ToolchainSource] = field(default_factory=dict)
    default_source: Optional[str] = None

    @property
    def deployable(self) -> bool:
        """Whether the profile declares a usable acquisition source."""
        return self.default_source is not None and self.default_source in self.sources


@dataclass(frozen=True)
class PlatformProfile:
    """Upper-level profile keyed by (ffmpeg_version, platform)."""

    name: str
    ffmpeg_version: str
    platform: str
    system_tool_range: Dict[str, str] = field(default_factory=dict)
    fallback_toolchain: Optional[str] = None


@dataclass(frozen=True)
class ToolchainAssignment:
    """The effective toolchain resolved for a component.

    ``mode`` is ``"system"`` (use the host toolchain) or ``"deploy"`` (deploy
    ``profile`` from the toolchain registry).
    """

    mode: str
    profile: Optional[str] = None


@dataclass(frozen=True)
class ComponentToolchainPlan:
    """Resolved toolchain assignment for one component (read-only)."""

    component: str
    toolchain: str
    assignment: Optional[ToolchainAssignment]
    cuda_toolchain: Optional[str] = None


class _HasToolchain(Protocol):
    """Structural type for anything with a component name and toolchain field."""

    name: str
    toolchain: str


class ToolchainRegistry:
    """Registry of toolchain profiles, platform profiles, and mapping tables."""

    # Path to the YAML registry file (next to this module)
    _YAML_PATH = Path(__file__).parent / "toolchains.yaml"

    def __init__(self, yaml_path: Optional[Path] = None) -> None:
        """Load and validate the toolchain registry.

        Args:
            yaml_path: Optional override path to the toolchain YAML file.
                       Defaults to ``toolchains.yaml`` next to this module.
        """
        self.profiles: Dict[str, ToolchainProfile] = {}
        self.platform_profiles: Dict[str, PlatformProfile] = {}
        self.cuda_map: Dict[str, str] = {}
        self._load(yaml_path or self._YAML_PATH)

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def get_profile(self, name: str) -> Optional[ToolchainProfile]:
        """Return a toolchain profile by name, or ``None`` if not declared."""
        return self.profiles.get(name)

    def get_platform_profile(self, name: str) -> Optional[PlatformProfile]:
        """Return a platform profile by name, or ``None`` if not declared."""
        return self.platform_profiles.get(name)

    def resolve_platform_profile(
        self, ffmpeg_version: str, platform: str
    ) -> Optional[PlatformProfile]:
        """Resolve the platform profile for an (ffmpeg_version, platform) pair."""
        wanted = str(ffmpeg_version)
        for profile in self.platform_profiles.values():
            if profile.ffmpeg_version == wanted and profile.platform == platform:
                return profile
        return None

    def default_toolchain_for(self, ffmpeg_version: str, platform: str) -> Optional[str]:
        """Return the fallback toolchain profile name for a platform profile.

        This is the profile a component with ``toolchain: n/a`` resolves to when
        the system toolchain is out of the allowed range; ``None`` means the
        system toolchain is used.
        """
        profile = self.resolve_platform_profile(ffmpeg_version, platform)
        return profile.fallback_toolchain if profile is not None else None

    def is_profile_available(self, profile: ToolchainProfile, platform: str) -> bool:
        """Whether a toolchain profile applies to ``platform`` and is acquirable.

        A profile must list the platform and carry a usable acquisition source.
        Installation state is checked by the caller (the store), not here.
        """
        return platform in profile.platforms and profile.deployable

    def toolchain_for_cuda(self, cuda_version: str) -> Optional[str]:
        """Return the toolchain profile name mapped to a CUDA Toolkit version."""
        return self.cuda_map.get(str(cuda_version))

    # ------------------------------------------------------------------
    # Resolution (read-only; no deployment)
    # ------------------------------------------------------------------

    def resolve_effective_toolchain(
        self,
        ffmpeg_version: str,
        platform: str,
        system_tool_versions: Mapping[str, str],
    ) -> Optional[ToolchainAssignment]:
        """Effective toolchain for components declared as ``toolchain: n/a``.

        If the host system toolchain is within the platform profile's allowed
        range, the system toolchain is used; otherwise the profile's
        ``fallback_toolchain`` is resolved for deployment. Returns ``None`` when
        neither applies (no platform profile, or system out of range with no
        fallback), which callers treat as an unresolved/skip candidate.
        """
        profile = self.resolve_platform_profile(ffmpeg_version, platform)
        if profile is None:
            return None
        if _system_tool_in_range(profile.system_tool_range, system_tool_versions):
            return ToolchainAssignment(mode="system")
        if profile.fallback_toolchain is not None:
            return ToolchainAssignment(mode="deploy", profile=profile.fallback_toolchain)
        return None

    def resolve_component_toolchain(
        self,
        component_toolchain: str,
        ffmpeg_version: str,
        platform: str,
        system_tool_versions: Mapping[str, str],
    ) -> Optional[ToolchainAssignment]:
        """Resolve the effective toolchain for a single component.

        An explicit non-``n/a`` value is a lower profile and is used verbatim
        (no automatic substitution); ``"n/a"`` inherits the platform profile's
        effective toolchain.
        """
        if component_toolchain and component_toolchain != "n/a":
            return ToolchainAssignment(mode="deploy", profile=component_toolchain)
        return self.resolve_effective_toolchain(ffmpeg_version, platform, system_tool_versions)

    def plan_component_toolchains(
        self,
        components: Iterable[_HasToolchain],
        ffmpeg_version: str,
        platform: str,
        system_tool_versions: Mapping[str, str],
        cuda_version: Optional[str] = None,
    ) -> List[ComponentToolchainPlan]:
        """Build a read-only toolchain plan for a component list.

        When ``cuda_version`` is given, the effective toolchain for the
        ``nv-codec`` component additionally honours the registry ``cuda``
        table: the mapped host compiler is shown as the CUDA assignment,
        while ``unresolved`` entries keep their skip semantics.
        """
        plan: List[ComponentToolchainPlan] = []
        cuda_profile = self.toolchain_for_cuda(cuda_version) if cuda_version else None
        for component in components:
            assignment = self.resolve_component_toolchain(
                component.toolchain, ffmpeg_version, platform, system_tool_versions
            )
            plan.append(
                ComponentToolchainPlan(
                    component=component.name,
                    toolchain=component.toolchain,
                    assignment=assignment,
                    cuda_toolchain=cuda_profile if component.name == "nv-codec" else None,
                )
            )
        return plan

    # ------------------------------------------------------------------
    # YAML loader + validation
    # ------------------------------------------------------------------

    def _load(self, yaml_path: Path) -> None:
        """Load and validate the registry from a YAML file."""
        if not yaml_path.exists():
            raise FileNotFoundError(f"Toolchain registry file not found: {yaml_path}")
        with open(yaml_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(
                f"Expected top-level mapping in {yaml_path}, got {type(data).__name__}"
            )

        self.profiles = self._parse_profiles(_as_mapping(data.get("toolchains"), "toolchains"))
        self.platform_profiles = self._parse_platform_profiles(
            _as_mapping(data.get("platform_profiles"), "platform_profiles")
        )
        self.cuda_map = self._parse_cuda_map(_as_mapping(data.get("cuda"), "cuda"))
        self._validate_references()
        self._validate_bootstrap_cycles()

    def _parse_profiles(self, raw: Dict[str, Any]) -> Dict[str, ToolchainProfile]:
        profiles: Dict[str, ToolchainProfile] = {}
        for key, entry in raw.items():
            name = str(key)
            if not isinstance(entry, dict):
                raise ValueError(f"Toolchain profile {name!r} must be a mapping")
            kind = _require_str(entry, "kind", f"toolchain {name!r}")
            if kind not in ALLOWED_KINDS:
                raise ValueError(
                    f"Toolchain {name!r} has invalid kind {kind!r}; "
                    f"allowed: {', '.join(sorted(ALLOWED_KINDS))}"
                )
            platforms = _as_str_list(entry.get("platforms"), f"toolchain {name!r} platforms")
            for platform in platforms:
                if platform not in ALLOWED_PLATFORMS:
                    raise ValueError(
                        f"Toolchain {name!r} has invalid platform {platform!r}; "
                        f"allowed: {', '.join(sorted(ALLOWED_PLATFORMS))}"
                    )
            provides = _as_str_list(entry.get("provides"), f"toolchain {name!r} provides")
            version = entry.get("version")
            if version is not None and not isinstance(version, str):
                raise ValueError(f"Toolchain {name!r} version must be a string")
            materialize = _as_optional_str_mapping(
                entry.get("materialize"), f"toolchain {name!r} materialize"
            )
            patches = _as_patch_list(entry.get("patches"), f"toolchain {name!r} patches")
            sources, default_source = self._parse_sources(name, entry.get("source"))
            profiles[name] = ToolchainProfile(
                name=name,
                kind=kind,
                platforms=platforms,
                provides=provides,
                version=version,
                materialize=materialize,
                patches=patches,
                sources=sources,
                default_source=default_source,
            )
        return profiles

    @staticmethod
    def _parse_sources(name: str, raw: Any) -> Tuple[Dict[str, ToolchainSource], Optional[str]]:
        if raw is None:
            return {}, None
        if not isinstance(raw, dict):
            raise ValueError(f"Toolchain {name!r} source must be a mapping")

        default = raw.get("default")
        if not isinstance(default, str) or not default:
            raise ValueError(f"Toolchain {name!r} source requires a non-empty string 'default'")

        variants_raw = {str(k): v for k, v in raw.items() if k != "default"}
        if default not in variants_raw:
            raise ValueError(
                f"Toolchain {name!r} source default {default!r} is not among variants "
                f"{', '.join(sorted(variants_raw)) or '<none>'}"
            )

        sources: Dict[str, ToolchainSource] = {}
        for variant_name, variant in variants_raw.items():
            if variant_name not in ALLOWED_SOURCE_TYPES:
                raise ValueError(
                    f"Toolchain {name!r} source variant {variant_name!r} is invalid; "
                    f"allowed: {', '.join(sorted(ALLOWED_SOURCE_TYPES))}"
                )
            if not isinstance(variant, dict):
                raise ValueError(
                    f"Toolchain {name!r} source variant {variant_name!r} must be a mapping"
                )
            declared_type = _require_str(
                variant, "type", f"toolchain {name!r} source {variant_name!r}"
            )
            if declared_type != variant_name:
                raise ValueError(
                    f"Toolchain {name!r} source variant {variant_name!r} declares type "
                    f"{declared_type!r}; type must match the variant key"
                )

            url = variant.get("url")
            archive_filename = variant.get("archive_filename")
            sha256 = variant.get("sha256")
            path_value = variant.get("path")
            if declared_type == "local":
                if not isinstance(path_value, str) or not path_value:
                    raise ValueError(f"Toolchain {name!r} local source requires a non-empty 'path'")
            else:
                if not isinstance(url, str) or not url:
                    raise ValueError(
                        f"Toolchain {name!r} source variant {variant_name!r} requires a 'url'"
                    )
                if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
                    raise ValueError(
                        f"Toolchain {name!r} source variant {variant_name!r} requires a "
                        "64-hex 'sha256'"
                    )
                if archive_filename is not None and not isinstance(archive_filename, str):
                    raise ValueError(
                        f"Toolchain {name!r} source variant {variant_name!r} "
                        "'archive_filename' must be a string"
                    )

            context = f"toolchain {name!r} source {variant_name!r}"
            recipe = variant.get("recipe")
            configure_args = _as_str_list(
                variant.get("configure_args"), f"{context} configure_args"
            )
            build_args = _as_str_list(variant.get("build_args"), f"{context} build_args")
            install_args = _as_str_list(variant.get("install_args"), f"{context} install_args")
            if declared_type == "build":
                if recipe is not None and (
                    not isinstance(recipe, str) or recipe not in ALLOWED_RECIPES
                ):
                    raise ValueError(
                        f"{context} has invalid recipe {recipe!r}; "
                        f"allowed: {', '.join(sorted(ALLOWED_RECIPES))}"
                    )
            elif recipe is not None or configure_args or build_args or install_args:
                raise ValueError(
                    f"{context} may declare recipe/configure_args/build_args/install_args "
                    "only for a 'build' source"
                )

            sources[variant_name] = ToolchainSource(
                type=declared_type,
                url=url,
                archive_filename=archive_filename,
                sha256=sha256,
                path=path_value,
                bootstrap=_parse_bootstrap(name, variant.get("bootstrap")),
                recipe=recipe,
                configure_args=configure_args,
                build_args=build_args,
                install_args=install_args,
            )
        return sources, default

    def _parse_platform_profiles(self, raw: Dict[str, Any]) -> Dict[str, PlatformProfile]:
        result: Dict[str, PlatformProfile] = {}
        for key, entry in raw.items():
            name = str(key)
            if not isinstance(entry, dict):
                raise ValueError(f"Platform profile {name!r} must be a mapping")
            ffmpeg_version = _require_str(entry, "ffmpeg_version", f"platform profile {name!r}")
            platform = _require_str(entry, "platform", f"platform profile {name!r}")
            if platform not in ALLOWED_PLATFORMS:
                raise ValueError(
                    f"Platform profile {name!r} has invalid platform {platform!r}; "
                    f"allowed: {', '.join(sorted(ALLOWED_PLATFORMS))}"
                )
            system_tool_range = _as_str_mapping(
                entry.get("system_tool_range"), f"platform profile {name!r} system_tool_range"
            )
            for tool, constraint in system_tool_range.items():
                _validate_range(
                    constraint, f"platform profile {name!r} system_tool_range[{tool!r}]"
                )
            fallback = entry.get("fallback_toolchain")
            if fallback is not None and not isinstance(fallback, str):
                raise ValueError(
                    f"Platform profile {name!r} fallback_toolchain must be a string or null"
                )
            result[name] = PlatformProfile(
                name=name,
                ffmpeg_version=ffmpeg_version,
                platform=platform,
                system_tool_range=system_tool_range,
                fallback_toolchain=fallback,
            )
        return result

    @staticmethod
    def _parse_cuda_map(raw: Dict[str, Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"cuda {key!r} must map to a non-empty toolchain name")
            result[str(key)] = value
        return result

    def _validate_references(self) -> None:
        for profile in self.profiles.values():
            for source in profile.sources.values():
                if source.bootstrap is None:
                    continue
                for requirement in source.bootstrap.requires:
                    if requirement not in self.profiles:
                        raise ValueError(
                            f"Toolchain {profile.name!r} bootstrap requires unknown "
                            f"profile {requirement!r}"
                        )
        for platform_profile in self.platform_profiles.values():
            fallback = platform_profile.fallback_toolchain
            if fallback is not None and fallback not in self.profiles:
                raise ValueError(
                    f"Platform profile {platform_profile.name!r} references unknown "
                    f"fallback_toolchain {fallback!r}"
                )
        for cuda_version, profile_name in self.cuda_map.items():
            if profile_name not in self.profiles:
                raise ValueError(
                    f"cuda {cuda_version!r} references unknown toolchain {profile_name!r}"
                )

    def _validate_bootstrap_cycles(self) -> None:
        graph: Dict[str, set[str]] = {name: set() for name in self.profiles}
        for profile in self.profiles.values():
            for source in profile.sources.values():
                if source.bootstrap is not None:
                    graph[profile.name].update(source.bootstrap.requires)

        visited: set[str] = set()
        stack: set[str] = set()
        path: List[str] = []

        def visit(node: str) -> None:
            if node in stack:
                raise ValueError(
                    "Bootstrap dependency cycle detected: " + " -> ".join(path + [node])
                )
            if node in visited:
                return
            stack.add(node)
            path.append(node)
            for dependency in sorted(graph.get(node, set())):
                visit(dependency)
            path.pop()
            stack.discard(node)
            visited.add(node)

        for name in sorted(graph):
            visit(name)


# ----------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------


def _as_mapping(value: Any, context: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a mapping, got {type(value).__name__}")
    return value


def _require_str(entry: Dict[str, Any], key: str, context: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} requires a non-empty string {key!r}")
    return value


def _as_str_list(value: Any, context: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{context} must be a list of strings")
    return list(value)


def _as_str_mapping(value: Any, context: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"{context} must be a mapping of string to string")
    return {str(k): str(v) for k, v in value.items()}


def _as_optional_str_mapping(value: Any, context: str) -> Dict[str, Optional[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a mapping")
    result: Dict[str, Optional[str]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not (item is None or isinstance(item, str)):
            raise ValueError(f"{context} must map strings to strings or null")
        result[key] = item
    return result


def _as_patch_list(value: Any, context: str) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list of mappings")
    result: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in item.items()
        ):
            raise ValueError(f"{context} entries must be mappings of string to string")
        result.append({str(k): str(v) for k, v in item.items()})
    return result


def _parse_bootstrap(name: str, raw: Any) -> Optional[BootstrapSpec]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"Toolchain {name!r} bootstrap must be a mapping")
    compiler = raw.get("compiler", "system")
    if not isinstance(compiler, str) or not compiler:
        raise ValueError(f"Toolchain {name!r} bootstrap compiler must be a non-empty string")
    requires = _as_str_list(raw.get("requires"), f"toolchain {name!r} bootstrap requires")
    return BootstrapSpec(compiler=compiler, requires=requires)


# ----------------------------------------------------------------------
# Version-range evaluation
# ----------------------------------------------------------------------


def _version_tuple(value: str) -> Tuple[int, ...]:
    """Parse a dotted version string into a tuple of integers (leading digits)."""
    numbers: List[int] = []
    for chunk in str(value).split("."):
        match = re.match(r"\d+", chunk)
        if match is None:
            break
        numbers.append(int(match.group()))
    return tuple(numbers)


def _validate_range(value: str, context: str) -> None:
    """Validate that a range string is a comma-separated list of constraints."""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise ValueError(f"{context} must contain at least one version constraint")
    for part in parts:
        if _RANGE_OP_RE.match(part) is None:
            raise ValueError(f"{context} has invalid version constraint {part!r}")


def _satisfies_constraint(version: Tuple[int, ...], constraint: str) -> bool:
    """Evaluate a single constraint against a parsed version.

    Comparison uses the precision of the constraint target, so ``<=25`` compares
    major versions only (``25.3.0`` satisfies it) while ``<14`` compares majors.
    """
    match = _RANGE_OP_RE.match(constraint.strip())
    if match is None:
        raise ValueError(f"Invalid version constraint: {constraint!r}")
    op = match.group(1)
    target = _version_tuple(match.group(2))
    width = len(target)
    left = (version + (0,) * width)[:width]
    right = (target + (0,) * width)[:width]
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    return left == right


def _satisfies_range(version: Tuple[int, ...], value: str) -> bool:
    """Whether a parsed version satisfies every constraint in a range string."""
    return all(
        _satisfies_constraint(version, part)
        for part in (part.strip() for part in value.split(","))
        if part
    )


def _system_tool_in_range(
    system_tool_range: Mapping[str, str], system_tool_versions: Mapping[str, str]
) -> bool:
    """Whether the detected system tools satisfy an allowed range.

    An empty range is vacuously satisfied. A required tool whose version is
    unknown is treated as *not* satisfied (allow-list: unconfirmed is not
    allowed), so the profile's fallback toolchain is selected instead.
    """
    for tool, constraint in system_tool_range.items():
        detected = system_tool_versions.get(tool)
        if detected is None:
            return False
        if not _satisfies_range(_version_tuple(detected), constraint):
            return False
    return True
