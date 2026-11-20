"""Bounded G4 Wave-3A cross-builds for the LV2 dependency chain."""

from __future__ import annotations

import hashlib
import json
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
from .g4_wave1 import G4_WAVE1_COMPONENTS, Wave1TargetValidator
from .g4_wave2 import (
    G4_WAVE2_COMPONENTS,
    Wave2TargetValidator,
)
from .state import ComponentStatus

G4_WAVE3A_COMPONENTS = ("lv2", "serd", "zix", "sord", "sratom", "lilv")
_ACCEPTANCE_MANIFEST = "g4-wave3a-acceptance.json"
_EXPECTED_REQUIRES: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "lv2": ((), ()),
    "serd-0": ((), ()),
    "zix-0": ((), ()),
    "sord-0": (("serd-0", "zix-0"), ()),
    "sratom-0": (("serd-0", "sord-0", "lv2"), ()),
    "lilv-0": (("lv2", "serd-0", "sord-0", "sratom-0", "zix-0"), ()),
}
_PACKAGE_LIBRARIES = {
    "serd-0": "serd-0",
    "zix-0": "zix-0",
    "sord-0": "sord-0",
    "sratom-0": "sratom-0",
    "lilv-0": "lilv-0",
}


@dataclass(frozen=True)
class _OutputSpec:
    pkg_config: str
    headers: tuple[str, ...]
    libraries: tuple[str, ...]
    owned_paths: tuple[str, ...]
    smoke_source: str
    data_files: tuple[str, ...] = ()
    rejected_outputs: tuple[str, ...] = ()


_OUTPUTS: Mapping[str, _OutputSpec] = {
    "lv2": _OutputSpec(
        pkg_config="lv2",
        headers=(
            "include/lv2.h",
            "include/lv2/core/lv2.h",
            "include/lv2/atom/atom.h",
        ),
        libraries=(),
        owned_paths=(
            "include/lv2.h",
            "include/lv2",
            "lib/pkgconfig/lv2.pc",
            "lib/lv2",
        ),
        smoke_source=(
            "#include <lv2/core/lv2.h>\n"
            "int main(void) { const LV2_Descriptor *descriptor = 0; "
            "return descriptor != 0; }\n"
        ),
        data_files=("lib/lv2/schemas.lv2/manifest.ttl",),
        rejected_outputs=("bin/lv2_validate",),
    ),
    "serd": _OutputSpec(
        pkg_config="serd-0",
        headers=("include/serd-0/serd/serd.h",),
        libraries=("lib/libserd-0.a",),
        owned_paths=("include/serd-0", "lib/libserd-0.a", "lib/pkgconfig/serd-0.pc"),
        smoke_source=("#include <serd/serd.h>\n" "int main(void) { serd_free(0); return 0; }\n"),
    ),
    "zix": _OutputSpec(
        pkg_config="zix-0",
        headers=("include/zix-0/zix/digest.h", "include/zix-0/zix/zix.h"),
        libraries=("lib/libzix-0.a",),
        owned_paths=("include/zix-0", "lib/libzix-0.a", "lib/pkgconfig/zix-0.pc"),
        smoke_source=(
            "#include <zix/digest.h>\n" "int main(void) { return (int)zix_digest32(0, 0, 0); }\n"
        ),
    ),
    "sord": _OutputSpec(
        pkg_config="sord-0",
        headers=("include/sord-0/sord/sord.h",),
        libraries=("lib/libsord-0.a",),
        owned_paths=("include/sord-0", "lib/libsord-0.a", "lib/pkgconfig/sord-0.pc"),
        smoke_source=(
            "#include <sord/sord.h>\n"
            "int main(void) { SordWorld *world = sord_world_new(); "
            "sord_world_free(world); return 0; }\n"
        ),
    ),
    "sratom": _OutputSpec(
        pkg_config="sratom-0",
        headers=("include/sratom-0/sratom/sratom.h",),
        libraries=("lib/libsratom-0.a",),
        owned_paths=("include/sratom-0", "lib/libsratom-0.a", "lib/pkgconfig/sratom-0.pc"),
        smoke_source=(
            "#include <sratom/sratom.h>\n"
            "int main(void) { Sratom *sratom = sratom_new(0); "
            "sratom_free(sratom); return 0; }\n"
        ),
    ),
    "lilv": _OutputSpec(
        pkg_config="lilv-0",
        headers=("include/lilv-0/lilv/lilv.h",),
        libraries=("lib/liblilv-0.a",),
        owned_paths=("include/lilv-0", "lib/liblilv-0.a", "lib/pkgconfig/lilv-0.pc"),
        smoke_source=(
            "#include <lilv/lilv.h>\n"
            "int main(void) { LilvWorld *world = lilv_world_new(); "
            "lilv_world_free(world); return 0; }\n"
        ),
    ),
}


