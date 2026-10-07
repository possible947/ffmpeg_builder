"""Release bundle creation helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
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

    dependencies, missing_dependencies, dependency_aliases = _collect_runtime_dependencies(
        builder, source_binaries
    )
    copied_dependencies: List[str] = []

    for dep in sorted(dependencies, key=lambda item: item.name.lower()):
        destination = release_dir / dep.name
        if destination.exists():
            continue
        shutil.copy2(dep, destination)
        copied_dependencies.append(str(destination))

    install_name_rewrites: List[str] = []
    linux_runtime_rewrites: List[str] = []
    if builder.platform == "darwin":
        install_name_rewrites = _make_macos_bundle_relocatable(
            builder, release_dir, source_binaries, dependencies
        )
    elif builder.platform == "linux":
        for soname, library in sorted(dependency_aliases.items()):
            target = library.resolve().name
            if soname == target:
                continue
            alias = release_dir / soname
            if alias.exists() or alias.is_symlink():
                if alias.resolve() != release_dir / target:
                    raise BuildError(
                        "release",
                        f"Conflicting runtime dependency alias {soname}: {alias.resolve()} "
                        f"and {target}",
                    )
                continue
            alias.symlink_to(target)
        linux_runtime_rewrites = _make_linux_bundle_relocatable(
            builder,
            release_dir,
            source_binaries,
            dependencies,
            validate_startup=not missing_dependencies,
        )

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "platform": builder.platform,
        "build_backend": backend,
        "ffmpeg_version": builder.config.ffmpeg_version,
        "binaries": copied_binaries,
        "missing_binaries": sorted(missing_binaries),
        "dependencies": copied_dependencies,
        "missing_dependencies": sorted(missing_dependencies),
        "install_name_rewrites": install_name_rewrites,
        "linux_runtime_rewrites": linux_runtime_rewrites,
        "dependency_aliases": {
            alias: library.resolve().name
            for alias, library in sorted(dependency_aliases.items())
            if alias != library.resolve().name
        },
    }
    (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if builder.platform == "linux" and missing_dependencies:
        missing = ", ".join(sorted(missing_dependencies))
        raise BuildError(
            "release",
            f"Release bundle has unresolved runtime dependencies: {missing}. "
            f"See {release_dir / 'manifest.json'}",
        )

    return release_dir


def _rmtree(builder: "FFmpegBuilder", path: Path) -> None:
    builder._rmtree(path)


def _collect_runtime_dependencies(
    builder: "FFmpegBuilder", binaries: List[Path]
) -> Tuple[Set[Path], Set[str], dict[str, Path]]:
    queue = list(binaries)
    visited: Set[str] = set()
    collected: Set[Path] = set()
    collected_keys: Set[str] = set()
    missing: Set[str] = set()
    aliases: dict[str, Path] = {}

    while queue:
        current = queue.pop(0).resolve()
        current_key = _path_key(builder, current)
        if current_key in visited:
            continue
        visited.add(current_key)

        if builder.platform == "linux":
            linux_dependencies = _read_linux_dependency_entries(builder, current)
            dependencies = [(name, path) for name, path in linux_dependencies]
        else:
            dependencies = [(dep, dep) for dep in _read_runtime_dependencies(builder, current)]

        for dep_name, dep in dependencies:
            if builder.platform == "linux" and _is_linux_system_runtime_name(dep_name):
                continue
            if dep is None and builder.platform == "linux":
                dep = dep_name
            elif dep is None:
                missing.add(dep_name)
                continue
            resolved = _resolve_runtime_dependency(builder, dep, current)
            if resolved is None:
                if _is_system_dependency_reference(builder, dep):
                    continue
                missing.add(dep_name if builder.platform == "linux" else dep)
                continue

            resolved = resolved.resolve()
            if builder.platform == "linux":
                previous = aliases.get(dep_name)
                if previous is not None and previous.resolve() != resolved:
                    # A single SONAME can resolve to only one file in this
                    # flat bundle. Keep the first runtime resolution.
                    continue
                aliases.setdefault(dep_name, resolved)
            resolved_key = _path_key(builder, resolved)
            if builder.platform != "linux" and _is_system_runtime_library(builder, resolved):
                visited.add(resolved_key)
                continue

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
    return [
        path
        for _name, path in _read_linux_dependency_entries(builder, binary_path)
        if path is not None
    ]


def _read_linux_dependency_entries(
    builder: "FFmpegBuilder", binary_path: Path
) -> List[Tuple[str, Optional[str]]]:
    result = builder.executor.execute(
        ["ldd", str(binary_path)], env=_get_linux_release_env(builder)
    )
    if not result.success:
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "not a dynamic executable" in combined or "not a valid dynamic program" in combined:
            # Fully static binary (full_static build): ldd exits non-zero
            # with "not a dynamic executable". There is nothing to bundle
            # beyond the binary itself.
            return []
        if "=> not found" not in result.stdout:
            raise BuildError(
                "release",
                f"Failed to inspect dependencies for {binary_path.name}: "
                f"{result.stderr.strip()}",
            )

    dependencies: List[Tuple[str, Optional[str]]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("linux-vdso")
            or ": version `" in line
            or " not found (required by " in line
        ):
            continue
        if "=> not found" in line:
            name = line.split("=>", 1)[0].strip()
            dependencies.append((Path(name).name, None))
            continue
        if "=>" in line:
            name = line.split("=>", 1)[0].strip()
            path = line.split("=>", 1)[1].strip().split(" ", 1)[0].strip()
            if path and path != "not":
                dependencies.append((Path(name).name, path))
            continue
        if line.startswith("/"):
            path = line.split(" ", 1)[0].strip()
            dependencies.append((Path(path).name, path))
    return dependencies


def _is_linux_system_runtime_name(name: str) -> bool:
    """Keep the host's glibc and ELF loader out of the application bundle."""
    basename = Path(name).name
    return basename in {
        "libc.so.6",
        "libm.so.6",
        "libmvec.so.1",
        "libpthread.so.0",
        "libdl.so.2",
        "librt.so.1",
        "libresolv.so.2",
        "libanl.so.1",
        "libutil.so.1",
        "libBrokenLocale.so.1",
    } or basename.startswith(("ld-linux", "ld64.so", "libnss_"))


