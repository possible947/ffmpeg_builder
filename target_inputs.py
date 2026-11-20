"""Materialise isolated project-managed inputs for Windows target builds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from .build_types import CATEGORY_PROJECT_SOURCE, BuildError
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


def _validate_target_tree(root: Path, allow_component_outputs: bool = False) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            if not allow_component_outputs:
                raise BuildError("target-inputs", f"Target prefix contains a symlink: {path.name}")
            try:
                path.resolve(strict=True).relative_to(root.resolve(strict=True))
            except (OSError, ValueError):
                raise BuildError(
                    "target-inputs", f"Target prefix contains an escaping symlink: {path.name}"
                )
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.name == "target-inputs.json":
            continue
        allowed_roots = (
            ("include", "lib", "share") if allow_component_outputs else ("include", "lib")
        )
        if not relative.parts or relative.parts[0] not in allowed_roots:
            raise BuildError("target-inputs", f"Unexpected target-prefix path: {relative}")
        lowered = path.name.lower()
        forbidden_suffixes: tuple[str, ...] = (".so", ".dll", ".exe", ".o", ".obj")
        if not allow_component_outputs:
            forbidden_suffixes += (".pc", ".la", ".cmake")
        if lowered.endswith(forbidden_suffixes) or ".so." in lowered or lowered == "cmakecache.txt":
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


def validate_target_inputs_manifest(
    manifest: object,
    target_prefix: Optional[Path] = None,
    *,
    allow_component_outputs: bool = False,
) -> Dict[str, object]:
    """Validate G2.6 identity metadata and its staged files.

    Existing identity prefixes may contain later target-component installs;
    this mode preserves strict hashes for G2 files while allowing validated
    component outputs to coexist with them.
    """
    if not isinstance(manifest, dict):
        raise BuildError("target-inputs", "Target-input manifest must be a JSON object")
    target_backend = manifest.get("target_backend")
    sources = manifest.get("sources")
    interfaces = manifest.get("interfaces")
    files = manifest.get("files")
    materialization_id = manifest.get("materialization_id")
    if (
        not isinstance(target_backend, str)
        or target_backend != "linux-cross-mingw"
        or not isinstance(sources, dict)
        or not isinstance(interfaces, list)
        or not isinstance(files, dict)
        or not isinstance(materialization_id, str)
    ):
        raise BuildError("target-inputs", "Target-input manifest is incomplete")
    identity = {
        "target_backend": target_backend,
        "sources": sources,
        "interfaces": interfaces,
        "files": files,
    }
    expected_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if materialization_id != expected_id:
        raise BuildError("target-inputs", "Target-input materialisation ID does not match content")
    for name, source in sources.items():
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"].lower()) is None
        ):
            raise BuildError("target-inputs", f"Target source identity is incomplete: {name}")
    for relative, digest in files.items():
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not relative.startswith(("include/", "lib/"))
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest.lower()) is None
        ):
            raise BuildError("target-inputs", f"Target-input file identity is invalid: {relative}")
    for interface in interfaces:
        if (
            not isinstance(interface, dict)
            or not isinstance(interface.get("output"), str)
            or not isinstance(interface.get("recipe"), str)
            or not isinstance(interface.get("output_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", interface["output_sha256"].lower()) is None
        ):
            raise BuildError("target-inputs", "Generated interface identity is incomplete")
        if files.get(interface["output"]) != interface["output_sha256"]:
            raise BuildError(
                "target-inputs",
                f"Generated interface hash disagrees with file identity: {interface['output']}",
            )
        if (
            re.fullmatch(
                r"[0-9a-f]{64}", str(interface.get("definition_source_sha256", "")).lower()
            )
            is None
        ):
            raise BuildError(
                "target-inputs", "Generated interface definition-source checksum is invalid"
            )
    if target_prefix is not None:
        _validate_target_tree(target_prefix, allow_component_outputs=allow_component_outputs)
        actual_files = {
            path.relative_to(target_prefix).as_posix(): _sha256(path)
            for path in sorted(target_prefix.rglob("*"))
            if path.is_file() and path.name != "target-inputs.json"
        }
        if allow_component_outputs:
            mismatch = any(
                actual_files.get(relative) != digest for relative, digest in files.items()
            )
        else:
            mismatch = actual_files != files
        if mismatch:
            raise BuildError("target-inputs", "Target-input files do not match manifest hashes")
    return manifest


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
                    message = str(exc)
                    if "missing in local source storage" in message:
                        code = "source_missing"
                    elif "SHA256 mismatch" in message:
                        code = "checksum_mismatch"
                    else:
                        code = "source_acquisition_failed"
                    raise BuildError(
                        name,
                        f"Project source acquisition failed: {exc}",
                        category=CATEGORY_PROJECT_SOURCE,
                        code=code,
                    ) from exc
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
                        for tree in sorted(
                            component.target_headers,
                            key=lambda item: (item.destination, item.source),
                        )
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
            interfaces.sort(
                key=lambda item: (str(item.get("output", "")), str(item.get("dll", "")))
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
        validate_target_inputs_manifest(manifest, staging_root)
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