def verify_wave_sources(registry: ComponentRegistry, source_archives_dir: Path) -> dict[str, str]:
    """Verify every registered Wave-3A archive before preparing or building."""
    outcomes: dict[str, str] = {}
    for name in G4_WAVE3A_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None:
            raise BuildError(name, "Wave-3A component is missing from components.yaml.")
        if not component.sha256:
            raise BuildError(name, "Wave-3A source has no registered SHA-256 checksum.")
        archive = source_archives_dir / component.get_archive_filename()
        try:
            with archive.open("rb") as stream:
                header = stream.read(128)
                stream.seek(0)
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
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
    """Validate and topologically order exactly the Wave-3A target subset."""
    available = {component.name: component for component in selected_target_components}
    requested = set(G4_WAVE3A_COMPONENTS)
    if len(available) != len(selected_target_components):
        raise BuildError("G4 Wave 3A", "Configured target selection contains duplicate names.")
    missing = requested - available.keys()
    if missing:
        raise BuildError(
            "G4 Wave 3A",
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
            raise BuildError("G4 Wave 3A", f"Dependency cycle detected at {name}.")
        visiting.add(name)
        component = available[name]
        if component.build_role != BuildRole.TARGET_COMPONENT:
            raise BuildError(component.name, "Wave-3A entries must be TARGET_COMPONENTs.")
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
                    f"Dependency {dependency_name!r} is outside Wave 3A and has not been "
                    "accepted for this identity.",
                )
        visiting.remove(name)
        visited.add(name)
        ordered.append(component)

    for name in G4_WAVE3A_COMPONENTS:
        visit(name)
    if {component.name for component in ordered} != requested:
        raise BuildError("G4 Wave 3A", "Topological order escaped the exact Wave-3A subset.")
    return ordered