def _get_linux_release_env(builder: "FFmpegBuilder") -> dict[str, str]:
    """Prevent an inherited Nix or user library path from affecting bundle checks."""
    env = builder.get_build_env()
    env["LD_LIBRARY_PATH"] = "/dev/null"
    return env


def _make_linux_bundle_relocatable(
    builder: "FFmpegBuilder",
    release_dir: Path,
    source_binaries: List[Path],
    dependencies: Set[Path],
    validate_startup: bool = True,
) -> List[str]:
    """Replace Nix ELF paths with the host loader and bundle-local library lookup."""
    env = _get_linux_release_env(builder)
    readelf = shutil.which("readelf", path=env.get("PATH"))
    if not readelf:
        raise BuildError(
            "release",
            "Linux release bundling requires readelf in the build environment.",
        )

    bundled_elfs = [
        *(release_dir / binary.name for binary in source_binaries),
        *(release_dir / dependency.name for dependency in sorted(dependencies)),
    ]
    dynamic_elfs: List[Path] = []
    for path in bundled_elfs:
        dynamic = builder.executor.execute([readelf, "-d", str(path)], env=env)
        if not dynamic.success:
            raise BuildError(
                "release",
                f"Cannot inspect ELF dynamic section for {path.name}: " f"{dynamic.stderr.strip()}",
            )
        if "There is no dynamic section" not in dynamic.stdout:
            dynamic_elfs.append(path)

    if not dynamic_elfs:
        return []

    patchelf = shutil.which("patchelf", path=env.get("PATH"))
    if not patchelf:
        raise BuildError(
            "release",
            "Dynamic Linux release bundling requires patchelf in the build environment.",
        )
    chrpath = shutil.which("chrpath", path=env.get("PATH"))
    if not chrpath:
        raise BuildError(
            "release",
            "Dynamic Linux release bundling requires chrpath in the build environment.",
        )

    system_shell = Path("/bin/sh")
    shell_program = builder.executor.execute([readelf, "-l", str(system_shell)], env=env)
    if not shell_program.success:
        raise BuildError(
            "release",
            f"Cannot inspect the host ELF loader using {system_shell}: "
            f"{shell_program.stderr.strip()}",
        )
    host_interpreter = _parse_elf_interpreter(shell_program.stdout)
    if not host_interpreter or "/nix/store/" in host_interpreter:
        raise BuildError(
            "release",
            f"Could not resolve a non-Nix host ELF interpreter from {system_shell}.",
        )
    if not Path(host_interpreter).is_file():
        raise BuildError(
            "release",
            f"Host ELF interpreter does not exist: {host_interpreter}",
        )

    rewritten: List[str] = []
    for path in dynamic_elfs:
        rewritten.extend(
            _rewrite_linux_elf(
                builder, path, release_dir, readelf, patchelf, chrpath, host_interpreter, env
            )
        )

    if validate_startup:
        for binary in source_binaries:
            bundled_binary = release_dir / binary.name
            result = builder.executor.execute([str(bundled_binary), "-version"], env=env)
            if not result.success:
                raise BuildError(
                    "release",
                    f"Linux release binary {binary.name} failed its startup check: "
                    f"{result.stderr.strip() or result.stdout.strip()}",
                )

    return rewritten


