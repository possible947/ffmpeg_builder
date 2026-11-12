"""Verify the installed Vulkan headers and their matching registry."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from ...build_types import BuildError
from ...state import ComponentStatus

if TYPE_CHECKING:
    from ...components import Component
    from ...state import StateManager


def _digest(root: Path, registry: Path) -> str:
    digest = hashlib.sha256()
    files = []
    for name in ("vulkan", "vk_video"):
        directory = root / name
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        files.extend(sorted(path for path in directory.rglob("*") if path.is_file()))
    if (
        not (root / "vulkan" / "vulkan.h").is_file()
        or not (root / "vulkan" / "vulkan_core.h").is_file()
        or not registry.is_file()
    ):
        raise FileNotFoundError("Vulkan headers or registry are missing")
    for path in [*files, registry]:
        name = path.relative_to(root).as_posix() if path != registry else "registry/vk.xml"
        digest.update(name.encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _manifest_path(workspace: Path) -> Path:
    return workspace / "share" / "vulkan" / "headers-manifest.json"


def record_vulkan_headers(workspace: Path, source: Path, component: Component) -> None:
    """Record provenance only after verifying the installed files against the source."""
    registry = workspace / "share" / "vulkan" / "registry" / "vk.xml"
    try:
        installed = _digest(workspace / "include", registry)
        original = _digest(source / "include", source / "registry" / "vk.xml")
        if installed != original:
            raise ValueError("installed headers and registry differ from the source archive")
        manifest = _manifest_path(workspace)
        temporary = manifest.with_name(f"{manifest.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": component.version,
                        "archive_sha256": component.sha256,
                        "digest": installed,
                    }
                ),
                encoding="utf-8",
            )
            os.replace(temporary, manifest)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        raise BuildError(
            component.name, f"Cannot verify Vulkan headers and registry: {exc}"
        ) from exc


def vulkan_headers_valid(workspace: Path, component: Component) -> bool:
    try:
        manifest = json.loads(_manifest_path(workspace).read_text(encoding="utf-8"))
        registry = workspace / "share" / "vulkan" / "registry" / "vk.xml"
        return bool(
            manifest["version"] == component.version
            and manifest["archive_sha256"] == component.sha256
            and manifest["digest"] == _digest(workspace / "include", registry)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def invalidate_vulkan_resume(
    state_manager: StateManager, workspace: Path, components: Iterable[Component]
) -> None:
    """Rebuild old or modified Vulkan inputs and their completed consumers."""
    component = next((item for item in components if item.name == "vulkan-headers"), None)
    if component is None or vulkan_headers_valid(workspace, component):
        return
    states = state_manager.get().components
    for name in ("vulkan-headers", "libplacebo", "ffmpeg"):
        state = states.get(name)
        if state and state.status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM):
            state_manager.mark_component_status(
                name, ComponentStatus.PENDING, state.version, detail="Vulkan inputs changed"
            )
