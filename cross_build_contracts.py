"""Typed contracts shared by the Linux-to-Windows production build path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RuntimeDependency:
    """A runtime requirement classified for future packaging policy."""

    name: str
    classification: str
    source_identity: str


@dataclass(frozen=True)
class TargetLinkMetadata:
    """Transitive link/interface requirements for Windows target artifacts."""

    include_roots: tuple[str, ...] = ()
    library_roots: tuple[str, ...] = ()
    static_libraries: tuple[str, ...] = ()
    import_interfaces: tuple[str, ...] = ()
    public_link_requirements: tuple[str, ...] = ()
    private_transitive_link_requirements: tuple[str, ...] = ()
    runtime_dependencies: tuple[RuntimeDependency, ...] = ()
    provenance: tuple[str, ...] = ()

    @property
    def consumer_link_requirements(self) -> tuple[str, ...]:
        """Return public and transitive static-link requirements in link order."""
        return _ordered_unique(
            self.private_transitive_link_requirements + self.public_link_requirements
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "include_roots": list(self.include_roots),
            "library_roots": list(self.library_roots),
            "static_libraries": list(self.static_libraries),
            "import_interfaces": list(self.import_interfaces),
            "public_link_requirements": list(self.public_link_requirements),
            "private_transitive_link_requirements": list(self.private_transitive_link_requirements),
            "runtime_dependencies": [
                {
                    "name": item.name,
                    "classification": item.classification,
                    "source_identity": item.source_identity,
                }
                for item in self.runtime_dependencies
            ],
            "provenance": list(self.provenance),
        }


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def compose_target_link_metadata(
    *items: TargetLinkMetadata,
) -> TargetLinkMetadata:
    """Compose interface metadata while preserving first-seen static-link order."""
    runtime: dict[tuple[str, str, str], RuntimeDependency] = {}
    for item in items:
        for dependency in item.runtime_dependencies:
            runtime[(dependency.name, dependency.classification, dependency.source_identity)] = (
                dependency
            )
    return TargetLinkMetadata(
        include_roots=_ordered_unique(tuple(v for item in items for v in item.include_roots)),
        library_roots=_ordered_unique(tuple(v for item in items for v in item.library_roots)),
        static_libraries=_ordered_unique(tuple(v for item in items for v in item.static_libraries)),
        import_interfaces=_ordered_unique(
            tuple(v for item in items for v in item.import_interfaces)
        ),
        public_link_requirements=_ordered_unique(
            tuple(v for item in items for v in item.public_link_requirements)
        ),
        private_transitive_link_requirements=_ordered_unique(
            tuple(
                v
                for item in items
                for v in (item.private_transitive_link_requirements + item.public_link_requirements)
            )
        ),
        runtime_dependencies=tuple(runtime.values()),
        provenance=_ordered_unique(tuple(v for item in items for v in item.provenance)),
    )


@dataclass(frozen=True)
class TargetInterfaceProjection:
    """Target-side Vulkan/OpenCL interfaces with host tools kept separate."""

    prefix: Path
    vulkan_headers: Path
    vulkan_registry: Path
    vulkan_import_library: Path
    opencl_headers: Path
    opencl_import_library: Path
    host_shader_tools: Mapping[str, str]
    runtime_dependencies: tuple[RuntimeDependency, ...]
