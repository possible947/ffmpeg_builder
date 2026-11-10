"""Host/target environment boundaries for the WSL-Mingw backend."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_PATH_LIST_VARIABLES = (
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "PKG_CONFIG_PATH",
    "PKG_CONFIG_LIBDIR",
    "PKG_CONFIG_SYSROOT_DIR",
    "CMAKE_PREFIX_PATH",
    "CMAKE_MODULE_PATH",
)
_HOST_ONLY_VARIABLES = (
    "CUDA_HOME",
    "CUDA_PATH",
    "VULKAN_SDK",
    "CUDA_NVCC_CCBIN",
    "NVCC_PREPEND_FLAGS",
    "NVCC_APPEND_FLAGS",
)
_VULKAN_RUNTIME_VARIABLES = (
    "VK_LAYER_PATH",
    "VK_ADD_LAYER_PATH",
    "VK_ICD_FILENAMES",
    "VK_DRIVER_FILES",
    "VK_IMPLICIT_LAYER_PATH",
    "VK_EXPLICIT_LAYER_PATH",
)


@dataclass(frozen=True)
class FilteredEnvironmentPath:
    """A host-only environment value omitted from target dependency discovery."""

    variable: str
    value: str
    prerequisite: str | None
    reason: str


@dataclass(frozen=True)
class WslMingwTargetEnvironment:
    """Filtered child environment and structured records of removed host inputs."""

    environment: Mapping[str, str]
    removed_paths: tuple[FilteredEnvironmentPath, ...]


def _canonical_path(value: str) -> Path:
    path = Path(value).expanduser()
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.normpath(str(path))))


def _matching_prerequisite(path: Path, roots: Mapping[str, tuple[Path, ...]]) -> str | None:
    for prerequisite, prerequisite_roots in roots.items():
        for root in prerequisite_roots:
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return prerequisite
    return None


def _is_within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def filter_wsl_mingw_target_environment(
    parent_environment: Mapping[str, str],
    external_sdk_roots: Mapping[str, Sequence[str | Path]],
    target_input_roots: Sequence[str | Path] = (),
) -> WslMingwTargetEnvironment:
    """Keep only approved target paths in discovery variables for a child environment.

    SDK roots are reported distinctly from other unapproved host paths. This is
    a filtering primitive, not the final G3 cross environment; callers must
    still construct target compiler and dependency paths explicitly.
    """
    roots = {
        prerequisite: tuple(_canonical_path(str(root)) for root in prerequisite_roots)
        for prerequisite, prerequisite_roots in external_sdk_roots.items()
    }
    approved_target_roots = tuple(_canonical_path(str(root)) for root in target_input_roots)
    environment = dict(parent_environment)
    removed: list[FilteredEnvironmentPath] = []

    for variable in _HOST_ONLY_VARIABLES:
        value = environment.pop(variable, None)
        if value is not None:
            removed.append(
                FilteredEnvironmentPath(
                    variable=variable,
                    value=value,
                    prerequisite=_matching_prerequisite(_canonical_path(value), roots),
                    reason="host-only SDK or tool setting",
                )
            )

    for variable in _VULKAN_RUNTIME_VARIABLES:
        value = environment.pop(variable, None)
        if value is not None:
            for entry in value.split(os.pathsep):
                if entry:
                    removed.append(
                        FilteredEnvironmentPath(
                            variable=variable,
                            value=entry,
                            prerequisite=_matching_prerequisite(_canonical_path(entry), roots),
                            reason="Linux Vulkan loader/runtime configuration",
                        )
                    )

    for variable in _PATH_LIST_VARIABLES:
        value = environment.get(variable)
        if value is None:
            continue
        retained: list[str] = []
        seen: set[Path] = set()
        for entry in value.split(os.pathsep):
            if not entry:
                continue
            canonical_entry = _canonical_path(entry)
            prerequisite = _matching_prerequisite(canonical_entry, roots)
            if prerequisite is not None:
                removed.append(
                    FilteredEnvironmentPath(
                        variable=variable,
                        value=entry,
                        prerequisite=prerequisite,
                        reason="path is inside an external Linux SDK",
                    )
                )
                continue
            if not _is_within_any(canonical_entry, approved_target_roots):
                removed.append(
                    FilteredEnvironmentPath(
                        variable=variable,
                        value=entry,
                        prerequisite=None,
                        reason="path is not an approved Windows target input",
                    )
                )
                continue
            if canonical_entry in seen:
                continue
            seen.add(canonical_entry)
            retained.append(entry)
        if retained:
            environment[variable] = os.pathsep.join(retained)
        else:
            environment.pop(variable, None)

    return WslMingwTargetEnvironment(environment=environment, removed_paths=tuple(removed))
