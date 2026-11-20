"""Bounded G4 Wave-2 cross-builds and primary-profile OpenMP acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import BuildRole, Component, ComponentRegistry
from .config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .cross_environment import CPU_TUNING_FLAGS, CrossEnvironment
from .g4_wave1 import Wave1TargetValidator
from .state import ComponentStatus

G4_WAVE2_COMPONENTS = (
    "gettext",
    "pcre",
    "xvidcore",
    "opencore",
    "lame",
    "fdk_aac",
    "soxr",
    "libtiff",
    "lcms2",
    "libwebp",
    "libsdl",
)

_ACCEPTANCE_MANIFEST = "g4-wave2-acceptance.json"
_OPENMP_MANIFEST = "g4-wave2-openmp-acceptance.json"


@dataclass(frozen=True)
class _OutputSpec:
    headers: tuple[str, ...]
    libraries: tuple[str, ...]
    pkg_config_file: str | None
    pkg_config_name: str | None
    smoke_source: str
    language: str = "c"

    @property
    def files(self) -> tuple[str, ...]:
        values = (*self.headers, *self.libraries)
        if self.pkg_config_file:
            values = (*values, f"lib/pkgconfig/{self.pkg_config_file}")
        return values


_OUTPUTS: Mapping[str, _OutputSpec] = {
    "gettext": _OutputSpec(
        headers=("include/libintl.h",),
        libraries=("lib/libintl.a",),
        pkg_config_file=None,
        pkg_config_name=None,
        smoke_source=(
            "#include <libintl.h>\n" 'int main(void) { return textdomain("g4-wave2") == 0; }\n'
        ),
    ),
    "pcre": _OutputSpec(
        headers=("include/pcre.h",),
        libraries=("lib/libpcre.a",),
        pkg_config_file="libpcre.pc",
        pkg_config_name="libpcre",
        smoke_source=("#include <pcre.h>\n" "int main(void) { return pcre_version() == 0; }\n"),
    ),
    "xvidcore": _OutputSpec(
        headers=("include/xvid.h",),
        libraries=("lib/libxvidcore.a",),
        pkg_config_file=None,
        pkg_config_name=None,
        smoke_source=(
            "#include <xvid.h>\n"
            "int main(void) { xvid_gbl_info_t info = {0}; "
            "info.version = XVID_VERSION; "
            "return xvid_global(0, XVID_GBL_INFO, &info, 0) < 0; }\n"
        ),
    ),
    "opencore": _OutputSpec(
        headers=(
            "include/opencore-amrnb/interf_dec.h",
            "include/opencore-amrnb/interf_enc.h",
            "include/opencore-amrwb/dec_if.h",
        ),
        libraries=("lib/libopencore-amrnb.a", "lib/libopencore-amrwb.a"),
        pkg_config_file=None,
        pkg_config_name=None,
        smoke_source=(
            "#include <opencore-amrnb/interf_dec.h>\n"
            "int main(void) { void *decoder = Decoder_Interface_init(); "
            "if (!decoder) return 1; Decoder_Interface_exit(decoder); return 0; }\n"
        ),
    ),
    "lame": _OutputSpec(
        headers=("include/lame/lame.h",),
        libraries=("lib/libmp3lame.a",),
        pkg_config_file=None,
        pkg_config_name=None,
        smoke_source=(
            "#include <lame/lame.h>\n"
            "int main(void) { lame_global_flags *g = lame_init(); "
            "if (!g) return 1; return lame_close(g) < 0; }\n"
        ),
    ),
    "fdk_aac": _OutputSpec(
        headers=("include/fdk-aac/aacenc_lib.h", "include/fdk-aac/aacdecoder_lib.h"),
        libraries=("lib/libfdk-aac.a",),
        pkg_config_file="fdk-aac.pc",
        pkg_config_name="fdk-aac",
        smoke_source=(
            "#include <fdk-aac/aacenc_lib.h>\n"
            "int main(void) { HANDLE_AACENCODER encoder = 0; "
            "AACENC_ERROR status = aacEncOpen(&encoder, 0, 2); "
            "if (encoder) aacEncClose(&encoder); return status != AACENC_OK; }\n"
        ),
    ),
    "soxr": _OutputSpec(
        headers=("include/soxr.h",),
        libraries=("lib/libsoxr.a",),
        pkg_config_file="soxr.pc",
        pkg_config_name="soxr",
        smoke_source=(
            "#include <soxr.h>\n"
            "int main(void) { soxr_error_t error = 0; "
            "soxr_t resampler = soxr_create(48000, 48000, 1, &error, 0, 0, 0); "
            "if (resampler) soxr_delete(resampler); return error != 0; }\n"
        ),
    ),
    "libtiff": _OutputSpec(
        headers=("include/tiffio.h", "include/tiffvers.h"),
        libraries=("lib/libtiff.a",),
        pkg_config_file="libtiff-4.pc",
        pkg_config_name="libtiff-4",
        smoke_source=("#include <tiffio.h>\n" "int main(void) { return TIFFGetVersion() == 0; }\n"),
    ),
    "lcms2": _OutputSpec(
        headers=("include/lcms2.h",),
        libraries=("lib/liblcms2.a",),
        pkg_config_file="lcms2.pc",
        pkg_config_name="lcms2",
        smoke_source=(
            "#include <lcms2.h>\n" "int main(void) { return cmsGetEncodedCMMversion() == 0; }\n"
        ),
    ),
    "libwebp": _OutputSpec(
        headers=("include/webp/decode.h", "include/webp/encode.h"),
        libraries=("lib/libwebp.a", "lib/libsharpyuv.a"),
        pkg_config_file="libwebp.pc",
        pkg_config_name="libwebp",
        smoke_source=(
            "#include <webp/decode.h>\n" "int main(void) { return WebPGetDecoderVersion() == 0; }\n"
        ),
    ),
    "libsdl": _OutputSpec(
        headers=("include/SDL2/SDL.h",),
        libraries=("lib/libSDL2.a",),
        pkg_config_file="sdl2.pc",
        pkg_config_name="sdl2",
        smoke_source=(
            "#define SDL_MAIN_HANDLED\n"
            "#include <SDL.h>\n"
            "#undef main\n"
            "int main(void) { return SDL_GetRevision() == 0; }\n"
        ),
    ),
}


def verify_wave_sources(registry: ComponentRegistry, source_archives_dir: Path) -> dict[str, str]:
    """Verify every registered Wave-2 archive before any component build."""
    outcomes: dict[str, str] = {}
    for name in G4_WAVE2_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None:
            raise BuildError(name, "Wave-2 component is missing from components.yaml.")
        if not component.sha256:
            raise BuildError(name, "Wave-2 source has no registered SHA-256 checksum.")
        archive = source_archives_dir / component.get_archive_filename()
        try:
            with archive.open("rb") as source:
                header = source.read(128)
                source.seek(0)
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as exc:
            raise BuildError(
                name,
                f"Required project-managed source archive is unavailable: {archive}: {exc}",
            ) from exc
        if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise BuildError(name, f"Source archive is an unresolved Git LFS pointer: {archive}")
        if digest.lower() != component.sha256.lower():
            raise BuildError(
                name,
                f"Source checksum mismatch for {archive}: expected {component.sha256}, "
                f"got {digest}.",
            )
        outcomes[name] = digest
    return outcomes


def order_wave_components(
    registry: ComponentRegistry,
    selected_target_components: Sequence[Component],
    accepted_external_dependencies: Sequence[str] = (),
) -> list[Component]:
    """Order exactly the Wave-2 components and reject unaccepted dependencies."""
    available = {component.name: component for component in selected_target_components}
    requested = set(G4_WAVE2_COMPONENTS)
    if len(requested) != len(G4_WAVE2_COMPONENTS):
        raise BuildError("G4 Wave 2", "The requested Wave-2 subset contains duplicates.")
    missing = requested - available.keys()
    if missing:
        raise BuildError(
            "G4 Wave 2",
            "Requested components are not in the configured cross target selection: "
            + ", ".join(sorted(missing)),
        )
    accepted = set(accepted_external_dependencies)
    ordered: list[Component] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise BuildError("G4 Wave 2", f"Dependency cycle detected at {name}.")
        visiting.add(name)
        component = available[name]
        for dependency_name in component.depends_on:
            dependency = registry.get_by_name(dependency_name)
            if dependency is None:
                raise BuildError(
                    component.name,
                    f"Declared dependency {dependency_name!r} is missing from the registry.",
                )
            if dependency.build_role != BuildRole.TARGET_COMPONENT:
                raise BuildError(
                    component.name,
                    f"Target dependency {dependency_name!r} is not a TARGET_COMPONENT.",
                )
            if dependency_name in requested:
                visit(dependency_name)
            elif dependency_name not in accepted:
                raise BuildError(
                    component.name,
                    f"Dependency {dependency_name!r} is outside Wave 2 and has not been "
                    "accepted for this identity.",
                )
        visiting.remove(name)
        visited.add(name)
        ordered.append(component)

    for name in G4_WAVE2_COMPONENTS:
        visit(name)
    return ordered


class Wave2TargetValidator:
    """Verify Wave-2 target outputs, COFF archives, metadata, and linkability."""

    def __init__(self, environment: CrossEnvironment, identity_key: str):
        self.environment = environment
        self.prefix = environment.paths.target_prefix.resolve()
        self.manifest_path = environment.paths.workspace_root / _ACCEPTANCE_MANIFEST
        self.identity_key = identity_key

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        for relative in spec.files:
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Expected target output is missing: {path}")
            self._require_inside_prefix(component.name, path)
        for relative in spec.libraries:
            self._validate_archive(component.name, self.prefix / relative)
        if spec.pkg_config_file:
            self._validate_pkg_config(component.name, spec)
        self._link_smoke(component.name, spec)
        return list(spec.files)

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        if manifest is None or manifest.get("identity_key") != self.identity_key:
            return False
        components = manifest.get("components")
        entry = components.get(component.name) if isinstance(components, dict) else None
        if not isinstance(entry, dict) or entry.get("version") != component.version:
            return False
        hashes = entry.get("sha256")
        spec = self._spec(component)
        if not isinstance(hashes, dict) or set(hashes) != set(spec.files):
            return False
        return all(
            (self.prefix / relative).is_file()
            and self._sha256(self.prefix / relative) == hashes.get(relative)
            for relative in spec.files
        )

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        manifest = self._read_manifest() or {
            "schema_version": 1,
            "identity_key": self.identity_key,
            "target_prefix": str(self.prefix),
            "components": {},
        }
        if manifest.get("identity_key") != self.identity_key:
            manifest = {
                "schema_version": 1,
                "identity_key": self.identity_key,
                "target_prefix": str(self.prefix),
                "components": {},
            }
        entries = manifest.setdefault("components", {})
        entries[component.name] = {
            "version": component.version,
            "outputs": list(outputs),
            "sha256": {path: self._sha256(self.prefix / path) for path in outputs},
        }
        self._write_manifest(manifest)

    def remove_component_outputs(self, component: Component) -> None:
        for relative in self._spec(component).files:
            path = self.prefix / relative
            if path.is_file() or path.is_symlink():
                path.unlink()
        manifest = self._read_manifest()
        if manifest and manifest.get("identity_key") == self.identity_key:
            entries = manifest.get("components")
            if isinstance(entries, dict) and component.name in entries:
                del entries[component.name]
                self._write_manifest(manifest)

    def validate_prefix(self) -> None:
        """Reject Linux/host files and metadata throughout the identity prefix."""
        wave1_validator = Wave1TargetValidator(self.environment, self.identity_key)
        for path in self.prefix.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            self._require_inside_prefix("target prefix", path)
            if path.suffix.lower() in {".so", ".dylib", ".dll", ".exe", ".o", ".obj"}:
                raise BuildError(
                    "target prefix",
                    f"Unexpected shared library, executable, or object file installed: {path}",
                )
            try:
                with path.open("rb") as stream:
                    magic = stream.read(4)
            except OSError as exc:
                raise BuildError("target prefix", f"Cannot inspect {path}: {exc}") from exc
            if magic == b"\x7fELF":
                raise BuildError("target prefix", f"Linux ELF artifact leaked into {path}")
            if path.suffix.lower() == ".a":
                self._validate_archive("target prefix", path)
            if path.suffix.lower() in {".pc", ".cmake", ".la"}:
                wave1_validator._validate_metadata_paths(path)

    def require_openmp_metadata(self, registry: ComponentRegistry) -> None:
        metadata = registry.get_target_link_metadata(["soxr"])
        if "-fopenmp" not in metadata.consumer_link_requirements:
            raise BuildError("soxr", "OpenMP is absent from target static-consumer metadata.")
        runtime = [
            item
            for item in metadata.runtime_dependencies
            if item.name.lower() == "libomp.dll"
            and item.classification == "toolchain-runtime-dll"
            and item.source_identity == self.environment.target_tools.prefix.name
        ]
        if not runtime:
            raise BuildError(
                "soxr",
                "The LLVM-MinGW OpenMP runtime DLL is absent from target link metadata.",
            )

    def validate_soxr_metadata_link(
        self, component: Component, registry: ComponentRegistry
    ) -> dict[str, Any]:
        """Prove static soxr links with metadata and fails without its OpenMP requirement."""
        spec = self._spec(component)
        source = self.environment.paths.generated_files_root / "soxr-metadata-consumer.c"
        executable = source.with_suffix(".exe")
        source.write_text(spec.smoke_source, encoding="utf-8")
        env = self.environment.compiler_environment
        base_flags = shlex.split(env.get("CFLAGS", ""))
        link_flags = shlex.split(env.get("LDFLAGS", ""))
        no_openmp_cflags = [flag for flag in base_flags if flag != "-fopenmp"]
        no_openmp_ldflags = [flag for flag in link_flags if flag != "-fopenmp"]
        archive = str(self.prefix / "lib/libsoxr.a")
        compiler = str(self.environment.target_tools.cc)
        negative = subprocess.run(
            [
                compiler,
                *no_openmp_cflags,
                str(source),
                archive,
                *no_openmp_ldflags,
                "-o",
                str(executable),
            ],
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if negative.returncode == 0:
            executable.unlink(missing_ok=True)
            raise BuildError(
                "soxr",
                "Static soxr linked without its declared OpenMP metadata; "
                "the negative metadata acceptance did not fail.",
            )
        metadata = registry.get_target_link_metadata(["soxr"])
        positive = subprocess.run(
            [
                compiler,
                *base_flags,
                str(source),
                archive,
                *link_flags,
                *metadata.consumer_link_requirements,
                "-o",
                str(executable),
            ],
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if positive.returncode:
            raise BuildError(
                "soxr",
                "Static soxr did not link with declared OpenMP requirements: "
                + (positive.stderr or positive.stdout)[-4000:],
            )
        self._validate_executable("soxr", executable)
        executable.unlink(missing_ok=True)
        return {
            "negative_without_metadata_failed": True,
            "positive_with_metadata_passed": True,
            "requirements": list(metadata.consumer_link_requirements),
            "runtime_dependencies": [item.name for item in metadata.runtime_dependencies],
        }

    def _validate_pkg_config(self, component_name: str, spec: _OutputSpec) -> None:
        assert spec.pkg_config_file and spec.pkg_config_name
        pc = self.prefix / "lib/pkgconfig" / spec.pkg_config_file
        wrapper = str(self.environment.pkg_config_wrapper)
        env = dict(self.environment.compiler_environment)
        prefix = self._run(
            component_name,
            [wrapper, "--variable=prefix", spec.pkg_config_name],
            env,
            "target pkg-config prefix lookup failed",
        ).strip()
        target_prefix = Path(prefix)
        if not target_prefix.is_absolute() or not self._inside(target_prefix, self.prefix):
            raise BuildError(component_name, f"pkg-config reports a non-target prefix: {prefix!r}")
        for option in ("--cflags", "--libs"):
            output = self._run(
                component_name,
                [wrapper, "--static", option, spec.pkg_config_name],
                env,
                "target pkg-config query failed",
            )
            self._validate_pkg_flags(component_name, output)
        self._require_inside_prefix(component_name, pc)

    def _link_smoke(self, component_name: str, spec: _OutputSpec) -> None:
        env = dict(self.environment.compiler_environment)
        flags = shlex.split(env.get("CXXFLAGS" if spec.language == "c++" else "CFLAGS", ""))
        linker_flags = shlex.split(env.get("LDFLAGS", ""))
        libraries: list[str] = []
        if spec.pkg_config_name:
            output = self._run(
                component_name,
                [
                    str(self.environment.pkg_config_wrapper),
                    "--static",
                    "--cflags",
                    spec.pkg_config_name,
                ],
                env,
                "target pkg-config compile query failed",
            )
            flags.extend(shlex.split(output))
            output = self._run(
                component_name,
                [
                    str(self.environment.pkg_config_wrapper),
                    "--static",
                    "--libs",
                    spec.pkg_config_name,
                ],
                env,
                "target pkg-config link query failed",
            )
            libraries.extend(shlex.split(output))
        else:
            libraries.extend(str(self.prefix / relative) for relative in spec.libraries)
        if component_name == "libtiff" and not spec.pkg_config_name:
            libraries.append("-lz")
        source = self.environment.paths.generated_files_root / f"{component_name}-consumer.c"
        executable = source.with_suffix(".exe")
        source.write_text(spec.smoke_source, encoding="utf-8")
        compiler = (
            self.environment.target_tools.cxx
            if spec.language == "c++"
            else self.environment.target_tools.cc
        )
        result = subprocess.run(
            [
                str(compiler),
                *flags,
                str(source),
                *libraries,
                *linker_flags,
                "-o",
                str(executable),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise BuildError(
                component_name,
                "target consumer link smoke failed: " + (result.stderr or result.stdout)[-4000:],
            )
        self._validate_executable(component_name, executable)
        executable.unlink(missing_ok=True)

    def _validate_archive(self, component_name: str, archive: Path) -> None:
        result = subprocess.run(
            [str(self.environment.target_tools.readobj), "--file-headers", str(archive)],
            env=dict(self.environment.compiler_environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode:
            raise BuildError(
                component_name,
                f"LLVM-MinGW could not inspect {archive}: "
                f"{(result.stderr or result.stdout).strip()}",
            )
        formats = re.findall(r"^Format:\s*(\S+)", result.stdout, flags=re.MULTILINE)
        if not formats or any(
            value not in {"COFF-x86-64", "COFF-import-file-x86-64"} for value in formats
        ):
            raise BuildError(component_name, f"Archive contains non-x86-64 COFF members: {archive}")

    def _validate_executable(self, component_name: str, executable: Path) -> None:
        result = subprocess.run(
            [str(self.environment.target_tools.readobj), "--file-headers", str(executable)],
            env=dict(self.environment.compiler_environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if (
            result.returncode
            or "Format: COFF-x86-64" not in result.stdout
            or not re.search(r"Magic:\s*0x20[Bb]", result.stdout)
        ):
            raise BuildError(component_name, f"Expected a PE32+ x86-64 executable: {executable}")

    def _validate_pkg_flags(self, component_name: str, output: str) -> None:
        tokens = shlex.split(output)
        for index, token in enumerate(tokens):
            value = None
            if token.startswith(("-I", "-L")) and len(token) > 2:
                value = token[2:]
            elif token in ("-I", "-L") and index + 1 < len(tokens):
                value = tokens[index + 1]
            if value and Path(value).is_absolute() and not self._inside(Path(value), self.prefix):
                raise BuildError(
                    component_name,
                    f"Target pkg-config returned a path outside the target prefix: {value}",
                )

    def _require_inside_prefix(self, component_name: str, path: Path) -> None:
        if not self._inside(path.resolve(strict=False), self.prefix):
            raise BuildError(
                component_name, f"Installed output escapes the authorized target prefix: {path}"
            )

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            return False

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        try:
            return _OUTPUTS[component.name]
        except KeyError as exc:
            raise BuildError(component.name, "No Wave-2 output contract is defined.") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    @staticmethod
    def _run(
        component_name: str,
        command: list[str],
        env: Mapping[str, str],
        message: str,
    ) -> str:
        result = subprocess.run(
            command,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise BuildError(
                component_name, f"{message}: {(result.stderr or result.stdout)[-4000:]}"
            )
        return result.stdout

    def _read_manifest(self) -> dict[str, Any] | None:
        try:
            result = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return result if isinstance(result, dict) else None

    def _write_manifest(self, manifest: Mapping[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.manifest_path)


def accept_openmp_profile(
    environment: CrossEnvironment,
    config: BuildConfig,
    identity_key: str,
) -> dict[str, Any]:
    """Compile/link C and C++ OpenMP programs and run them on physical Windows."""
    if not config.native_build or not config.openmp:
        raise BuildError(
            "G4 Wave 2 OpenMP",
            "The primary profile requires native_build=true and openmp=true; "
            "compatibility settings are not an acceptance fallback.",
        )
    env = environment.compiler_environment
    for key in ("CFLAGS", "CXXFLAGS"):
        flags = shlex.split(env.get(key, ""))
        for flag in CPU_TUNING_FLAGS + ("-fopenmp",):
            if flags.count(flag) != 1:
                raise BuildError(
                    "G4 Wave 2 OpenMP",
                    f"Primary target {key} must contain {flag} exactly once: {flags}",
                )
        if any(
            flag.startswith(("-march=", "-mtune=")) and flag not in CPU_TUNING_FLAGS
            for flag in flags
        ):
            raise BuildError(
                "G4 Wave 2 OpenMP",
                f"Unexpected target CPU tuning option in {key}: {flags}",
            )
    if shlex.split(env.get("LDFLAGS", "")).count("-fopenmp") != 1:
        raise BuildError("G4 Wave 2 OpenMP", "Primary target LDFLAGS must contain -fopenmp once.")
    if env.get("CC") != str(environment.target_tools.cc) or env.get("CXX") != str(
        environment.target_tools.cxx
    ):
        raise BuildError(
            "G4 Wave 2 OpenMP", "The selected target C/C++ compilers are not projected."
        )

    target_root = environment.target_tools.prefix
    library = target_root / "x86_64-w64-mingw32/lib/libomp.dll.a"
    runtime = target_root / "x86_64-w64-mingw32/bin/libomp.dll"
    header = target_root / "generic-w64-mingw32/include/omp.h"
    for path in (library, runtime, header):
        if not path.is_file():
            raise BuildError(
                "G4 Wave 2 OpenMP", f"Verified LLVM-MinGW OpenMP file is missing: {path}"
            )
    readobj = str(environment.target_tools.readobj)
    for path in (library, runtime):
        result = subprocess.run(
            [readobj, "--file-headers", str(path)],
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode or "COFF-x86-64" not in result.stdout:
            raise BuildError("G4 Wave 2 OpenMP", f"OpenMP runtime is not x86-64 COFF: {path}")

    generated = environment.paths.generated_files_root
    generated.mkdir(parents=True, exist_ok=True)
    temp_result = subprocess.run(
        ["/mnt/c/WINDOWS/system32/cmd.exe", "/d", "/c", "echo %PUBLIC%"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    windows_temp = next(
        (line.strip() for line in temp_result.stdout.splitlines() if line.strip()), ""
    )
    if temp_result.returncode or not re.match(r"^[A-Za-z]:[\\/]", windows_temp):
        raise BuildError(
            "G4 Wave 2 OpenMP",
            f"Could not resolve the Windows public temporary directory: {temp_result.stderr}",
        )
    temp_path_result = subprocess.run(
        ["wslpath", "-u", windows_temp],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    windows_temp_path = Path(temp_path_result.stdout.strip())
    if temp_path_result.returncode or not windows_temp_path.is_dir():
        raise BuildError(
            "G4 Wave 2 OpenMP",
            f"Windows temporary directory is not accessible from WSL: {windows_temp}",
        )
    runtime_windows_path = subprocess.run(
        ["wslpath", "-w", str(runtime)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if runtime_windows_path.returncode:
        raise BuildError("G4 Wave 2 OpenMP", "Cannot map the OpenMP runtime DLL to Windows.")
    runtime_windows_file = runtime_windows_path.stdout.strip()
    runtime_windows_dir_path = subprocess.run(
        ["wslpath", "-w", str(runtime.parent)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if runtime_windows_dir_path.returncode:
        raise BuildError("G4 Wave 2 OpenMP", "Cannot map the OpenMP runtime directory to Windows.")
    runtime_windows_dir = runtime_windows_dir_path.stdout.strip()

    sources = {
        "c": (
            "#include <omp.h>\n#include <windows.h>\n#include <stdio.h>\n"
            "int main(void) { int workers=0;\n"
            "#pragma omp parallel reduction(+:workers)\n"
            "workers += 1;\n"
            'char path[32768]={0}; HMODULE h=GetModuleHandleA("libomp.dll");\n'
            "if (h) GetModuleFileNameA(h,path,sizeof(path));\n"
            'printf("workers=%d max=%d runtime=%s\\n",workers,omp_get_max_threads(),path);\n'
            "return workers < 1 || !h; }\n"
        ),
        "c++": (
            "#include <omp.h>\n#include <windows.h>\n#include <cstdio>\n"
            "int main() { int workers=0;\n"
            "#pragma omp parallel reduction(+:workers)\n"
            "workers += 1;\n"
            'char path[32768]={0}; HMODULE h=GetModuleHandleA("libomp.dll");\n'
            "if (h) GetModuleFileNameA(h,path,sizeof(path));\n"
            'std::printf("workers=%d max=%d runtime=%s\\n",workers,omp_get_max_threads(),path);\n'
            "return workers < 1 || !h; }\n"
        ),
    }
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="g4-wave2-openmp-", dir=windows_temp_path) as directory:
        scratch = Path(directory)
        for language, compiler in (
            ("c", environment.target_tools.cc),
            ("c++", environment.target_tools.cxx),
        ):
            source = scratch / f"openmp.{language.replace('+', 'p')}"
            executable = scratch / f"openmp-{language.replace('+', 'p')}.exe"
            source.write_text(sources[language], encoding="utf-8")
            flags = shlex.split(env["CFLAGS" if language == "c" else "CXXFLAGS"])
            linker_flags = shlex.split(env.get("LDFLAGS", ""))
            command = [str(compiler), *flags, str(source), *linker_flags, "-o", str(executable)]
            link_trace = subprocess.run(
                [str(compiler), "-###", *flags, str(source), *linker_flags, "-o", str(executable)],
                env=dict(env),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if (
                link_trace.returncode
                or f"-L{library.parent}" not in link_trace.stderr
                or not re.search(r'(?<!\S)"?-lomp"?', link_trace.stderr)
                or re.search(r"(?:libgomp|libomp)\.so(?:\.\d+)*", link_trace.stderr, re.IGNORECASE)
            ):
                raise BuildError(
                    "G4 Wave 2 OpenMP",
                    "LLVM-MinGW did not resolve its Windows target OpenMP import library: "
                    + (link_trace.stderr or link_trace.stdout)[-4000:],
                )
            result = subprocess.run(
                command,
                env=dict(env),
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            if result.returncode:
                raise BuildError(
                    "G4 Wave 2 OpenMP",
                    f"Target {language} OpenMP compile/link failed: "
                    + (result.stderr or result.stdout)[-4000:],
                )
            inspection = subprocess.run(
                [readobj, "--file-headers", "--coff-imports", str(executable)],
                env=dict(env),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if (
                inspection.returncode
                or "Format: COFF-x86-64" not in inspection.stdout
                or not re.search(r"Magic:\s*0x20[Bb]", inspection.stdout)
                or "Name: libomp.dll" not in inspection.stdout
                or re.search(r"Name: .*\.so(?:\.|$)", inspection.stdout, flags=re.IGNORECASE)
            ):
                raise BuildError(
                    "G4 Wave 2 OpenMP",
                    f"OpenMP {language} executable lacks PE32+ / Windows OpenMP imports.",
                )
            windows_exe_result = subprocess.run(
                ["wslpath", "-w", str(executable)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if windows_exe_result.returncode:
                raise BuildError("G4 Wave 2 OpenMP", "Cannot map the OpenMP probe to Windows.")
            command_line = (
                f"set PATH={runtime_windows_dir};C:\\Windows\\System32;C:\\Windows"
                f"&{windows_exe_result.stdout.strip()}"
            )
            execution = subprocess.run(
                ["/mnt/c/WINDOWS/system32/cmd.exe", "/d", "/c", command_line],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            output = execution.stdout.strip()
            match = re.search(r"workers=(\d+)\s+max=(\d+)\s+runtime=(.+)", output)
            if execution.returncode or match is None:
                raise BuildError(
                    "G4 Wave 2 OpenMP",
                    f"Windows OpenMP {language} runtime smoke failed: "
                    f"exit={execution.returncode}, command={command_line}, "
                    f"stdout={output}, stderr={execution.stderr.strip()}",
                )
            workers, maximum, loaded = int(match.group(1)), int(match.group(2)), match.group(3)
            if workers < 1 or Path(loaded.replace("\\", "/")).name.lower() != "libomp.dll":
                raise BuildError(
                    "G4 Wave 2 OpenMP",
                    f"Windows OpenMP {language} runtime evidence is invalid: {output}",
                )
            normalized_loaded = loaded.replace("\\", "/").lower()
            normalized_expected = runtime_windows_file.replace("\\", "/").lower()
            if normalized_loaded != normalized_expected:
                raise BuildError(
                    "G4 Wave 2 OpenMP",
                    "Windows loaded a different OpenMP runtime than the verified LLVM-MinGW "
                    f"closure: {loaded}",
                )
            results[language] = {
                "compiler": str(compiler),
                "flags": [*flags, *linker_flags],
                "executable_sha256": Wave2TargetValidator._sha256(executable),
                "pe32_plus_x86_64": True,
                "import": "libomp.dll",
                "windows_exit_code": execution.returncode,
                "workers": workers,
                "max_threads": maximum,
                "loaded_runtime": loaded,
            }

    manifest = {
        "schema_version": 1,
        "identity_key": identity_key,
        "target": environment.target_triple,
        "toolchain": target_root.name,
        "openmp_header": str(header),
        "openmp_import_library": str(library),
        "openmp_runtime_dll": str(runtime),
        "openmp_runtime_sha256": Wave2TargetValidator._sha256(runtime),
        "cflags": env["CFLAGS"],
        "cxxflags": env["CXXFLAGS"],
        "ldflags": env["LDFLAGS"],
        "c": results["c"],
        "cxx": results["c++"],
    }
    path = environment.paths.workspace_root / _OPENMP_MANIFEST
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def build_or_reuse_component(
    component: Component,
    builder: Any,
    state_manager: Any,
    validator: Wave2TargetValidator,
) -> bool:
    """Build/accept a Wave-2 output set or reuse validated same-identity files."""
    prior = state_manager.get().components.get(component.name)
    if (
        prior is not None
        and prior.status == ComponentStatus.COMPLETED
        and validator.manifest_matches(component)
    ):
        try:
            validator.validate_component(component)
            validator.validate_prefix()
            return False
        except BuildError:
            validator.remove_component_outputs(component)
    try:
        validator.remove_component_outputs(component)
        state_manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-2 target outputs require build or revalidation",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        state_manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="installed target outputs and COFF/link acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        validator.remove_component_outputs(component)
        state_manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="G4 Wave-2 build or target acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-2 build or acceptance failed: {exc}") from exc


def run_g4_wave2(app: Any, config: BuildConfig) -> int:
    """Run only the exact Wave-2 component subset with mandatory OpenMP."""
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW:
        raise BuildError("G4 Wave 2", "Wave 2 requires target_backend=linux-cross-mingw.")
    if not config.native_build or not config.openmp:
        raise BuildError(
            "G4 Wave 2",
            "Primary Windows acceptance requires native_build=true and openmp=true; "
            "there is no compatibility retry.",
        )
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    verified_sources = verify_wave_sources(app.registry, archives)
    print("Verified local Wave-2 source archives:")
    for name, digest in verified_sources.items():
        print(f"  {name}: sha256={digest}")
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
    except BuildError as exc:
        print(f"BLOCKED by G1/G2/G3 acceptance: {exc}")
        return 2

    try:
        openmp_acceptance = accept_openmp_profile(environment, config, session.identity.build_key)
    except BuildError as exc:
        print(f"FAIL: mandatory primary-profile OpenMP acceptance failed: {exc}")
        return 1
    print(
        "Primary OpenMP C/C++ compile-link and physical Windows runtime acceptance passed; "
        f"runtime=libomp.dll ({environment.target_tools.prefix.name})."
    )

    selection = app._get_component_selection(config)
    state = app.state_manager.get()
    wave1 = Wave1TargetValidator(environment, session.identity.build_key)
    accepted_wave1 = []
    try:
        for name in ("zlib", "libogg", "opus", "libpng", "FreeType2", "dav1d", "vid_stab"):
            component = app.registry.get_by_name(name)
            current = state.components.get(name)
            if (
                component is None
                or current is None
                or current.status != ComponentStatus.COMPLETED
                or not wave1.manifest_matches(component)
            ):
                raise BuildError(
                    name, "Accepted Wave-1 dependency is not reusable for this target identity."
                )
            wave1.validate_component(component)
            accepted_wave1.append(name)
        wave1.validate_prefix()
        selected = order_wave_components(
            app.registry,
            selection.target_components_to_build,
            accepted_external_dependencies=accepted_wave1,
        )
    except BuildError as exc:
        print(f"BLOCKED by accepted Wave-1 outputs or exact Wave-2 selection: {exc}")
        return 2

    required_tools = sorted({tool for component in selected for tool in component.requires_tools})
    missing_tools = [name for name in required_tools if not app._host_tool_available(name)]
    if missing_tools:
        print("BLOCKED: required WSL host tools unavailable: " + ", ".join(missing_tools))
        return 2

    print("Wave-2 target component execution order:")
    for component in selected:
        print(f"  {component.name} {component.version} ({component.build_system.value})")
    builder = FFmpegBuilder(
        config,
        app.workspace,
        app.packages,
        app.state_manager,
        app.platform_detector,
        toolchain_env=app._component_toolchain_env(config, selected),
        cross_environment=environment,
    )
    validator = Wave2TargetValidator(environment, session.identity.build_key)
    state.config = config.to_dict()
    state.total_steps = len(selected)
    app.state_manager.save()
    accepted: list[str] = []
    built: list[str] = []
    try:
        for position, component in enumerate(selected, 1):
            print(f"\n=== G4 Wave 2 [{position}/{len(selected)}] {component.name} ===")
            if build_or_reuse_component(component, builder, app.state_manager, validator):
                built.append(component.name)
            accepted.append(component.name)
        validator.require_openmp_metadata(app.registry)
        soxr = app.registry.get_by_name("soxr")
        if soxr is None:
            raise BuildError("soxr", "Registered soxr component is missing.")
        openmp_metadata = validator.validate_soxr_metadata_link(soxr, app.registry)
        validator.validate_prefix()
        accepted_manifest = {
            "schema_version": 1,
            "identity_key": session.identity.build_key,
            "target_prefix": str(environment.paths.target_prefix),
            "components": _read_component_acceptance(validator),
            "openmp": {
                "manifest": _OPENMP_MANIFEST,
                "runtime": "libomp.dll",
                "runtime_classification": "toolchain-runtime-dll; G6 packaging input",
                "link_metadata": openmp_metadata,
            },
            "wave1_reused": accepted_wave1,
            "wave2_built": built,
            "wave2_accepted": accepted,
        }
        validator.manifest_path.write_text(
            json.dumps(accepted_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nG4 Wave 2 accepted for identity {session.identity.build_key}.")
        print("OpenMP static consumer metadata:", openmp_metadata)
        print("Wave-2 components accepted:", ", ".join(accepted))
        return 0
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        print(f"\nG4 Wave 2 stopped: {exc}")
        return 1
    finally:
        builder.shutdown_downloads(wait=True)


def _read_component_acceptance(validator: Wave2TargetValidator) -> dict[str, Any]:
    manifest = validator._read_manifest()
    if manifest is None or manifest.get("identity_key") != validator.identity_key:
        raise BuildError("G4 Wave 2", "Wave-2 component acceptance manifest is missing.")
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise BuildError("G4 Wave 2", "Wave-2 component acceptance data is invalid.")
    return components
