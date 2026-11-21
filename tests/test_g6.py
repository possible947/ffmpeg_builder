import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.config import TARGET_BACKEND_CROSS_MINGW
from ffmpeg_builder.release_bundle import _runtime_search_dirs, make_release_bundle


class _Result:
    def __init__(self, stdout: str, success: bool = True):
        self.stdout = stdout
        self.stderr = ""
        self.success = success


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _builder(tmp_path: Path, *, unknown_import: bool = False):
    workspace = tmp_path / "workspace"
    source_bin = workspace / "bin"
    source_bin.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
        (source_bin / name).write_bytes(name.encode())

    toolchain = tmp_path / "llvm-mingw"
    runtime_dir = toolchain / "x86_64-w64-mingw32" / "bin"
    runtime_dir.mkdir(parents=True)
    libcxx = runtime_dir / "libc++.dll"
    libunwind = runtime_dir / "libunwind.dll"
    libcxx.write_bytes(b"accepted libc++")
    libunwind.write_bytes(b"accepted libunwind")

    imports_by_name = {
        "ffmpeg.exe": (
            "DLL Name: KERNEL32.dll\nDLL Name: libc++.dll\nDLL Name: OpenCL.dll\n"
            "DLL name: ffmpeg_g.exe\n"
        ),
        "ffprobe.exe": "DLL Name: KERNEL32.dll\nDLL Name: libc++.dll\n",
        "libc++.dll": "DLL Name: libunwind.dll\nDLL Name: KERNEL32.dll\n",
        "libunwind.dll": "DLL Name: KERNEL32.dll\n",
    }
    if unknown_import:
        imports_by_name["ffmpeg.exe"] += "DLL Name: mystery.dll\n"

    class _Executor:
        def execute(self, command, env=None):
            return _Result(imports_by_name[Path(command[-1]).name])

    builder = SimpleNamespace(
        platform="linux-wsl2",
        workspace=workspace,
        config=SimpleNamespace(
            target_backend=TARGET_BACKEND_CROSS_MINGW,
            ffmpeg_version="9.0",
            windows=SimpleNamespace(msys2_root=str(tmp_path / "legacy-msys")),
        ),
        platform_detector=SimpleNamespace(
            get_build_backend_name=lambda: TARGET_BACKEND_CROSS_MINGW
        ),
        executor=_Executor(),
        platform_context=SimpleNamespace(
            cross_environment=SimpleNamespace(
                backend=TARGET_BACKEND_CROSS_MINGW,
                target_tools=SimpleNamespace(objdump=Path("/target/objdump"), prefix=toolchain),
            )
        ),
        _rmtree=lambda path: shutil.rmtree(path),
        get_build_env=lambda: {},
    )

    policy = {
        "pe_imports": ["KERNEL32.dll", "libc++.dll", "OpenCL.dll"],
        "toolchain_dlls": {
            "libc++.dll": _sha256(libcxx),
            "libunwind.dll": _sha256(libunwind),
        },
        "system_imports": ["KERNEL32.dll"],
        "external_runtime_prerequisites": [
            {
                "name": "OpenCL.dll",
                "classification": "external-runtime-prerequisite",
                "source_identity": "Windows OpenCL dispatcher",
            }
        ],
    }
    return builder, policy, runtime_dir


def test_cross_mingw_bundle_contains_only_accepted_toolchain_runtime(tmp_path):
    builder, policy, _runtime_dir = _builder(tmp_path)

    release = make_release_bundle(builder, policy)

    assert {path.name for path in release.iterdir()} == {
        "ffmpeg.exe",
        "ffprobe.exe",
        "libc++.dll",
        "libunwind.dll",
        "manifest.json",
    }
    manifest = json.loads((release / "manifest.json").read_text())
    assert manifest["platform"] == "windows"
    assert manifest["target_backend"] == TARGET_BACKEND_CROSS_MINGW
    assert manifest["binaries"] == ["ffmpeg.exe", "ffprobe.exe"]
    assert manifest["dependencies"] == ["libc++.dll", "libunwind.dll"]
    assert manifest["missing_binaries"] == []
    assert manifest["missing_dependencies"] == []
    assert manifest["runtime"]["pe_imports"] == [
        "kernel32.dll",
        "libc++.dll",
        "opencl.dll",
    ]
    assert manifest["runtime"]["toolchain_dlls"] == policy["toolchain_dlls"]
    assert manifest["runtime"]["system_imports"] == ["kernel32.dll"]
    assert manifest["runtime"]["external_runtime_prerequisites"][0]["name"] == "OpenCL.dll"
    assert manifest["distribution"]["redistributable"] is False


def test_cross_mingw_bundle_fails_for_unclassified_pe_import(tmp_path):
    builder, policy, _runtime_dir = _builder(tmp_path, unknown_import=True)

    with pytest.raises(BuildError, match="PE imports differ"):
        make_release_bundle(builder, policy)


def test_cross_mingw_runtime_search_uses_llvm_mingw_not_legacy_msys(tmp_path):
    builder, _policy, runtime_dir = _builder(tmp_path)
    legacy = Path(builder.config.windows.msys2_root) / "ucrt64" / "bin"
    legacy.mkdir(parents=True)
    binary = builder.workspace / "bin" / "ffmpeg.exe"

    roots = _runtime_search_dirs(builder, binary)

    assert runtime_dir in roots
    assert legacy not in roots


def test_cross_mingw_packaging_requires_verified_runtime_policy(tmp_path):
    builder, _policy, _runtime_dir = _builder(tmp_path)

    with pytest.raises(BuildError, match="requires verified G4/G5"):
        make_release_bundle(builder)
