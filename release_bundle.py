"""Release bundle creation helpers."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from .build_types import BuildError
from .config import TARGET_BACKEND_CROSS_MINGW

if TYPE_CHECKING:
    from .builder import FFmpegBuilder


OPTIONAL_TOOLS = ("x265", "SvtAv1EncApp")


def make_release_bundle(
    builder: "FFmpegBuilder",
    windows_runtime_policy: Optional[Dict[str, object]] = None,
) -> Path:
    """Create a release directory and collect its project-owned runtime files."""
    backend = builder.platform_detector.get_build_backend_name()
    cross_mingw = getattr(builder.config, "target_backend", "native") == TARGET_BACKEND_CROSS_MINGW
    target_platform = "windows" if cross_mingw else builder.platform
    if cross_mingw and windows_runtime_policy is None:
        raise BuildError(
            "release",
            "linux-cross-mingw packaging requires verified G4/G5 Windows runtime metadata.",
        )
    release_dir = builder.workspace / "release"

    if release_dir.exists():
        _rmtree(builder, release_dir)

    release_dir.mkdir(parents=True, exist_ok=True)

    source_bin = builder.workspace / "bin"
    source_binaries: List[Path] = []
    copied_binaries: List[str] = []
    missing_binaries: List[str] = []

    binary_names = ("ffmpeg", "ffprobe") if cross_mingw else ("ffmpeg", "ffprobe", "ffplay")
    for name in binary_names:
        candidates = [source_bin / name]
        if target_platform == "windows":
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
    if cross_mingw and missing_binaries:
        raise BuildError(
            "release",
            "Accepted Windows bundle binaries are missing: " + ", ".join(sorted(missing_binaries)),
        )

    # Standalone encoder CLIs consumed by downstream packaging. They are
    # optional (not every profile builds them) and go through the same
    # dependency collection / relocation as ffmpeg.
    copied_tools: List[str] = []
    for name in (() if cross_mingw else OPTIONAL_TOOLS):
        candidates = [source_bin / name]
        if target_platform == "windows":
            candidates.insert(0, source_bin / f"{name}.exe")
        source = next((candidate for candidate in candidates if candidate.exists()), None)
        if source is None:
            continue
        destination = release_dir / source.name
        shutil.copy2(source, destination)
        source_binaries.append(source)
        copied_binaries.append(str(destination))
        copied_tools.append(source.name)

    if cross_mingw:
        assert windows_runtime_policy is not None
        dependencies, missing_dependencies, aliases, windows_runtime = (
            _collect_cross_mingw_dependencies(builder, source_binaries, windows_runtime_policy)
        )
    else:
        dependencies, missing_dependencies, aliases = _collect_runtime_dependencies(
            builder, source_binaries
        )
        windows_runtime = None
    copied_dependencies: List[str] = []

    for dep in sorted(dependencies, key=lambda item: item.name.lower()):
        destination = release_dir / dep.name
        if destination.exists():
            continue
        shutil.copy2(dep, destination)
        copied_dependencies.append(str(destination))

    created_aliases: List[str] = []
    if target_platform == "linux":
        created_aliases = _create_dependency_aliases(release_dir, aliases)

    install_name_rewrites: List[str] = []
    if target_platform == "darwin":
        install_name_rewrites = _make_macos_bundle_relocatable(
            builder, release_dir, source_binaries, dependencies
        )
        _verify_macos_signatures(builder, release_dir)

    rpath_rewrites: List[str] = []
    if target_platform == "linux" and dependencies:
        rpath_rewrites = _make_linux_bundle_relocatable(
            builder, release_dir, [Path(p) for p in copied_binaries + copied_dependencies]
        )

    manifest_binaries = (
        [Path(path).name for path in copied_binaries] if cross_mingw else copied_binaries
    )
    manifest_dependencies = (
        [Path(path).name for path in copied_dependencies] if cross_mingw else copied_dependencies
    )
    manifest: Dict[str, object] = {
        "generated_at": datetime.now().isoformat(),
        "platform": target_platform,
        "build_backend": backend,
        "ffmpeg_version": builder.config.ffmpeg_version,
        "binaries": manifest_binaries,
        "missing_binaries": sorted(missing_binaries),
        "tools": copied_tools,
        "dependencies": manifest_dependencies,
        "missing_dependencies": sorted(missing_dependencies),
        "install_name_rewrites": install_name_rewrites,
        "rpath_rewrites": rpath_rewrites,
        "dependency_aliases": created_aliases,
    }
    if cross_mingw:
        manifest["target_backend"] = TARGET_BACKEND_CROSS_MINGW
    if windows_runtime is not None:
        manifest.update(
            {
                "distribution": {
                    "scope": "internal-target-specific",
                    "redistributable": False,
                    "reason": "The accepted FFmpeg GPL/nonfree configuration is unredistributable.",
                },
                "runtime": windows_runtime,
            }
        )
    (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return release_dir


def _collect_cross_mingw_dependencies(
    builder: "FFmpegBuilder",
    binaries: List[Path],
    policy: Dict[str, object],
) -> Tuple[Set[Path], Set[str], Dict[Path, Set[str]], Dict[str, object]]:
    """Collect only the accepted LLVM-MinGW DLL closure for a cross-built bundle."""
    cross = builder.platform_context.cross_environment
    if cross is None or cross.backend != TARGET_BACKEND_CROSS_MINGW:
        raise BuildError("release", "The accepted linux-cross-mingw toolchain is unavailable.")

    raw_toolchain = policy.get("toolchain_dlls")
    raw_system = policy.get("system_imports")
    raw_imports = policy.get("pe_imports")
    raw_external = policy.get("external_runtime_prerequisites")
    if (
        not isinstance(raw_toolchain, dict)
        or not all(
            isinstance(name, str) and isinstance(digest, str)
            for name, digest in raw_toolchain.items()
        )
        or not isinstance(raw_system, list)
        or not all(isinstance(name, str) for name in raw_system)
        or not isinstance(raw_imports, list)
        or not all(isinstance(name, str) for name in raw_imports)
        or not isinstance(raw_external, list)
        or not all(
            isinstance(item, dict)
            and all(
                isinstance(item.get(key), str) and item[key]
                for key in ("name", "classification", "source_identity")
            )
            for item in raw_external
        )
    ):
        raise BuildError("release", "Verified Windows runtime metadata is malformed.")

    approved = {name.lower(): digest for name, digest in raw_toolchain.items()}
    system = {name.lower() for name in raw_system}
    external = {
        str(item.get("name", "")).lower(): item
        for item in raw_external
        if isinstance(item.get("name"), str)
    }
    expected_imports = {name.lower() for name in raw_imports}
    runtime_dir = cross.target_tools.prefix / "x86_64-w64-mingw32" / "bin"
    available = {path.name.lower(): path for path in runtime_dir.glob("*.dll")}
    if len(available) != len(list(runtime_dir.glob("*.dll"))):
        raise BuildError("release", "Ambiguous DLL names in the accepted LLVM-MinGW runtime.")

    def imports(path: Path) -> Set[str]:
        result = builder.executor.execute(
            [str(cross.target_tools.objdump), "-p", str(path)], env=builder.get_build_env()
        )
        if not result.success:
            raise BuildError(
                "release",
                f"Failed to inspect dependencies for {path.name}: {result.stderr.strip()}",
            )
        names = {
            match.group(1).lower()
            for line in result.stdout.splitlines()
            if (match := re.search(r"DLL Name:\s*(\S+)", line))
        }
        return names

    roots: Set[str] = set()
    for binary in binaries:
        roots.update(imports(binary))
    if roots != expected_imports:
        raise BuildError(
            "release",
            "FFmpeg PE imports differ from the accepted G4 manifest: "
            f"unexpected={sorted(roots - expected_imports)}, "
            f"missing={sorted(expected_imports - roots)}",
        )

    pending = list(roots)
    visited: Set[str] = set()
    copied: Set[str] = set()
    dependencies: Set[Path] = set()
    missing: Set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if name in approved:
            source = available.get(name)
            if source is None:
                raise BuildError("release", f"Accepted LLVM-MinGW runtime DLL is missing: {name}")
            if _file_digest(source) != approved[name]:
                raise BuildError("release", f"Accepted LLVM-MinGW runtime DLL changed: {name}")
            dependencies.add(source)
            copied.add(name)
            pending.extend(imports(source))
        elif name not in system and name not in external:
            missing.add(name)

    if copied != set(approved):
        raise BuildError(
            "release",
            "The Windows runtime closure differs from G5: "
            f"unused={sorted(set(approved) - copied)}, unexpected={sorted(copied - set(approved))}",
        )
    if missing:
        raise BuildError(
            "release",
            "Unclassified Windows runtime imports: " + ", ".join(sorted(missing)),
        )

    runtime: Dict[str, object] = {
        "pe_imports": sorted(roots),
        "toolchain_dlls": {name: approved[name] for name in sorted(copied)},
        "system_imports": sorted(system - set(external)),
        "external_runtime_prerequisites": [external[name] for name in sorted(external)],
    }
    return dependencies, set(), {}, runtime


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _rmtree(builder: "FFmpegBuilder", path: Path) -> None:
    builder._rmtree(path)


def _create_dependency_aliases(release_dir: Path, aliases: Dict[Path, Set[str]]) -> List[str]:
    """Expose bundled libraries under the name the dynamic loader requests.

    Dependencies are copied under their symlink-resolved name
    (``libxvidcore.so.4.3``) while binaries reference the soname
    (``libxvidcore.so.4``), so the soname must exist in the bundle too.
    Falls back to a copy where symlinks are not permitted.
    """
    created: List[str] = []
    for real in sorted(aliases, key=lambda item: item.name):
        for alias in sorted(aliases[real]):
            target = release_dir / real.name
            link = release_dir / alias
            if link.exists() or link.is_symlink() or not target.exists():
                continue
            try:
                link.symlink_to(real.name)
            except OSError:
                shutil.copy2(target, link)
            created.append(f"{alias} -> {real.name}")
    return created


def _collect_runtime_dependencies(
    builder: "FFmpegBuilder", binaries: List[Path]
) -> Tuple[Set[Path], Set[str], Dict[Path, Set[str]]]:
    queue = list(binaries)
    visited: Set[str] = set()
    collected: Set[Path] = set()
    collected_keys: Set[str] = set()
    missing: Set[str] = set()
    aliases: Dict[Path, Set[str]] = {}

    while queue:
        current = queue.pop(0).resolve()
        current_key = _path_key(builder, current)
        if current_key in visited:
            continue
        visited.add(current_key)

        for dep in _read_runtime_dependencies(builder, current):
            resolved = _resolve_runtime_dependency(builder, dep, current)
            if resolved is None:
                if _is_system_dependency_reference(builder, dep):
                    continue
                missing.add(dep)
                continue

            requested_name = Path(dep).name
            resolved = resolved.resolve()
            resolved_key = _path_key(builder, resolved)
            if _is_system_runtime_library(builder, resolved):
                visited.add(resolved_key)
                continue

            if requested_name and requested_name != resolved.name:
                aliases.setdefault(resolved, set()).add(requested_name)

            if resolved_key in collected_keys:
                continue

            collected.add(resolved)
            collected_keys.add(resolved_key)
            queue.append(resolved)

    return collected, missing, aliases


def _read_runtime_dependencies(builder: "FFmpegBuilder", binary_path: Path) -> List[str]:
    if builder.platform == "windows":
        return _read_windows_dependencies(builder, binary_path)
    if builder.platform == "darwin":
        return _read_macos_dependencies(builder, binary_path)
    return _read_linux_dependencies(builder, binary_path)


def _read_windows_dependencies(builder: "FFmpegBuilder", binary_path: Path) -> List[str]:
    result = builder.executor.execute(
        ["objdump", "-p", str(binary_path)], env=builder.get_build_env()
    )
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
    return _read_macos_install_names(builder, binary_path)[1]


def _read_macos_install_names(builder: "FFmpegBuilder", binary_path: Path) -> Tuple[str, List[str]]:
    """Return (header line, indented entries) from `otool -L` output.

    The header is the path passed to otool; for a dylib the first
    indented entry is its own install name (id) and the rest are
    dependencies, while for an executable all indented entries are
    dependencies.
    """
    result = builder.executor.execute(
        ["otool", "-L", str(binary_path)], env=builder.get_build_env()
    )
    if not result.success:
        raise BuildError(
            "release",
            f"Failed to inspect dependencies for {binary_path.name}: {result.stderr.strip()}",
        )

    header = ""
    entries: List[str] = []
    for raw_line in result.stdout.splitlines():
        if not raw_line.strip():
            continue
        if not raw_line[0].isspace():
            header = raw_line.strip().rstrip(":").strip()
            continue
        entry = raw_line.strip().split(" (", 1)[0].strip()
        if entry:
            entries.append(entry)
    return header, entries


def _read_macos_rpaths(builder: "FFmpegBuilder", binary_path: Path) -> List[str]:
    result = builder.executor.execute(
        ["otool", "-l", str(binary_path)], env=builder.get_build_env()
    )
    if not result.success:
        raise BuildError(
            "release",
            f"Failed to inspect rpaths for {binary_path.name}: {result.stderr.strip()}",
        )

    rpaths: List[str] = []
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if "LC_RPATH" not in line:
            continue
        for follow in lines[index + 1 : index + 3]:
            stripped = follow.strip()
            if stripped.startswith("path "):
                rpaths.append(stripped.split("path", 1)[1].split(" (", 1)[0].strip())
                break
    return rpaths


def _run_install_name_tool(builder: "FFmpegBuilder", macho: Path, args: List[str]) -> None:
    result = builder.executor.execute(
        ["install_name_tool", *args, str(macho)], env=builder.get_build_env()
    )
    if not result.success:
        raise BuildError(
            "release",
            f"install_name_tool {' '.join(args)} failed for {macho.name}: "
            f"{result.stderr.strip()}",
        )


def _ad_hoc_sign(builder: "FFmpegBuilder", macho: Path) -> None:
    """Re-sign a modified Mach-O ad-hoc (Apple Silicon refuses to load a
    Mach-O whose signature was invalidated by install_name_tool)."""
    result = builder.executor.execute(
        ["codesign", "--force", "-s", "-", str(macho)], env=builder.get_build_env()
    )
    if not result.success:
        raise BuildError(
            "release",
            f"codesign failed for {macho.name}: {result.stderr.strip()}",
        )


def _verify_macos_signatures(builder: "FFmpegBuilder", release_dir: Path) -> None:
    """Fail the release if any bundled Mach-O has a missing or invalid signature."""
    for path in sorted(release_dir.iterdir()):
        if not path.is_file() or path.name == "manifest.json":
            continue
        result = builder.executor.execute(
            ["codesign", "--verify", "--strict", str(path)], env=builder.get_build_env()
        )
        if not result.success:
            raise BuildError(
                "release",
                f"signature verification failed for {path.name}: {result.stderr.strip()}",
            )


def _make_macos_bundle_relocatable(
    builder: "FFmpegBuilder",
    release_dir: Path,
    source_binaries: List[Path],
    dependencies: Set[Path],
) -> List[str]:
    """Rewrite install names and rpaths so the bundle runs from any location.

    References to bundled dylibs are rewritten to @rpath/<name> (only
    references that resolve to a file actually copied into the bundle are
    touched, so system libraries keep their original references), each
    bundled dylib's own install name is set to @rpath/<name>, @loader_path
    is added as an rpath to every Mach-O file, and each modified file is
    re-signed ad-hoc. Returns a human-readable list of the rewrites.
    """
    bundled = {path.resolve() for path in dependencies}
    bundled.update(path.resolve() for path in source_binaries)
    binary_names = {path.name for path in source_binaries}
    rewrites: List[str] = []

    macho_files = [
        path
        for path in sorted(release_dir.iterdir())
        if path.is_file() and path.name != "manifest.json"
    ]

    for macho in macho_files:
        _header, entries = _read_macos_install_names(builder, macho)
        is_dylib = macho.name not in binary_names
        if is_dylib and entries:
            own_id, referenced = entries[0], entries[1:]
        else:
            own_id, referenced = "", entries

        for dep in referenced:
            resolved = _resolve_runtime_dependency(builder, dep, macho)
            if resolved is None:
                continue
            # The bundle copies files under their resolved (symlink-followed)
            # names, so @rpath targets must use the resolved name too —
            # e.g. a reference to /opt/local/lib/libxcb.1.dylib (a symlink)
            # must target the copied file libxcb.1.1.0.dylib.
            real = resolved.resolve()
            if real not in bundled:
                continue
            if real.name == macho.name:
                # The dylib's own install name (id); handled by -id below.
                continue
            target = f"@rpath/{real.name}"
            if dep == target:
                continue
            _run_install_name_tool(builder, macho, ["-change", dep, target])
            rewrites.append(f"{macho.name}: {dep} -> {target}")

        if macho.name not in binary_names:
            target_id = f"@rpath/{macho.name}"
            if own_id != target_id:
                _run_install_name_tool(builder, macho, ["-id", target_id])
                rewrites.append(f"{macho.name}: id -> {target_id}")

        if "@loader_path" not in _read_macos_rpaths(builder, macho):
            _run_install_name_tool(builder, macho, ["-add_rpath", "@loader_path"])

        _ad_hoc_sign(builder, macho)

    return rewrites


def _make_linux_bundle_relocatable(
    builder: "FFmpegBuilder", release_dir: Path, elf_files: List[Path]
) -> List[str]:
    """Make the bundle self-contained: every ELF file gets RUNPATH=$ORIGIN.

    Bundled libraries are collected through ``ldd`` under the build
    environment, which may expose toolchain runtimes (e.g. Intel oneAPI
    libiomp5/libsvml/libirc/libintlc) only via LD_LIBRARY_PATH. Setting the
    RUNPATH on *each* file (not just the executables) is required because
    RUNPATH is not inherited by a dependency's own NEEDED lookups
    (libsvml.so -> libintlc.so.5). The result is then verified with a
    scrubbed loader environment so nothing resolves outside the bundle.
    """
    patchelf = shutil.which("patchelf") or shutil.which(
        "patchelf", path=str(Path(sys.executable).parent)
    )
    if patchelf is None:
        raise BuildError(
            "release",
            "patchelf is required to make the release bundle relocatable "
            "(pip install patchelf, or install the distro package)",
        )

    rewrites: List[str] = []
    for elf in elf_files:
        result = builder.executor.execute([patchelf, "--set-rpath", "$ORIGIN", str(elf)])
        if not result.success:
            raise BuildError(
                "release",
                f"patchelf failed for {elf.name}: {result.stderr.strip()}",
            )
        rewrites.append(f"{elf.name}: RUNPATH=$ORIGIN")

    _verify_linux_bundle_closure(builder, release_dir, elf_files)
    return rewrites


def _verify_linux_bundle_closure(
    builder: "FFmpegBuilder", release_dir: Path, elf_files: List[Path]
) -> None:
    """Fail if any bundled ELF resolves a bundled-toolchain library outside
    the release directory, or has an absolute RPATH/RUNPATH entry."""
    clean_env = {"LD_LIBRARY_PATH": "", "LD_PRELOAD": ""}
    bundled = {p.name for p in release_dir.iterdir() if p.is_file()}
    problems: List[str] = []

    for elf in elf_files:
        dyn = builder.executor.execute(["readelf", "-d", str(elf)], env=clean_env)
        for line in dyn.stdout.splitlines():
            if "(RPATH)" in line or "(RUNPATH)" in line:
                value = line.split("[", 1)[-1].rstrip("]").strip()
                if any(part.startswith("/") for part in value.split(":") if part):
                    problems.append(f"{elf.name}: absolute search path {value}")

        result = builder.executor.execute(["ldd", str(elf)], env=clean_env)
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if "=>" not in line:
                continue
            name, target = (part.strip() for part in line.split("=>", 1))
            if target.startswith("not found"):
                problems.append(f"{elf.name}: {name} not found")
            elif name in bundled and not target.startswith(str(release_dir.resolve())):
                problems.append(f"{elf.name}: {name} resolved outside bundle ({target})")

    if problems:
        raise BuildError(
            "release",
            "Release bundle is not self-contained:\n  " + "\n  ".join(problems),
        )


def _prefer_real_library_path(path: Path) -> Path:
    """Prefer the real versioned dylib when a symlink alias has been copied
    to disk (e.g. Windows without symlink privileges)."""
    resolved = path.resolve()
    if path.name != resolved.name:
        return resolved

    if path.suffix != ".dylib" or not path.parent.exists():
        return path

    stem = path.stem
    for sibling in path.parent.iterdir():
        if not sibling.is_file() or sibling.name == path.name:
            continue
        if sibling.suffix != ".dylib":
            continue
        if not sibling.name.startswith(stem + "."):
            continue
        try:
            if path.read_bytes() == sibling.read_bytes():
                return sibling.resolve()
        except OSError:
            continue
    return path


def _resolve_runtime_dependency(
    builder: "FFmpegBuilder", dep: str, binary_path: Path
) -> Optional[Path]:
    if dep.startswith("@"):
        return _resolve_macos_dynamic_path(builder, dep, binary_path)

    dep_path = Path(dep)
    if dep_path.is_absolute() and dep_path.exists():
        return _prefer_real_library_path(dep_path)

    if dep_path.parts and not dep_path.is_absolute():
        candidate = (binary_path.parent / dep_path).resolve()
        if candidate.exists():
            return _prefer_real_library_path(candidate)

    dep_name = dep_path.name if dep_path.name else dep
    for root in _runtime_search_dirs(builder, binary_path):
        candidate = root / dep_name
        if candidate.exists():
            return _prefer_real_library_path(candidate)

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

    if getattr(builder.config, "target_backend", "native") == TARGET_BACKEND_CROSS_MINGW:
        cross = builder.platform_context.cross_environment
        if cross is not None:
            candidates.append(cross.target_tools.prefix / "x86_64-w64-mingw32" / "bin")
    elif builder.platform == "windows":
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


def _is_system_dependency_reference(builder: "FFmpegBuilder", dep: str) -> bool:
    """True for references dyld resolves from the system even when the
    literal path does not exist on disk (dyld shared cache: /usr/lib
    libraries and /System/Library frameworks on macOS)."""
    if builder.platform != "darwin":
        return False
    return dep.startswith("/System/Library/") or dep.startswith("/usr/lib/")


def _is_system_runtime_library(builder: "FFmpegBuilder", lib_path: Path) -> bool:
    path = lib_path.resolve()
    workspace = builder.workspace.resolve()
    if _is_under(path, workspace):
        return False

    if builder.platform == "windows":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return _is_under(path, windir)

    if builder.platform == "linux":
        return any(
            _is_under(path, Path(prefix)) for prefix in ("/lib", "/lib64", "/usr/lib", "/usr/lib64")
        )

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
