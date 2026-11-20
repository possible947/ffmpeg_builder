import hashlib
import json
import copy
import tarfile
from pathlib import Path

import pytest
import yaml

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.target_inputs import (
    _validate_target_tree,
    materialize_target_inputs,
    validate_target_inputs_manifest,
)


def _archive(root: Path, archive: Path) -> str:
    with tarfile.open(archive, "w:gz") as output:
        for path in sorted(root.rglob("*")):
            output.add(path, arcname=path.relative_to(root.parent))
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def _source(root: Path, name: str, files: dict[str, str]) -> Path:
    source = root / name
    for relative, contents in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return source


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def test_target_input_materialisation_uses_pinned_sources_and_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_mirror = tmp_path / "sources"
    source_mirror.mkdir()
    archives = [
        (
            "nv-codec",
            "1",
            {
                "include/ffnvcodec/nvEncodeAPI.h": "#define NVENC_VERSION 1\n",
                "include/ffnvcodec/dynlink_cuda.h": "#define CUDA_HEADER 1\n",
            },
        ),
        (
            "vulkan-headers",
            "1",
            {
                "include/vulkan/vulkan.h": "#define VK_HEADER_VERSION 1\n",
                "registry/vk.xml": (
                    "<registry><commands><command><proto>"
                    "<name>vkGetInstanceProcAddr</name></proto></command></commands></registry>"
                ),
            },
        ),
        (
            "opencl-headers",
            "1",
            {"include/CL/cl.h": "#define CL_TARGET_OPENCL_VERSION 300\n"},
        ),
        (
            "opencl-icd-loader",
            "1",
            {"loader/windows/OpenCL.def": ("LIBRARY OpenCL.dll\nEXPORTS\n  clGetPlatformIDs\n")},
        ),
    ]
    component_data = []
    for name, version, files in archives:
        filename = f"{name}-{version}.tar.gz"
        tree = _source(tmp_path / "trees", f"{name}-{version}", files)
        checksum = _archive(tree, source_mirror / filename)
        item = {
            "name": name,
            "version": version,
            "url": f"https://example.invalid/{filename}",
            "archive_filename": filename,
            "sha256": checksum,
            "category": "hw_accel",
            "build_system": "headers_only",
        }
        if name != "opencl-icd-loader":
            item["target_backends"] = ["linux-cross-mingw"]
            item["target_headers"] = [{"source": "include", "destination": "include"}]
        if name == "vulkan-headers":
            item["target_import_interface"] = {
                "dll": "vulkan-1.dll",
                "definition_source_component": name,
                "definition_path": "registry/vk.xml",
                "definition_format": "vulkan_registry",
                "symbols": ["vkGetInstanceProcAddr"],
                "output": "lib/libvulkan.a",
            }
        if name == "opencl-headers":
            item["target_import_interface"] = {
                "dll": "OpenCL.dll",
                "definition_source_component": "opencl-icd-loader",
                "definition_path": "loader/windows/OpenCL.def",
                "output": "lib/libOpenCL.a",
            }
        component_data.append(item)

    registry_path = tmp_path / "components.yaml"
    registry_path.write_text(yaml.safe_dump(component_data), encoding="utf-8")
    registry = ComponentRegistry(yaml_path=registry_path)

    fake_host_headers = tmp_path / "ambient-host-headers" / "CL"
    fake_host_headers.mkdir(parents=True)
    (fake_host_headers / "cl.h").write_text("HOST HEADER MUST NOT BE USED\n", encoding="utf-8")
    monkeypatch.setenv("CPATH", str(fake_host_headers.parent))

    toolchain = tmp_path / "llvm-mingw-20261104"
    _write_executable(
        toolchain / "bin" / "x86_64-w64-mingw32-dlltool",
        """#!/usr/bin/env python3
import pathlib, sys
output = pathlib.Path(sys.argv[sys.argv.index("-l") + 1])
output.write_bytes(b"!<arch>\\nwindows-coff-import-library")
""",
    )
    _write_executable(
        toolchain / "bin" / "x86_64-w64-mingw32-objdump",
        "#!/bin/sh\nprintf 'architecture: pe-x86-64\\n'\n",
    )

    prefix = tmp_path / "target-prefix"
    manifest = materialize_target_inputs(
        registry=registry,
        target_backend="linux-cross-mingw",
        source_archives_dir=source_mirror,
        packages_dir=tmp_path / "packages",
        target_prefix=prefix,
        toolchain_root=toolchain,
    )

    assert (prefix / "include/ffnvcodec/nvEncodeAPI.h").is_file()
    assert (prefix / "include/vulkan/vulkan.h").is_file()
    assert (prefix / "include/CL/cl.h").read_text() == "#define CL_TARGET_OPENCL_VERSION 300\n"
    assert (prefix / "lib/libOpenCL.a").is_file()
    assert (prefix / "lib/libvulkan.a").is_file()
    assert sorted(path.name for path in (prefix / "lib").iterdir()) == [
        "libOpenCL.a",
        "libvulkan.a",
    ]
    assert not list(prefix.rglob("*.so"))
    assert not list(prefix.rglob("*.dll"))
    assert manifest["target_backend"] == "linux-cross-mingw"
    assert manifest["materialization_id"]
    assert set(manifest["sources"]) == {"nv-codec", "vulkan-headers", "opencl-headers"}
    assert {interface["dll"] for interface in manifest["interfaces"]} == {
        "OpenCL.dll",
        "vulkan-1.dll",
    }
    assert (
        json.loads((prefix / "target-inputs.json").read_text())["materialization_id"]
        == manifest["materialization_id"]
    )
    validate_target_inputs_manifest(manifest, prefix)

    tampered_id = copy.deepcopy(manifest)
    tampered_id["materialization_id"] = "0" * 64
    with pytest.raises(BuildError, match="ID does not match"):
        validate_target_inputs_manifest(tampered_id)

    tampered_interface = copy.deepcopy(manifest)
    tampered_interface["interfaces"][0]["output_sha256"] = "0" * 64
    unsigned = {
        key: tampered_interface[key] for key in ("target_backend", "sources", "interfaces", "files")
    }
    tampered_interface["materialization_id"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with pytest.raises(BuildError, match="Generated interface hash disagrees"):
        validate_target_inputs_manifest(tampered_interface)

    (prefix / "include" / "CL" / "cl.h").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(BuildError, match="do not match manifest hashes"):
        validate_target_inputs_manifest(manifest, prefix)


def test_target_prefix_contamination_is_rejected(tmp_path: Path):
    prefix = tmp_path / "target-prefix"
    (prefix / "lib").mkdir(parents=True)
    (prefix / "lib" / "libhost.so").write_bytes(b"not an ELF")
    with pytest.raises(BuildError, match="Host/runtime artifact"):
        _validate_target_tree(prefix)


@pytest.mark.parametrize(
    ("source_content", "expected_code"),
    [(None, "source_missing"), (b"corrupt archive", "checksum_mismatch")],
)
def test_target_source_failures_are_classified_and_leave_no_prefix(
    tmp_path: Path, source_content: bytes | None, expected_code: str
):
    registry = ComponentRegistry()
    component = registry.get_target_inputs("linux-cross-mingw")[0]
    source_mirror = tmp_path / "sources"
    source_mirror.mkdir()
    if source_content is not None:
        (source_mirror / component.get_archive_filename()).write_bytes(source_content)
    target_prefix = tmp_path / "target-prefix"

    with pytest.raises(BuildError) as error:
        materialize_target_inputs(
            registry,
            "linux-cross-mingw",
            source_mirror,
            tmp_path / "packages",
            target_prefix,
            tmp_path / "llvm-mingw",
            allow_network_downloads=False,
        )

    assert error.value.category == "project_source"
    assert error.value.code == expected_code
    assert not target_prefix.exists()
    assert not list(tmp_path.glob(".target-prefix.target-inputs-*"))


def test_target_stage_refuses_existing_or_system_prefix(tmp_path: Path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BuildError, match="already exists"):
        materialize_target_inputs(
            ComponentRegistry(),
            "linux-cross-mingw",
            tmp_path,
            tmp_path / "packages",
            existing,
        )

    with pytest.raises(BuildError, match="system directory"):
        materialize_target_inputs(
            ComponentRegistry(),
            "linux-cross-mingw",
            tmp_path,
            tmp_path / "packages",
            Path("/opt/ffmpeg-target-test"),
            toolchain_root=tmp_path / "toolchain",
        )
