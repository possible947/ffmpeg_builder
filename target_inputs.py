"""Materialise isolated project-managed inputs for Windows target builds."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from .build_types import BuildError
from .components import Component, ComponentRegistry
from .downloader import Downloader


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_source(archive: Path, component: Component, destination: Path) -> Path:
    destination.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:*") as source:
            source.extractall(destination, filter="data")
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise BuildError(component.name, f"Failed to extract source archive: {exc}") from exc

    top_level = list(destination.iterdir())
    if component.archive_strip_components == 1 and len(top_level) == 1 and top_level[0].is_dir():
        return top_level[0]
    return destination


def _copy_header_tree(source: Path, destination: Path, component: str) -> None:
    if not source.is_dir():
        raise BuildError(component, f"Declared target header directory is missing: {source.name}")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise BuildError(component, f"Symlink is not allowed in target headers: {path.name}")
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() != path.read_bytes():
                    raise BuildError(component, f"Conflicting target header: {relative}")
            else:
                shutil.copy2(path, target)


def _generate_import_interface(
    component: Component,
    definition_source: Component,
    source_root: Path,
    output_root: Path,
    toolchain_root: Path,
) -> Dict[str, object]:
    interface = component.target_import_interface
    if interface is None:
        raise BuildError(component.name, "Target import-interface metadata is missing")

    definition = source_root / interface.definition_path
    if not definition.is_file():
        raise BuildError(
            component.name,
            f"Target import definition is missing from {definition_source.name}: "
            f"{interface.definition_path}",
        )
    definition_file = definition
    with tempfile.TemporaryDirectory(
        prefix="target-import-definition-", dir=output_root
    ) as generated_definition_dir:
        if interface.definition_format == "vulkan_registry":
            try:
                registry = ET.parse(definition).getroot()
            except (OSError, ET.ParseError) as exc:
                raise BuildError(
                    component.name, f"Cannot read pinned Vulkan registry: {exc}"
                ) from exc
            declared = {
                command.findtext("proto/name") for command in registry.findall("./commands/command")
            }
            missing = sorted(set(interface.symbols) - declared)
            if missing:
                raise BuildError(
                    component.name,
                    f"Vulkan registry does not declare import symbol(s): {', '.join(missing)}",
                )
            definition_file = Path(generated_definition_dir) / "vulkan.def"
            definition_file.write_text(
                f"LIBRARY {interface.dll}\nEXPORTS\n"
                + "".join(f"  {symbol}\n" for symbol in interface.symbols),
                encoding="utf-8",
            )

        bin_dir = toolchain_root / "bin"
        dlltool = bin_dir / "x86_64-w64-mingw32-dlltool"
        if not dlltool.is_file() or not os.access(dlltool, os.X_OK):
            raise BuildError(component.name, f"LLVM-MinGW dlltool is unavailable: {dlltool}")
        try:
            dlltool.resolve().relative_to(toolchain_root.resolve())
        except ValueError as exc:
            raise BuildError(
                component.name, "LLVM-MinGW dlltool resolves outside its toolchain"
            ) from exc

        output = output_root / interface.output
        output.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                str(dlltool),
                "-m",
                "i386:x86-64",
                "-D",
                interface.dll,
                "-d",
                str(definition_file),
                "-l",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not output.is_file():
            detail = result.stderr.strip() or result.stdout.strip() or "no tool output"
            raise BuildError(component.name, f"Import-library generation failed: {detail}")

        objdump_candidates = (
            bin_dir / "x86_64-w64-mingw32-objdump",
            bin_dir / "llvm-objdump",
        )
        objdump = next((path for path in objdump_candidates if path.is_file()), None)
        if objdump is None:
            raise BuildError(
                component.name, "LLVM-MinGW objdump is unavailable for artifact inspection"
            )
        inspection = subprocess.run(
            [str(objdump), "-f", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        artifact_info = inspection.stdout + inspection.stderr
        lowered_info = artifact_info.lower()
        if inspection.returncode != 0 or not any(
            marker in lowered_info
            for marker in ("pe-x86-64", "coff-x86-64", "coff-import-file-x86-64")
        ):
            raise BuildError(
                component.name,
                "Generated import library is not a valid x86-64 Windows COFF archive",
            )
        if "elf" in lowered_info:
            raise BuildError(component.name, "Generated target import library contains ELF objects")

        return {
            "dll": interface.dll,
            "definition_source_component": definition_source.name,
            "definition_source_version": definition_source.version,
            "definition_source_sha256": definition_source.sha256 or "",
            "definition_path": interface.definition_path,
            "definition_format": interface.definition_format,
            "symbols": interface.symbols,
            "definition_sha256": _sha256(definition),
            "recipe": "llvm-dlltool-x86_64-v1",
            "toolchain": toolchain_root.name,
            "output": interface.output,
            "output_sha256": _sha256(output),
        }


def _validate_target_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BuildError("target-inputs", f"Target prefix contains a symlink: {path.name}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.name == "target-inputs.json":
            continue
        if not relative.parts or relative.parts[0] not in ("include", "lib"):
            raise BuildError("target-inputs", f"Unexpected target-prefix path: {relative}")
        lowered = path.name.lower()
        if (
            lowered.endswith((".so", ".dll", ".pc", ".la", ".cmake"))
            or ".so." in lowered
            or lowered == "cmakecache.txt"
        ):
            raise BuildError("target-inputs", f"Host/runtime artifact in target prefix: {relative}")
        with path.open("rb") as source:
            if source.read(4) == b"\x7fELF":
                raise BuildError(
                    "target-inputs", f"ELF object in Windows target prefix: {relative}"
                )

    manifest_path = root / "target-inputs.json"
    if manifest_path.exists():
        metadata = manifest_path.read_text(encoding="utf-8").lower()
        for forbidden in ("/opt/", "/usr/include", "/usr/lib", "msys2", "msys64", "session-state"):
            if forbidden in metadata:
                raise BuildError(
                    "target-inputs", f"Forbidden host path in target provenance: {forbidden}"
                )


def materialize_target_inputs(
    registry: ComponentRegistry,
    target_backend: str,
    source_archives_dir: Path,
    packages_dir: Path,
    target_prefix: Path,
    toolchain_root: Optional[Path] = None,
    allow_network_downloads: bool = False,
) -> Dict[str, object]:
    """Stage registered target headers and proven import interfaces in a fresh prefix."""
    components = registry.get_target_inputs(target_backend)
    if not components:
        raise BuildError("target-inputs", f"No target inputs declared for {target_backend}")

    target_prefix = target_prefix.resolve()
    protected_roots = (Path("/usr"), Path("/opt"), Path("/lib"), Path("/lib64"))
    if any(target_prefix == root or root in target_prefix.parents for root in protected_roots):
        raise BuildError("target-inputs", "Target staging prefix must not be a system directory")
    if target_prefix.exists():
        raise BuildError("target-inputs", f"Target staging prefix already exists: {target_prefix}")
    if (
        any(component.target_import_interface for component in components)
        and toolchain_root is None
    ):
        raise BuildError(
            "target-inputs", "The installed LLVM-MinGW ToolchainStore root is required"
        )
    target_prefix.parent.mkdir(parents=True, exist_ok=True)
    packages_dir.mkdir(parents=True, exist_ok=True)

    required_sources: Dict[str, Component] = {component.name: component for component in components}
    for component in components:
        interface = component.target_import_interface
        if interface:
            definition_component = registry.get_by_name(interface.definition_source_component)
            if definition_component is None:
                raise BuildError(
                    component.name,
                    f"Missing definition source component {interface.definition_source_component}",
                )
            required_sources[definition_component.name] = definition_component

    downloader = Downloader(
        packages_dir=packages_dir / "archives",
        source_archives_dir=source_archives_dir,
        allow_network_downloads=allow_network_downloads,
    )
    target_prefix.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{target_prefix.name}.target-inputs-", dir=target_prefix.parent)
    )
    try:
        extracted: Dict[str, Path] = {}
        with tempfile.TemporaryDirectory(prefix="target-inputs-sources-", dir=packages_dir) as tmp:
            extraction_root = Path(tmp)
            for name, component in required_sources.items():
                if not component.sha256:
                    raise BuildError(
                        name, "Target input source must have a pinned SHA-256 checksum"
                    )
                try:
                    archive = downloader.download(
                        component.get_url(),
                        component.get_archive_filename(),
                        expected_sha256=component.sha256,
                        show_progress=False,
                    )
                except RuntimeError as exc:
                    raise BuildError(name, f"Project source acquisition failed: {exc}") from exc
                extracted[name] = _extract_source(
                    archive, component, extraction_root / component.get_target_dir()
                )

            source_records: Dict[str, Dict[str, object]] = {}
            for component in components:
                for header_tree in component.target_headers:
                    source_path = extracted[component.name] / header_tree.source
                    destination = staging_root / header_tree.destination
                    _copy_header_tree(source_path, destination, component.name)
                source_records[component.name] = {
                    "version": component.version,
                    "url": component.get_url(),
                    "archive": component.get_archive_filename(),
                    "sha256": component.sha256 or "",
                    "headers": [
                        {"source": tree.source, "destination": tree.destination}
                        for tree in component.target_headers
                    ],
                }

            interfaces: List[Dict[str, object]] = []
            for component in components:
                interface = component.target_import_interface
                if interface is None:
                    continue
                if toolchain_root is None:
                    raise BuildError(
                        component.name, "The installed LLVM-MinGW ToolchainStore root is required"
                    )
                source_component = required_sources.get(interface.definition_source_component)
                if source_component is None:
                    raise BuildError(
                        component.name,
                        f"Missing definition source component {interface.definition_source_component}",
                    )
                interfaces.append(
                    _generate_import_interface(
                        component,
                        source_component,
                        extracted[source_component.name],
                        staging_root,
                        toolchain_root,
                    )
                )

        files = {
            path.relative_to(staging_root).as_posix(): _sha256(path)
            for path in sorted(staging_root.rglob("*"))
            if path.is_file()
        }
        identity = {
            "target_backend": target_backend,
            "sources": source_records,
            "interfaces": interfaces,
            "files": files,
        }
        materialization_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        manifest: Dict[str, object] = {
            **identity,
            "materialization_id": materialization_id,
        }
        (staging_root / "target-inputs.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _validate_target_tree(staging_root)
        staging_root.rename(target_prefix)
        return manifest
    except BuildError:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise
    except OSError as exc:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise BuildError("target-inputs", f"Target input materialisation failed: {exc}") from exc
