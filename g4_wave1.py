"""Bounded G4 Wave-1 cross-build and installed-target acceptance."""

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
from .cross_environment import CrossEnvironment
from .state import ComponentStatus

G4_WAVE1_COMPONENTS = (
    "zlib",
    "libogg",
    "opus",
    "libpng",
    "FreeType2",
    "dav1d",
    "vid_stab",
)

_ACCEPTANCE_MANIFEST = "g4-wave1-acceptance.json"


@dataclass(frozen=True)
class _OutputSpec:
    headers: tuple[str, ...]
    libraries: tuple[str, ...]
    pkg_config_file: str
    pkg_config_name: str
    smoke_source: str
    pkg_config_aliases: tuple[str, ...] = ()
    extra_pkg_config_files: tuple[str, ...] = ()
    pkg_config_dependencies: tuple[str, ...] = ()
    extra_cleanup: tuple[str, ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        return (
            *self.headers,
            *self.libraries,
            f"lib/pkgconfig/{self.pkg_config_file}",
            *self.extra_pkg_config_files,
        )

    @property
    def cleanup_files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.files, *self.extra_cleanup)))

    @property
    def pkg_config_names(self) -> tuple[str, ...]:
        return (self.pkg_config_name, *self.pkg_config_aliases)


_OUTPUTS: Mapping[str, _OutputSpec] = {
    "zlib": _OutputSpec(
        headers=("include/zlib.h", "include/zconf.h"),
        libraries=("lib/libz.a",),
        pkg_config_file="zlib.pc",
        pkg_config_name="zlib",
        smoke_source=("#include <zlib.h>\n" "int main(void) { return zlibVersion() == 0; }\n"),
    ),
    "libogg": _OutputSpec(
        headers=("include/ogg/ogg.h", "include/ogg/os_types.h"),
        libraries=("lib/libogg.a",),
        pkg_config_file="ogg.pc",
        pkg_config_name="ogg",
        smoke_source=(
            "#include <ogg/ogg.h>\n"
            "int main(void) { ogg_stream_state s; "
            "if (ogg_stream_init(&s, 1)) return 1; return ogg_stream_clear(&s); }\n"
        ),
    ),
    "opus": _OutputSpec(
        headers=("include/opus/opus.h",),
        libraries=("lib/libopus.a",),
        pkg_config_file="opus.pc",
        pkg_config_name="opus",
        smoke_source=(
            "#include <opus/opus.h>\n" "int main(void) { return opus_get_version_string() == 0; }\n"
        ),
    ),
    "libpng": _OutputSpec(
        headers=(
            "include/libpng16/png.h",
            "include/libpng16/pngconf.h",
            "include/libpng16/pnglibconf.h",
        ),
        libraries=("lib/libpng16.a",),
        pkg_config_file="libpng16.pc",
        pkg_config_name="libpng16",
        smoke_source=(
            "#include <png.h>\n"
            "int main(void) { png_structp p = png_create_write_struct("
            "PNG_LIBPNG_VER_STRING, 0, 0, 0); "
            "if (!p) return 1; png_destroy_write_struct(&p, 0); return 0; }\n"
        ),
        pkg_config_aliases=("libpng",),
        extra_pkg_config_files=("lib/pkgconfig/libpng.pc",),
        extra_cleanup=(
            "bin/libpng-config",
            "bin/libpng16-config",
            "bin/pngfix.exe",
            "bin/png-fix-itxt.exe",
            "include/png.h",
            "include/pngconf.h",
            "include/pnglibconf.h",
            "lib/libpng.a",
            "lib/libpng.la",
            "lib/libpng16.la",
            "lib/pkgconfig/libpng.pc",
            "share/man/man3/libpng.3",
            "share/man/man3/libpngpf.3",
            "share/man/man5/png.5",
        ),
    ),
    "FreeType2": _OutputSpec(
        headers=(
            "include/freetype2/ft2build.h",
            "include/freetype2/freetype/config/ftheader.h",
        ),
        libraries=("lib/libfreetype.a",),
        pkg_config_file="freetype2.pc",
        pkg_config_name="freetype2",
        smoke_source=(
            "#include <ft2build.h>\n"
            "#include FT_FREETYPE_H\n"
            "int main(void) { FT_Library library; "
            "if (FT_Init_FreeType(&library)) return 1; "
            "return FT_Done_FreeType(library); }\n"
        ),
        pkg_config_dependencies=("zlib", "libpng"),
    ),
    "dav1d": _OutputSpec(
        headers=("include/dav1d/dav1d.h",),
        libraries=("lib/libdav1d.a",),
        pkg_config_file="dav1d.pc",
        pkg_config_name="dav1d",
        smoke_source=(
            "#include <dav1d/dav1d.h>\n"
            "int main(void) { Dav1dSettings settings; "
            "dav1d_default_settings(&settings); return settings.n_threads < 0; }\n"
        ),
    ),
    "vid_stab": _OutputSpec(
        headers=("include/vid.stab/libvidstab.h", "include/vid.stab/motiondetect.h"),
        libraries=("lib/libvidstab.a",),
        pkg_config_file="vidstab.pc",
        pkg_config_name="vidstab",
        smoke_source=(
            "#include <vid.stab/libvidstab.h>\n"
            'int main(void) { (void)vsMotionDetectGetDefaultConfig("smoke"); return 0; }\n'
        ),
    ),
}