class Wave3ATargetValidator:
    """Validate Wave-3A outputs, COFF archives, target metadata, and linkability."""

    def __init__(self, environment: CrossEnvironment, identity_key: str):
        self.environment = environment
        self.prefix = environment.paths.target_prefix.resolve()
        self.workspace = environment.paths.workspace_root
        self.manifest_path = self.workspace / _ACCEPTANCE_MANIFEST
        self.identity_key = identity_key

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        for relative in spec.rejected_outputs:
            path = self.prefix / relative
            if path.exists():
                raise BuildError(
                    component.name, f"Non-required target utility was installed: {path}"
                )
        for relative in (
            spec.headers
            + spec.libraries
            + spec.data_files
            + (f"lib/pkgconfig/{self._pc_filename(spec)}",)
        ):
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Expected target output is missing: {path}")
            self._require_inside_prefix(component.name, path)
        for relative in spec.libraries:
            self._validate_archive(component.name, self.prefix / relative)
        self._validate_pkg_config(component.name, spec)
        self._link_smoke(component.name, spec)
        return self._owned_outputs(component)

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        if (
            manifest is None
            or manifest.get("identity_key") != self.identity_key
            or manifest.get("target_prefix") != str(self.prefix)
        ):
            return False
        entries = manifest.get("components")
        entry = entries.get(component.name) if isinstance(entries, dict) else None
        if not isinstance(entry, dict) or entry.get("version") != component.version:
            return False
        outputs = self._owned_outputs(component)
        hashes = entry.get("sha256")
        if not isinstance(hashes, dict) or set(hashes) != set(outputs):
            return False
        return all(
            (self.prefix / relative).is_file()
            and self._sha256(self.prefix / relative) == hashes.get(relative)
            for relative in outputs
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

    def component_acceptance(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        if (
            manifest is None
            or manifest.get("identity_key") != self.identity_key
            or manifest.get("target_prefix") != str(self.prefix)
        ):
            raise BuildError("G4 Wave 3A", "Wave-3A component acceptance manifest is missing.")
        components = manifest.get("components")
        if not isinstance(components, dict) or set(components) != set(G4_WAVE3A_COMPONENTS):
            raise BuildError("G4 Wave 3A", "Wave-3A component acceptance data is incomplete.")
        return components

    def remove_component_outputs(self, component: Component) -> None:
        manifest = self._read_manifest()
        entries = manifest.get("components") if manifest else None
        entry = entries.get(component.name) if isinstance(entries, dict) else None
        old_outputs = entry.get("outputs", []) if isinstance(entry, dict) else []
        safe_old_outputs = {
            item
            for item in old_outputs
            if isinstance(item, str)
            and not Path(item).is_absolute()
            and ".." not in Path(item).parts
        }
        rejected_outputs = set(self._spec(component).rejected_outputs)
        for relative in {*self._owned_outputs(component), *safe_old_outputs, *rejected_outputs}:
            path = self.prefix / relative
            if path.is_file() or path.is_symlink():
                path.unlink()
        if manifest and manifest.get("identity_key") == self.identity_key:
            entries = manifest.get("components")
            if isinstance(entries, dict):
                entries.pop(component.name, None)
                self._write_manifest(manifest)

    def validate_prefix(self) -> None:
        Wave2TargetValidator(self.environment, self.identity_key).validate_prefix()

    def _validate_pkg_config(self, component_name: str, spec: _OutputSpec) -> None:
        pc_file = self.prefix / "lib" / "pkgconfig" / self._pc_filename(spec)
        Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(pc_file)
        fields = self._read_pc_fields(pc_file, component_name)
        requires, requires_private = _EXPECTED_REQUIRES[spec.pkg_config]
        if self._requirement_names(fields.get("Requires", "")) != set(requires):
            raise BuildError(
                component_name,
                f"{pc_file.name} has unexpected Requires metadata: "
                f"{fields.get('Requires', '')!r}.",
            )
        if self._requirement_names(fields.get("Requires.private", "")) != set(requires_private):
            raise BuildError(
                component_name,
                f"{pc_file.name} has unexpected Requires.private metadata: "
                f"{fields.get('Requires.private', '')!r}.",
            )

        env = dict(self.environment.compiler_environment)
        variables = ["--variable=prefix", "--variable=includedir"]
        if spec.libraries:
            variables.append("--variable=libdir")
        resolved_variables: dict[str, Path] = {}
        for option in variables:
            value = self._pkg_config(component_name, [option, spec.pkg_config], env).strip()
            target_path = Path(value)
            if not target_path.is_absolute() or not self._inside(target_path, self.prefix):
                raise BuildError(
                    component_name,
                    f"Target pkg-config reports a non-target {option.split('=')[1]}: {value!r}.",
                )
            resolved_variables[option.split("=")[1]] = target_path.resolve()
        if resolved_variables.get("prefix") != self.prefix:
            raise BuildError(
                component_name,
                f"Target pkg-config reports a different prefix: "
                f"{resolved_variables.get('prefix')}.",
            )
        self._pkg_config(component_name, ["--exists", spec.pkg_config], env)
        for dependency in (*requires, *requires_private):
            self._pkg_config(component_name, ["--exists", dependency], env)

        cflags = self._pkg_config(component_name, ["--static", "--cflags", spec.pkg_config], env)
        libs = self._pkg_config(component_name, ["--static", "--libs", spec.pkg_config], env)
        self._validate_target_flags(component_name, cflags)
        self._validate_target_flags(component_name, libs)
        libraries = set(re.findall(r"(?:^|\s)-l([A-Za-z0-9_+.-]+)", libs))
        for dependency in (*requires, *requires_private):
            library = _PACKAGE_LIBRARIES.get(dependency)
            if library and library not in libraries:
                raise BuildError(
                    component_name,
                    f"Static pkg-config closure for {spec.pkg_config} omits -l{library}.",
                )
        for relative in spec.libraries:
            library_name = Path(relative).name.removeprefix("lib").removesuffix(".a")
            if library_name not in libraries:
                raise BuildError(
                    component_name,
                    f"Static pkg-config output for {spec.pkg_config} omits -l{library_name}.",
                )

    def _link_smoke(self, component_name: str, spec: _OutputSpec) -> None:
        env = dict(self.environment.compiler_environment)
        flags = shlex.split(
            self._pkg_config(component_name, ["--static", "--cflags", spec.pkg_config], env)
        )
        libraries = shlex.split(
            self._pkg_config(component_name, ["--static", "--libs", spec.pkg_config], env)
        )
        with tempfile.TemporaryDirectory(
            prefix=f"g4-wave3a-{component_name}-", dir=self.environment.paths.generated_files_root
        ) as directory:
            source = Path(directory) / f"{component_name}-consumer.c"
            executable = Path(directory) / f"{component_name}-consumer.exe"
            source.write_text(spec.smoke_source, encoding="utf-8")
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    *shlex.split(env.get("CFLAGS", "")),
                    *flags,
                    str(source),
                    *libraries,
                    *shlex.split(env.get("LDFLAGS", "")),
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
                    "target metadata link smoke failed: "
                    + (result.stderr or result.stdout)[-4000:],
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component_name, executable
            )

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
        if not formats or any(value != "COFF-x86-64" for value in formats):
            raise BuildError(
                component_name,
                f"Archive members are not all x86-64 Windows COFF objects: {archive}",
            )

    def _owned_outputs(self, component: Component) -> list[str]:
        spec = self._spec(component)
        paths: set[Path] = set()
        for relative in spec.owned_paths:
            root = self.prefix / relative
            if root.is_file() or root.is_symlink():
                paths.add(root)
            elif root.is_dir():
                paths.update(
                    path for path in root.rglob("*") if path.is_file() or path.is_symlink()
                )
        return sorted(path.relative_to(self.prefix).as_posix() for path in paths)

    @staticmethod
    def _pc_filename(spec: _OutputSpec) -> str:
        return f"{spec.pkg_config}.pc"

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        try:
            return _OUTPUTS[component.name]
        except KeyError as exc:
            raise BuildError(component.name, "No Wave-3A output contract is defined.") from exc

    @staticmethod
    def _read_pc_fields(pc_file: Path, component_name: str) -> dict[str, str]:
        try:
            lines = pc_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise BuildError(
                component_name, f"Cannot read pkg-config metadata {pc_file}: {exc}"
            ) from exc
        fields: dict[str, str] = {}
        for line in lines:
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key in {
                "Name",
                "Description",
                "Version",
                "Requires",
                "Requires.private",
                "Libs",
                "Libs.private",
                "Cflags",
            }:
                if key in fields:
                    raise BuildError(component_name, f"Duplicate {key} field in {pc_file}.")
                fields[key] = value.strip()
        for required in ("Name", "Version", "Cflags"):
            if not fields.get(required):
                raise BuildError(component_name, f"Missing {required} field in {pc_file}.")
        return fields

    @staticmethod
    def _requirement_names(value: str) -> set[str]:
        return {item.strip().split()[0] for item in value.split(",") if item.strip()}

    def _pkg_config(self, component: str, args: Sequence[str], env: dict[str, str]) -> str:
        result = subprocess.run(
            [str(self.environment.pkg_config_wrapper), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode:
            raise BuildError(
                component,
                "target pkg-config query failed: "
                + (result.stderr or result.stdout).strip()[-3000:],
            )
        return result.stdout

    def _validate_target_flags(self, component: str, output: str) -> None:
        for token in shlex.split(output):
            path_value = token[2:] if token.startswith(("-I", "-L")) else ""
            if path_value:
                path = Path(path_value)
                if not path.is_absolute() or not self._inside(path, self.prefix):
                    raise BuildError(component, f"pkg-config emitted a non-target path: {token!r}.")
            elif any(path in token for path in ("/usr/", "/usr ", "/lib/x86_64-linux-gnu")):
                raise BuildError(component, f"pkg-config emitted a host path: {token!r}.")

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _require_inside_prefix(self, component: str, path: Path) -> None:
        if not self._inside(path, self.prefix):
            raise BuildError(component, f"Installed output escaped the target prefix: {path}")

    @staticmethod
    def _sha256(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _read_manifest(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.manifest_path)


def reject_host_pkg_config_fallback(environment: CrossEnvironment) -> None:
    """Prove a host-only package cannot satisfy target pkg-config discovery."""
    with tempfile.TemporaryDirectory(
        prefix="g4-wave3a-host-pc-", dir=environment.paths.generated_files_root
    ) as directory:
        host_metadata = Path(directory) / "pkgconfig"
        host_metadata.mkdir()
        (host_metadata / "g4-wave3a-host-only.pc").write_text(
            "\n".join(
                (
                    "prefix=/host/only",
                    "Name: g4-wave3a-host-only",
                    "Description: host-only negative fixture",
                    "Version: 1",
                    "Libs: -L/host/only/lib -lhost-only",
                    "Cflags: -I/host/only/include",
                    "",
                )
            ),
            encoding="utf-8",
        )
        env = dict(environment.compiler_environment)
        env["PKG_CONFIG_PATH"] = str(host_metadata)
        result = subprocess.run(
            [str(environment.pkg_config_wrapper), "--exists", "g4-wave3a-host-only"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode == 0:
            raise BuildError(
                "G4 Wave 3A",
                "Target pkg-config accepted a host-only package absent from the target prefix.",
            )


def build_or_reuse_component(
    component: Component,
    builder: Any,
    state_manager: Any,
    validator: Wave3ATargetValidator,
) -> bool:
    """Build/accept a Wave-3A output set or revalidate its same-identity manifest."""
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
            detail="Wave-3A target outputs require build or revalidation",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        state_manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="installed target outputs and COFF/pkg-config/link acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        validator.remove_component_outputs(component)
        state_manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="G4 Wave-3A build or target acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-3A build or acceptance failed: {exc}") from exc


def run_wave_components(
    ordered: Sequence[Component],
    builder: Any,
    state_manager: Any,
    validator: Wave3ATargetValidator,
) -> tuple[list[str], list[str]]:
    """Execute only the already-validated topological Wave-3A list."""
    names = [component.name for component in ordered]
    if len(names) != len(G4_WAVE3A_COMPONENTS) or set(names) != set(G4_WAVE3A_COMPONENTS):
        raise BuildError(
            "G4 Wave 3A", "Execution list must contain exactly the six Wave-3A entries."
        )
    positions = {name: index for index, name in enumerate(names)}
    for component in ordered:
        for dependency in component.depends_on:
            if dependency in positions and positions[dependency] >= positions[component.name]:
                raise BuildError(
                    component.name,
                    f"Wave-3A dependency {dependency!r} is not ordered before its consumer.",
                )
    accepted: list[str] = []
    built: list[str] = []
    for position, component in enumerate(ordered, 1):
        print(
            f"\n=== G4 Wave 3A [{position}/{len(ordered)}] "
            f"{component.name} {component.version} ===",
            flush=True,
        )
        if build_or_reuse_component(component, builder, state_manager, validator):
            built.append(component.name)
        accepted.append(component.name)
    return accepted, built


def run_g4_wave3a(app: Any, config: BuildConfig) -> int:
    """Build and accept only the fixed six-component LV2 cross-build subset."""
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW:
        raise BuildError("G4 Wave 3A", "Wave 3A requires target_backend=linux-cross-mingw.")
    if config.allow_network_downloads:
        raise BuildError(
            "G4 Wave 3A",
            "Wave 3A requires project-managed local sources with network downloads disabled.",
        )
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    verified_sources = verify_wave_sources(app.registry, archives)
    print("Verified local Wave-3A source archives:")
    for name, digest in verified_sources.items():
        print(f"  {name}: sha256={digest}")

    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
    except BuildError as exc:
        print(f"BLOCKED by G1/G2/G3 acceptance: {exc}", flush=True)
        return 2
    if (
        environment.paths.workspace_root.resolve() != session.workspace.resolve()
        or environment.paths.target_prefix.resolve()
        != session.workspace.joinpath("prefix").resolve()
    ):
        raise BuildError(
            "G4 Wave 3A",
            "Prepared cross environment does not match the authorized identity workspace/prefix.",
        )

    state = app.state_manager.get()
    wave1 = Wave1TargetValidator(environment, session.identity.build_key)
    wave2 = Wave2TargetValidator(environment, session.identity.build_key)
    accepted_wave1: list[str] = []
    accepted_wave2: list[str] = []
    try:
        for name in G4_WAVE1_COMPONENTS:
            component = app.registry.get_by_name(name)
            current = state.components.get(name)
            if (
                component is None
                or current is None
                or current.status != ComponentStatus.COMPLETED
                or not wave1.manifest_matches(component)
            ):
                raise BuildError(
                    name, "Accepted Wave-1 dependency is not reusable for this identity."
                )
            wave1.validate_component(component)
            accepted_wave1.append(name)
        for name in G4_WAVE2_COMPONENTS:
            component = app.registry.get_by_name(name)
            current = state.components.get(name)
            if (
                component is None
                or current is None
                or current.status != ComponentStatus.COMPLETED
                or not wave2.manifest_matches(component)
            ):
                raise BuildError(
                    name, "Accepted Wave-2 dependency is not reusable for this identity."
                )
            wave2.validate_component(component)
            accepted_wave2.append(name)
        wave2.validate_prefix()
        ordered = order_wave_components(
            app.registry,
            app._get_component_selection(config).target_components_to_build,
            (*accepted_wave1, *accepted_wave2),
        )
    except BuildError as exc:
        print(f"BLOCKED by accepted prior waves or exact Wave-3A selection: {exc}", flush=True)
        return 2

    required_tools = sorted({tool for component in ordered for tool in component.requires_tools})
    missing_tools = [name for name in required_tools if not app._host_tool_available(name)]
    if missing_tools:
        print(
            "BLOCKED: required WSL host tools unavailable: " + ", ".join(missing_tools), flush=True
        )
        return 2

    print("Wave-3A target component execution order:")
    for component in ordered:
        print(f"  {component.name} {component.version} ({component.build_system.value})")
    builder = FFmpegBuilder(
        config,
        app.workspace,
        app.packages,
        app.state_manager,
        app.platform_detector,
        toolchain_env=app._component_toolchain_env(config, ordered),
        cross_environment=environment,
    )
    validator = Wave3ATargetValidator(environment, session.identity.build_key)
    state.config = config.to_dict()
    state.total_steps = len(ordered)
    app.state_manager.save()

    try:
        reject_host_pkg_config_fallback(environment)
        accepted, built = run_wave_components(ordered, builder, app.state_manager, validator)
        reused = [name for name in accepted if name not in set(built)]
        validator.validate_prefix()
        accepted_manifest = {
            "schema_version": 1,
            "identity_key": session.identity.build_key,
            "target_prefix": str(environment.paths.target_prefix),
            "dependency_graph": {
                component.name: list(component.depends_on) for component in ordered
            },
            "source_sha256": verified_sources,
            "components": validator.component_acceptance(),
            "wave1_reused": accepted_wave1,
            "wave2_reused": accepted_wave2,
            "wave3a_built": built,
            "wave3a_reused": reused,
            "wave3a_accepted": accepted,
            "wave3b_started": False,
            "ffmpeg_built": False,
        }
        validator._write_manifest(accepted_manifest)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-3A build or target acceptance failed: {exc}", flush=True)
        return 1

    print(
        "PASS: exact Wave-3A subset accepted; "
        f"built={built or 'none'}, reused={reused or 'none'}.",
        flush=True,
    )
    print(f"Acceptance manifest: {validator.manifest_path}", flush=True)
    print(
        "Wave 3B and later waves were not started; FFmpeg was not built.",
        flush=True,
    )
    return 0