def _rewrite_linux_elf(
    builder: "FFmpegBuilder",
    path: Path,
    release_dir: Path,
    readelf: str,
    patchelf: str,
    chrpath: str,
    host_interpreter: str,
    env: dict[str, str],
) -> List[str]:
    mode = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, mode | stat.S_IWUSR)
    try:
        program_headers = builder.executor.execute([readelf, "-l", str(path)], env=env)
        if not program_headers.success:
            raise BuildError(
                "release",
                f"Cannot inspect ELF program headers for {path.name}: "
                f"{program_headers.stderr.strip()}",
            )
        interpreter = _parse_elf_interpreter(program_headers.stdout)
        rewritten: List[str] = []
        if interpreter:
            result = builder.executor.execute(
                [patchelf, "--set-interpreter", host_interpreter, str(path)], env=env
            )
            if not result.success:
                raise BuildError(
                    "release",
                    f"patchelf could not set the host interpreter for {path.name}: "
                    f"{result.stderr.strip()}",
                )
            rewritten.append(f"{path.name}: interpreter -> {host_interpreter}")

        current_rpath = builder.executor.execute([patchelf, "--print-rpath", str(path)], env=env)
        if not current_rpath.success:
            raise BuildError(
                "release",
                f"patchelf could not inspect runtime paths for {path.name}: "
                f"{current_rpath.stderr.strip()}",
            )
        old_rpath = current_rpath.stdout.strip()
        needs_runtime_path = bool(interpreter or old_rpath)
        if old_rpath and old_rpath != "$ORIGIN":
            result = builder.executor.execute([chrpath, "-r", "$ORIGIN", str(path)], env=env)
            if not result.success:
                raise BuildError(
                    "release",
                    f"chrpath could not replace runtime paths for {path.name}: "
                    f"{result.stderr.strip()}",
                )
        elif not old_rpath and interpreter:
            result = builder.executor.execute(
                [patchelf, "--force-rpath", "--set-rpath", "$ORIGIN", str(path)], env=env
            )
            if not result.success:
                raise BuildError(
                    "release",
                    f"patchelf could not set $ORIGIN runtime lookup for {path.name}: "
                    f"{result.stderr.strip()}",
                )

        needed = builder.executor.execute([patchelf, "--print-needed", str(path)], env=env)
        if not needed.success:
            raise BuildError(
                "release",
                f"patchelf could not inspect dependencies for {path.name}: "
                f"{needed.stderr.strip()}",
            )
        for dependency in needed.stdout.splitlines():
            dependency = dependency.strip()
            if not dependency or (
                "/nix/store/" not in dependency and not dependency.startswith("/")
            ):
                continue
            replacement = Path(dependency).name
            if (
                not _is_linux_system_runtime_name(replacement)
                and not (release_dir / replacement).exists()
            ):
                raise BuildError(
                    "release",
                    f"Cannot rewrite absolute ELF dependency {dependency} for {path.name}: "
                    f"{replacement} is not in the bundle.",
                )
            result = builder.executor.execute(
                [patchelf, "--replace-needed", dependency, replacement, str(path)], env=env
            )
            if not result.success:
                raise BuildError(
                    "release",
                    f"patchelf could not rewrite dependency {dependency} in {path.name}: "
                    f"{result.stderr.strip()}",
                )
        verified_needed = builder.executor.execute([patchelf, "--print-needed", str(path)], env=env)
        if not verified_needed.success or any(
            "/nix/store/" in dependency or dependency.startswith("/")
            for dependency in verified_needed.stdout.splitlines()
        ):
            raise BuildError(
                "release",
                f"Absolute or Nix runtime dependency remains in {path.name}: "
                f"{verified_needed.stdout.strip() or verified_needed.stderr.strip()}",
            )
        verified_rpath = builder.executor.execute([patchelf, "--print-rpath", str(path)], env=env)
        expected_rpath = "$ORIGIN" if needs_runtime_path else ""
        if not verified_rpath.success or verified_rpath.stdout.strip() != expected_rpath:
            raise BuildError(
                "release",
                f"Failed to verify bundle-local runtime lookup for {path.name}: "
                f"{verified_rpath.stdout.strip() or verified_rpath.stderr.strip()}",
            )
        if interpreter:
            verified_headers = builder.executor.execute([readelf, "-l", str(path)], env=env)
            verified_interpreter = _parse_elf_interpreter(verified_headers.stdout)
            if not verified_headers.success or verified_interpreter != host_interpreter:
                raise BuildError(
                    "release",
                    f"ELF interpreter was not rewritten for {path.name}: "
                    f"{verified_interpreter or verified_headers.stderr.strip()}",
                )
        if needs_runtime_path:
            rewritten.append(f"{path.name}: rpath -> $ORIGIN")
        return rewritten
    finally:
        os.chmod(path, mode)


def _parse_elf_interpreter(output: str) -> Optional[str]:
    match = re.search(r"Requesting program interpreter:\s*([^\]]+)", output)
    return match.group(1).strip() if match else None


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
    else:
        candidates.extend(
            [
                Path("/lib"),
                Path("/lib64"),
                Path("/usr/local/lib"),
                Path("/usr/lib"),
                Path("/usr/lib64"),
            ]
        )
        multiarch = getattr(builder.platform_detector, "get_multiarch_dir", lambda: "")()
        if multiarch:
            candidates.extend([Path("/lib") / multiarch, Path("/usr/lib") / multiarch])

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