def verify_wave_sources(registry: ComponentRegistry, source_archives_dir: Path) -> dict[str, str]:
    """Verify all Wave-1 local archives without allowing a network fallback."""
    outcomes: dict[str, str] = {}
    for name in G4_WAVE1_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None:
            raise BuildError(name, "Wave-1 component is missing from components.yaml.")
        if not component.sha256:
            raise BuildError(name, "Wave-1 source has no registered SHA-256 checksum.")
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
                f"Source checksum mismatch for {archive}: expected {component.sha256}, got {digest}.",
            )
        outcomes[name] = digest
    return outcomes


def order_wave_components(
    registry: ComponentRegistry,
    selected_target_components: Sequence[Component],
    requested_names: Sequence[str] = G4_WAVE1_COMPONENTS,
    accepted_external_dependencies: Sequence[str] = (),
) -> list[Component]:
    """Validate and topologically order an explicit target-component subset."""
    if len(set(requested_names)) != len(requested_names):
        raise BuildError("G4 Wave 1", "The requested component subset contains duplicates.")
    available = {component.name: component for component in selected_target_components}
    requested = set(requested_names)
    missing = requested - available.keys()
    if missing:
        raise BuildError(
            "G4 Wave 1",
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
            raise BuildError("G4 Wave 1", f"Dependency cycle detected at {name}.")
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
                    f"Dependency {dependency_name!r} is outside Wave 1 and has not been "
                    "accepted for this identity.",
                )
        visiting.remove(name)
        visited.add(name)
        ordered.append(component)

    for name in requested_names:
        visit(name)
    return ordered


