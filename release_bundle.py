"""Release bundle creation helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from .build_types import BuildError

if TYPE_CHECKING:
    from .builder import FFmpegBuilder


def make_release_bundle(builder: "FFmpegBuilder") -> Path:
    """Create a redistributable release directory for built FFmpeg binaries."""
    backend = builder.platform_detector.get_build_backend_name()
    release_dir = builder.workspace / "release"

    if release_dir.exists():
        _rmtree(builder, release_dir)

    release_dir.mkdir(parents=True, exist_ok=True)

    source_bin = builder.workspace / "bin"
    source_binaries: List[Path] = []
    copied_binaries: List[str] = []
    missing_binaries: List[str] = []

    for name in ("ffmpeg", "ffprobe", "ffplay"):
        candidates = [source_bin / name]
        if builder.platform == "windows":
            candidates.insert(0, source_bin / f"{name}.exe")

        source = next((candidate for candidate in candidates if candidate.exists()), None)
        if source is None:
            missing_binaries.append(name)
            continue

        destination = release_dir / source.name
        shutil.copy2(source, destination)
        source_binaries.append(source)
        copied_binaries.append(str(destination))

    if not source_binaries:
        raise BuildError("release", f"No FFmpeg binaries found in {source_bin}")

    dependencies, missing_dependencies = _collect_runtime_dependencies(builder, source_binaries)
    copied_dependencies: List[str] = []

    for dep in sorted(dependencies, key=lambda item: item.name.lower()):
        destination = release_dir / dep.name
        if destination.exists():
            continue
        shutil.copy2(dep, destination)
        copied_dependencies.append(str(destination))

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "platform": builder.platform,
        "build_backend": backend,
        "ffmpeg_version": builder.config.ffmpeg_version,
        "binaries": copied_binaries,
        "missing_binaries": sorted(missing_binaries),
        "dependencies": copied_dependencies,
        "missing_dependencies": sorted(missing_dependencies),
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return release_dir


def _rmtree(builder: "FFmpegBuilder", path: Path) -> None:
    builder._rmtree(path)


def _collect_runtime_dependencies(
    builder: "FFmpegBuilder", binaries: List[Path]
) -> Tuple[Set[Path], Set[str]]:
    queue = list(binaries)
    visited: Set[str] = set()
    collected: Set[Path] = set()
    collected_keys: Set[str] = set()
    missing: Set[str] = set()

    while queue:
        current = queue.pop(0).resolve()
        current_key = _path_key(builder, current)
        if current_key in visited:
            continue
        visited.add(current_key)

        for dep in _read_runtime_dependencies(builder, current):
            resolved = _resolve_runtime_dependency(builder, dep, current)
            if resolved is None:
                missing.add(dep)
                continue

            resolved = resolved.resolve()
            resolved_key = _path_key(builder, resolved)
            if _is_system_runtime_library(builder, resolved):
                visited.add(resolved_key)
                continue

            if resolved_key in collected_keys:
                continue

            collected.add(resolved)
            collected_keys.add(resolved_key)
            queue.append(resolved)

    return collected, missing


def _read_runtime_dependencies(builder: "FFmpegBuilder", binary_path: Path) -> List[str]:
    if builder.platform == "windows":
        return _read_windows_dependencies(builder, binary_path)
    if builder.platform == "darwin":
        return _read_macos_dependencies(builder, binary_path)
    return _read_linux_dependencies(builder, binary_path)


def _read_windows_dependencies(builder: "FFmpegBuilder", binary_path: Path) -> List[str]:
    result = builder.executor.execute(["objdump", "-p", str(binary_path)], env=builder.get_build_env())
    if not result.success:
        raise BuildError(
            "release",
            f"Failed to inspect dependencies for {binary_path.name}: {result.stderr.strip()}",
        )

    dependencies: List[str] = []
    for line in result.stdout.splitlines():
        match = re.search(r"DLL Name:\s*(\S+)", line)
        if match:
            dependencies.append(match.group(1).strip())
    return dependencies


def _read_linux_dependencies(builder: "FFmpegBuilder", binary_path: Path) -> List[str]:
    result = builder.executor.execute(["ldd", str(binary_path)], env=builder.get_build_env())
    if not result.success:
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "not a dynamic executable" in combined or "not a valid dynamic program" in combined:
            # Fully static binary (full_static build): ldd exits non-zero
            # with "not a dynamic executable". There is nothing to bundle
            # beyond the binary itself.
            return []
        raise BuildError(
            "release",
            f"Failed to inspect dependencies for {binary_path.name}: {result.stderr.strip()}",
        )

    dependencies: List[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        if "=> not found" in line:
            dependencies.append(line.split("=>", 1)[0].strip())
            continue
        if "=>" in line:
            path = line.split("=>", 1)[1].strip().split(" ", 1)[0].strip()
            if path and path != "not":
                dependencies.append(path)
            continue
        if line.startswith("/"):
            dependencies.append(line.split(" ", 1)[0].strip())
    return dependencies


def _read_macos_dependencies(builder: "FFmpegBuilder", binary_path: Path) -> List[str]:
    result = builder.executor.execute(["otool", "-L", str(binary_path)], env=builder.get_build_env())
    if not result.success:
        raise BuildError(
            "release",
            f"Failed to inspect dependencies for {binary_path.name}: {result.stderr.strip()}",
        )

    dependencies: List[str] = []
    for index, raw_line in enumerate(result.stdout.splitlines()):
        if index == 0:
            continue
        dep = raw_line.strip().split(" (", 1)[0].strip()
        if dep:
            dependencies.append(dep)
    return dependencies


def _resolve_runtime_dependency(
    builder: "FFmpegBuilder", dep: str, binary_path: Path
) -> Optional[Path]:
    if dep.startswith("@"):
        return _resolve_macos_dynamic_path(builder, dep, binary_path)

    dep_path = Path(dep)
    if dep_path.is_absolute() and dep_path.exists():
        return dep_path

    if dep_path.parts and not dep_path.is_absolute():
        candidate = (binary_path.parent / dep_path).resolve()
        if candidate.exists():
            return candidate

    dep_name = dep_path.name if dep_path.name else dep
    for root in _runtime_search_dirs(builder, binary_path):
        candidate = root / dep_name
        if candidate.exists():
            return candidate

    return None


def _resolve_macos_dynamic_path(
    builder: "FFmpegBuilder", dep: str, binary_path: Path
) -> Optional[Path]:
    if dep.startswith("@loader_path/"):
        candidate = binary_path.parent / dep[len("@loader_path/") :]
        if candidate.exists():
            return candidate

    if dep.startswith("@executable_path/"):
        candidate = builder.workspace / "bin" / dep[len("@executable_path/") :]
        if candidate.exists():
            return candidate

    if dep.startswith("@rpath/"):
        rel = dep[len("@rpath/") :]
        for root in _runtime_search_dirs(builder, binary_path):
            candidate = root / rel
            if candidate.exists():
                return candidate

    return None


def _runtime_search_dirs(builder: "FFmpegBuilder", binary_path: Path) -> List[Path]:
    candidates = [
        binary_path.parent,
        builder.workspace / "bin",
        builder.workspace / "lib",
        builder.workspace / "lib64",
    ]

    if builder.platform == "windows":
        msys_root = Path(builder.config.windows.msys2_root)
        windows_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
        candidates.extend(
            [
                msys_root / "ucrt64" / "bin",
                msys_root / "usr" / "bin",
                windows_root / "System32",
                windows_root / "SysWOW64",
            ]
        )
    elif builder.platform == "darwin":
        candidates.extend([Path("/opt/local/lib"), Path("/usr/local/lib")])

    unique: List[Path] = []
    seen: Set[str] = set()
    for path in candidates:
        key = _path_key(builder, path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            unique.append(path)
    return unique


def _is_system_runtime_library(builder: "FFmpegBuilder", lib_path: Path) -> bool:
    path = lib_path.resolve()
    workspace = builder.workspace.resolve()
    if _is_under(path, workspace):
        return False

    if builder.platform == "windows":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return _is_under(path, windir)

    if builder.platform == "linux":
        return any(_is_under(path, Path(prefix)) for prefix in ("/lib", "/lib64", "/usr/lib", "/usr/lib64"))

    if builder.platform == "darwin":
        return _is_under(path, Path("/usr/lib")) or _is_under(path, Path("/System/Library"))

    return False


def _is_under(child: Path, parent: Path) -> bool:
    child_norm = str(child.resolve()).replace("\\", "/").rstrip("/").lower()
    parent_norm = str(parent.resolve()).replace("\\", "/").rstrip("/").lower()
    return child_norm == parent_norm or child_norm.startswith(f"{parent_norm}/")


def _path_key(builder: "FFmpegBuilder", path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/")
    return normalized.lower() if builder.platform == "windows" else normalized