class Wave1TargetValidator:
    """Validate Wave-1 target outputs, metadata, archives, and linkability."""

    def __init__(self, environment: CrossEnvironment, identity_key: str):
        self.environment = environment
        self.prefix = environment.paths.target_prefix.resolve()
        self.manifest_path = environment.paths.workspace_root / _ACCEPTANCE_MANIFEST
        self.identity_key = identity_key

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        outputs: list[Path] = []
        for relative in spec.files:
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Expected target output is missing: {path}")
            self._require_inside_prefix(component.name, path)
            outputs.append(path)

        for path in (self.prefix / relative for relative in spec.libraries):
            self._validate_archive(component.name, path)

        self._validate_package(component.name, spec)
        for pkg_config_name in spec.pkg_config_names:
            self._link_smoke(component.name, spec, pkg_config_name)
        return [path.relative_to(self.prefix).as_posix() for path in outputs]

    def validate_prefix(self) -> None:
        """Reject Linux/host artifacts and contaminated target metadata."""
        for path in self.prefix.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            self._require_inside_prefix("target prefix", path)
            suffix = path.suffix.lower()
            if (
                suffix in {".so", ".dylib", ".dll", ".exe", ".o", ".obj"}
                or ".so." in path.name.lower()
                or "bin" in path.relative_to(self.prefix).parts
            ):
                raise BuildError(
                    "target prefix",
                    f"Unexpected shared library, executable, or object file installed: {path}",
                )
            try:
                with path.open("rb") as stream:
                    magic = stream.read(4)
            except OSError as exc:
                raise BuildError(
                    "target prefix", f"Cannot inspect installed file {path}: {exc}"
                ) from exc
            if magic == b"\x7fELF":
                raise BuildError(
                    "target prefix", f"Linux ELF artifact leaked into target prefix: {path}"
                )
            if suffix == ".a":
                self._validate_archive("target prefix", path)
            if suffix in {".pc", ".cmake", ".la"}:
                self._validate_metadata_paths(path)

    def manifest_matches(self, component: Component) -> bool:
        """Check the accepted manifest hashes before considering output reuse."""
        manifest = self._read_manifest()
        if manifest is None or manifest.get("identity_key") != self.identity_key:
            return False
        entries = manifest.get("components")
        entry = entries.get(component.name) if isinstance(entries, dict) else None
        if not isinstance(entry, dict) or entry.get("version") != component.version:
            return False
        hashes = entry.get("sha256")
        spec = self._spec(component)
        if not isinstance(hashes, dict) or set(hashes) != set(spec.files):
            return False
        for relative in spec.files:
            path = self.prefix / relative
            if not path.is_file():
                return False
            if self._sha256(path) != hashes.get(relative):
                return False
        return True

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
            "sha256": {relative: self._sha256(self.prefix / relative) for relative in outputs},
        }
        self._write_manifest(manifest)

    def forget_component(self, component: Component) -> None:
        manifest = self._read_manifest()
        if manifest is None or manifest.get("identity_key") != self.identity_key:
            return
        entries = manifest.get("components")
        if isinstance(entries, dict) and component.name in entries:
            del entries[component.name]
            self._write_manifest(manifest)

    def remove_component_outputs(self, component: Component) -> None:
        """Remove only this component's known outputs after a rejected build."""
        spec = self._spec(component)
        for relative in spec.cleanup_files:
            path = self.prefix / relative
            if path.is_file() or path.is_symlink():
                path.unlink()
        self.forget_component(component)

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        try:
            return _OUTPUTS[component.name]
        except KeyError as exc:
            raise BuildError(
                component.name, "No Wave-1 installed-output acceptance contract is defined."
            ) from exc

    def _validate_archive(self, component_name: str, archive: Path) -> None:
        tool = str(self.environment.target_tools.readobj)
        try:
            result = subprocess.run(
                [tool, "--file-headers", str(archive)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuildError(
                component_name, f"Cannot inspect target archive {archive}: {exc}"
            ) from exc
        if result.returncode:
            raise BuildError(
                component_name,
                f"LLVM-MinGW could not inspect {archive}: {(result.stderr or result.stdout).strip()}",
            )
        formats = re.findall(r"^Format:\s*(\S+)", result.stdout, flags=re.MULTILINE)
        accepted_formats = {"COFF-x86-64", "COFF-import-file-x86-64"}
        if not formats or any(value not in accepted_formats for value in formats):
            raise BuildError(
                component_name,
                f"Archive members are not all x86-64 Windows COFF objects: {archive}",
            )

    def _validate_package(self, component_name: str, spec: _OutputSpec) -> None:
        wrapper = str(self.environment.pkg_config_wrapper)
        env = dict(self.environment.compiler_environment)
        for pkg_config_name in spec.pkg_config_names:
            prefix = self._pkg_config(
                component_name,
                wrapper,
                env,
                ["--variable=prefix", pkg_config_name],
            )
            prefix_path = Path(prefix.strip())
            if not prefix_path.is_absolute() or not self._inside(prefix_path, self.prefix):
                raise BuildError(
                    component_name,
                    f"pkg-config reports a non-target prefix for " f"{pkg_config_name}: {prefix!r}",
                )
            for option in ("--cflags", "--libs"):
                flags = self._pkg_config(
                    component_name,
                    wrapper,
                    env,
                    ["--static", option, pkg_config_name],
                )
                self._validate_pkg_flags(component_name, flags)
        self._validate_pkg_config_dependencies(component_name, spec)

    def _validate_pkg_config_dependencies(self, component_name: str, spec: _OutputSpec) -> None:
        if not spec.pkg_config_dependencies:
            return
        path = self.prefix / "lib/pkgconfig" / spec.pkg_config_file
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BuildError(
                component_name, f"Cannot read target pkg-config metadata {path}: {exc}"
            ) from exc
        requires = " ".join(
            line.split(":", 1)[1]
            for line in content.splitlines()
            if line.startswith(("Requires:", "Requires.private:"))
        )
        for dependency in spec.pkg_config_dependencies:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(dependency)}(?![A-Za-z0-9_])", requires):
                continue
            raise BuildError(
                component_name,
                f"Target pkg-config metadata omits required dependency {dependency!r}: {path}",
            )

    def _link_smoke(self, component_name: str, spec: _OutputSpec, pkg_config_name: str) -> None:
        wrapper = str(self.environment.pkg_config_wrapper)
        env = dict(self.environment.compiler_environment)
        cflags = self._pkg_config(
            component_name, wrapper, env, ["--static", "--cflags", pkg_config_name]
        )
        libs = self._pkg_config(
            component_name, wrapper, env, ["--static", "--libs", pkg_config_name]
        )
        self._validate_pkg_flags(component_name, cflags)
        self._validate_pkg_flags(component_name, libs)
        cc = str(self.environment.target_tools.cc)
        with tempfile.TemporaryDirectory(
            prefix="g4-wave1-smoke-", dir=self.environment.paths.generated_files_root
        ) as directory:
            root = Path(directory)
            source = root / "consumer.c"
            executable = root / "consumer.exe"
            source.write_text(spec.smoke_source, encoding="utf-8")
            command = [
                cc,
                *shlex.split(env.get("CFLAGS", "")),
                *shlex.split(cflags),
                str(source),
                *shlex.split(libs),
                "-o",
                str(executable),
            ]
            self._run(component_name, command, env, "target consumer link smoke failed")
            self._validate_archive(component_name, executable)

    def _pkg_config(
        self, component_name: str, wrapper: str, env: Mapping[str, str], args: list[str]
    ) -> str:
        return self._run(
            component_name,
            [wrapper, *args],
            env,
            f"target pkg-config failed for {' '.join(args)}",
        )

    def _run(
        self,
        component_name: str,
        command: list[str],
        env: Mapping[str, str],
        description: str,
    ) -> str:
        try:
            result = subprocess.run(
                command,
                env=dict(env),
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuildError(component_name, f"{description}: {exc}") from exc
        if result.returncode:
            output = (result.stdout + result.stderr).strip()
            raise BuildError(
                component_name,
                f"{description}: {output[-4000:]}",
            )
        return result.stdout.strip()

    def _validate_pkg_flags(self, component_name: str, output: str) -> None:
        tokens = shlex.split(output)
        for index, token in enumerate(tokens):
            path_text: str | None = None
            if token.startswith("-I") or token.startswith("-L"):
                path_text = token[2:]
            elif token in ("-I", "-L") and index + 1 < len(tokens):
                path_text = tokens[index + 1]
            if path_text and Path(path_text).is_absolute():
                if not self._inside(Path(path_text), self.prefix):
                    raise BuildError(
                        component_name,
                        f"Target pkg-config returned a path outside the target prefix: {path_text}",
                    )

    def _validate_metadata_paths(self, path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise BuildError(
                "target prefix", f"Cannot read installed metadata {path}: {exc}"
            ) from exc
        references = re.findall(r"""(?<![\w$}:/.])(?:-[IL])?(/[^ \t\r\n"'(),;]+)""", content)
        for reference in references:
            candidate = reference[2:] if reference.startswith(("-I/", "-L/")) else reference
            if not self._inside(Path(candidate), self.prefix):
                raise BuildError(
                    "target prefix",
                    f"Installed metadata contains a non-target absolute path: {path}: {candidate}",
                )
        if re.search(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/mnt/[a-z]/)", content, re.IGNORECASE):
            raise BuildError(
                "target prefix", f"Installed metadata contains an MSYS/Windows path: {path}"
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
    def _sha256(path: Path) -> str:
        with path.open("rb") as installed:
            return hashlib.file_digest(installed, "sha256").hexdigest()

    def _read_manifest(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_manifest(self, manifest: Mapping[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.manifest_path)


def run_g4_wave1(app: Any, config: BuildConfig) -> int:
    """Build only the explicit Wave-1 subset through production builders."""
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW:
        raise BuildError("G4 Wave 1", "Wave 1 requires target_backend=linux-cross-mingw.")
    if config.allow_network_downloads:
        raise BuildError("G4 Wave 1", "Project-managed source networking must be disabled.")

    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    verified_sources = verify_wave_sources(app.registry, archives)
    print("Verified local Wave-1 source archives (network disabled):")
    for name, digest in verified_sources.items():
        print(f"  {name}: sha256={digest}")

    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
    except BuildError as exc:
        print(f"BLOCKED by G1/G2/G3 acceptance: {exc}")
        return 2

    selection = app._get_component_selection(config)
    state = app.state_manager.get()
    validator = Wave1TargetValidator(environment, session.identity.build_key)
    accepted_external: list[str] = []
    selected_names = set(G4_WAVE1_COMPONENTS)
    for name in G4_WAVE1_COMPONENTS:
        component = app.registry.get_by_name(name)
        if component is None:
            raise BuildError(name, "Wave-1 component is missing from the production registry.")
        for dependency_name in component.depends_on:
            if dependency_name in selected_names:
                continue
            dependency = app.registry.get_by_name(dependency_name)
            dependency_state = state.components.get(dependency_name)
            if (
                dependency is None
                or dependency_state is None
                or dependency_state.status != ComponentStatus.COMPLETED
                or not validator.manifest_matches(dependency)
            ):
                raise BuildError(
                    name,
                    f"Dependency {dependency_name!r} is outside Wave 1 and is not accepted "
                    "for this target identity.",
                )
            validator.validate_component(dependency)
            accepted_external.append(dependency_name)

    try:
        components = order_wave_components(
            app.registry,
            selection.target_components_to_build,
            accepted_external_dependencies=accepted_external,
        )
    except BuildError as exc:
        print(f"BLOCKED by Wave-1 selection: {exc}")
        return 2
    wave_tool_requirements = sorted(
        {tool for component in components for tool in component.requires_tools}
    )
    missing_tools = [name for name in wave_tool_requirements if not app._host_tool_available(name)]
    if missing_tools:
        print("BLOCKED: required WSL host tools unavailable: " + ", ".join(missing_tools))
        return 2

    print("Wave-1 target component execution order:")
    for component in components:
        print(f"  {component.name} {component.version} ({component.build_system.value})")

    builder = FFmpegBuilder(
        config,
        app.workspace,
        app.packages,
        app.state_manager,
        app.platform_detector,
        toolchain_env=app._component_toolchain_env(config, components),
        cross_environment=environment,
    )
    current: Component | None = None
    try:
        state.config = config.to_dict()
        state.total_steps = len(components)
        app.state_manager.save()
        for position, component in enumerate(components, 1):
            current = component
            print(f"\n=== G4 Wave 1 [{position}/{len(components)}] {component.name} ===")
            if build_or_reuse_component(component, builder, app.state_manager, validator):
                print(f"ACCEPTED {component.name}")
            else:
                print(f"REUSED accepted target outputs: {component.name}")

        validator.validate_prefix()
        print(f"\nG4 Wave 1 accepted for identity {session.identity.build_key}.")
        print("Installed target output inventory:")
        for component in components:
            print(f"  {component.name}:")
            for output in _OUTPUTS[component.name].files:
                print(f"    {output}")
        return 0
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        print(f"\nG4 Wave 1 stopped at {current.name if current else 'preflight'}: {exc}")
        return 1
    finally:
        builder.shutdown_downloads(wait=True)


def build_or_reuse_component(
    component: Component,
    builder: Any,
    state_manager: Any,
    validator: Wave1TargetValidator,
) -> bool:
    """Build and accept one component, or reuse its identity-validated outputs."""
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
            detail="Wave-1 target outputs require build or revalidation",
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
            detail="G4 Wave-1 build or target acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-1 build or acceptance failed: {exc}") from exc
