"""Bounded G4 Wave-3B cross-builds for the Ogg/Vorbis/Theora chain."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import BuildRole, Component, ComponentRegistry
from .config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g4_wave1 import G4_WAVE1_COMPONENTS, Wave1TargetValidator
from .g4_wave2 import G4_WAVE2_COMPONENTS, Wave2TargetValidator
from .g4_wave3a import G4_WAVE3A_COMPONENTS, Wave3ATargetValidator, _OutputSpec
from .state import ComponentStatus

G4_WAVE3B_COMPONENTS = ("libvorbis", "libtheora")

_OUTPUTS = {
    "libvorbis": (
        (
            "vorbis",
            "libvorbis.a",
            "vorbis/codec.h",
            "#include <vorbis/codec.h>\nint main(void) { vorbis_info i; vorbis_info_init(&i); vorbis_info_clear(&i); return 0; }\n",
            {"ogg"},
        ),
        (
            "vorbisenc",
            "libvorbisenc.a",
            "vorbis/vorbisenc.h",
            "#include <vorbis/vorbisenc.h>\nint main(void) { vorbis_info i; vorbis_info_init(&i); return vorbis_encode_init_vbr(&i, 2, 44100, .5f); }\n",
            {"vorbis"},
        ),
        (
            "vorbisfile",
            "libvorbisfile.a",
            "vorbis/vorbisfile.h",
            "#include <vorbis/vorbisfile.h>\nint main(void) { return ov_clear((OggVorbis_File *)0); }\n",
            {"vorbis"},
        ),
    ),
    "libtheora": (
        (
            "theora",
            "libtheora.a",
            "theora/theora.h",
            "#include <theora/theora.h>\nint main(void) { return theora_version_string() == 0; }\n",
            {"ogg"},
        ),
        (
            "theoradec",
            "libtheoradec.a",
            "theora/theoradec.h",
            "#include <theora/theoradec.h>\nint main(void) { th_info i; th_info_init(&i); th_info_clear(&i); return 0; }\n",
            {"ogg"},
        ),
        (
            "theoraenc",
            "libtheoraenc.a",
            "theora/theoraenc.h",
            "#include <theora/theoraenc.h>\nint main(void) { th_info i; th_info_init(&i); th_enc_ctx *e = th_encode_alloc(&i); th_encode_free(e); return 0; }\n",
            {"ogg", "theoradec"},
        ),
    ),
}


def verify_wave_sources(registry: ComponentRegistry, archives: Path) -> dict[str, str]:
    result = {}
    for name in G4_WAVE3B_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None or not component.sha256:
            raise BuildError(name, "Missing registered source or checksum.")
        path = archives / component.get_archive_filename()
        try:
            with path.open("rb") as stream:
                header = stream.read(128)
                stream.seek(0)
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise BuildError(name, f"Project-managed source unavailable: {path}: {exc}") from exc
        if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise BuildError(name, f"Unmaterialised Git LFS source: {path}")
        if digest.lower() != component.sha256.lower():
            raise BuildError(name, f"Source checksum mismatch: {path}")
        result[name] = digest
    return result


def order_wave_components(
    registry: ComponentRegistry, selected: Sequence[Component], accepted: Sequence[str]
) -> list[Component]:
    available = {item.name: item for item in selected}
    if len(available) != len(selected):
        raise BuildError("G4 Wave 3B", "Duplicate configured component.")
    ordered: list[Component] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise BuildError(name, "Dependency cycle in Wave 3B.")
        item = available.get(name)
        if item is None or item.build_role != BuildRole.TARGET_COMPONENT:
            raise BuildError(name, "Wave-3B target component is not configured.")
        expected = {"libvorbis": {"libogg"}, "libtheora": {"libogg", "libvorbis"}}
        if not expected[name].issubset(item.depends_on):
            raise BuildError(name, "Required Wave-3B dependency edge is missing.")
        visiting.add(name)
        for dependency in item.depends_on:
            declared = registry.get_by_name(dependency)
            if declared is None or declared.build_role != BuildRole.TARGET_COMPONENT:
                raise BuildError(name, f"Invalid target dependency {dependency!r}.")
            if dependency in G4_WAVE3B_COMPONENTS:
                visit(dependency)
            elif dependency not in accepted:
                raise BuildError(name, f"Dependency {dependency!r} has not been accepted.")
        visiting.remove(name)
        done.add(name)
        ordered.append(item)

    for name in G4_WAVE3B_COMPONENTS:
        visit(name)
    return ordered


class Wave3BTargetValidator(Wave3ATargetValidator):
    def __init__(self, environment: Any, identity_key: str):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave3b-acceptance.json"

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        entries = manifest.get("components") if manifest else None
        entry = entries.get(component.name) if isinstance(entries, dict) else None
        return (
            isinstance(entry, dict)
            and entry.get("recipe") == "library-only-v2"
            and super().manifest_matches(component)
        )

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        super().record_component(component, outputs)
        manifest = self._read_manifest()
        if manifest is None:
            raise BuildError(component.name, "Cannot read Wave-3B acceptance manifest.")
        manifest["components"][component.name]["recipe"] = "library-only-v2"
        self._write_manifest(manifest)

    def remove_component_outputs(self, component: Component) -> None:
        roots = tuple(Path(path) for path in self._spec(component).owned_paths)
        paths = {Path(path) for path in self._owned_outputs(component)}
        manifest = self._read_manifest()
        entries = manifest.get("components") if manifest else None
        entry = entries.get(component.name) if isinstance(entries, dict) else None
        if isinstance(entry, dict):
            for value in entry.get("outputs", []):
                if not isinstance(value, str):
                    continue
                relative = Path(value)
                if (
                    not relative.is_absolute()
                    and ".." not in relative.parts
                    and any(relative == root or relative.is_relative_to(root) for root in roots)
                ):
                    paths.add(relative)
        for relative in paths:
            path = self.prefix / relative
            if not self._inside(path.parent, self.prefix):
                raise BuildError(component.name, f"Output parent escaped target prefix: {path}")
            if path.is_file() or path.is_symlink():
                path.unlink()
        if manifest and manifest.get("identity_key") == self.identity_key:
            if isinstance(entries, dict):
                entries.pop(component.name, None)
                self._write_manifest(manifest)

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        if component.name not in _OUTPUTS:
            raise BuildError(component.name, "No Wave-3B output contract.")
        entries = _OUTPUTS[component.name]
        stem = "vorbis" if component.name == "libvorbis" else "theora"
        return _OutputSpec(
            pkg_config=entries[0][0],
            headers=tuple(f"include/{entry[2]}" for entry in entries)
            + (("include/theora/codec.h",) if stem == "theora" else ()),
            libraries=tuple(f"lib/{entry[1]}" for entry in entries),
            owned_paths=(f"include/{stem}",)
            + tuple(f"lib/{entry[1]}" for entry in entries)
            + tuple(f"lib/pkgconfig/{entry[0]}.pc" for entry in entries)
            + tuple(f"lib/{entry[1][:-2]}.la" for entry in entries)
            + (
                (f"share/doc/libvorbis-{component.version}", "share/aclocal/vorbis.m4")
                if stem == "vorbis"
                else ("share/doc/libtheora",)
            ),
            smoke_source=entries[0][3],
        )

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        for relative in spec.headers + spec.libraries:
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Expected target output missing: {path}")
            self._require_inside_prefix(component.name, path)
        for relative in spec.libraries:
            self._validate_archive(component.name, self.prefix / relative)
        env = dict(self.environment.compiler_environment)
        for package, library, header, source, requirements in _OUTPUTS[component.name]:
            pc = self.prefix / "lib" / "pkgconfig" / f"{package}.pc"
            if not pc.is_file():
                raise BuildError(component.name, f"Expected metadata missing: {pc}")
            self._require_inside_prefix(component.name, pc)
            Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(pc)
            fields = self._read_pc_fields(pc, component.name)
            found = self._requirement_names(fields.get("Requires", "")) | self._requirement_names(
                fields.get("Requires.private", "")
            )
            if found != requirements:
                raise BuildError(component.name, f"Unexpected {package}.pc dependencies: {found}")
            for variable in ("prefix", "includedir", "libdir"):
                value = self._pkg_config(
                    component.name, [f"--variable={variable}", package], env
                ).strip()
                path = Path(value)
                if not path.is_absolute() or not self._inside(path, self.prefix):
                    raise BuildError(component.name, f"Non-target {package} {variable}: {value!r}")
                if variable == "prefix" and path.resolve() != self.prefix:
                    raise BuildError(component.name, f"Incorrect {package} prefix: {value!r}")
            for option in ("--cflags", "--libs"):
                flags = self._pkg_config(component.name, ["--static", option, package], env)
                self._validate_target_flags(component.name, flags)
                if option == "--libs":
                    names = {token[2:] for token in shlex.split(flags) if token.startswith("-l")}
                    required = {library[3:-2], "ogg"}
                    if package in {"vorbisenc", "vorbisfile"}:
                        required.add("vorbis")
                    if package == "theoraenc":
                        required.add("theoradec")
                    if not required <= names:
                        raise BuildError(
                            component.name, f"Incomplete static {package} closure: {flags}"
                        )
            smoke = _OutputSpec(package, (f"include/{header}",), (f"lib/{library}",), (), source)
            self._link_smoke(component.name, smoke)
        if component.name == "libtheora":
            self._link_chain()
        return self._owned_outputs(component)

    def validate_prefix(self) -> None:
        super().validate_prefix()
        for path in self.prefix.rglob("*"):
            if (path.is_file() or path.is_symlink()) and ".so." in path.name.lower():
                raise BuildError("target prefix", f"Linux shared library leaked into {path}")

    def _link_chain(self) -> None:
        env = dict(self.environment.compiler_environment)
        packages = ["theoraenc", "vorbisenc"]
        cflags = self._pkg_config("libtheora", ["--static", "--cflags", *packages], env)
        libs = self._pkg_config("libtheora", ["--static", "--libs", *packages], env)
        self._validate_target_flags("libtheora", cflags)
        self._validate_target_flags("libtheora", libs)
        source_text = (
            "#include <theora/theoraenc.h>\n#include <vorbis/vorbisenc.h>\n"
            "int main(void) { th_info t; vorbis_info v; th_info_init(&t); "
            "vorbis_info_init(&v); th_enc_ctx *e = th_encode_alloc(&t); "
            "th_encode_free(e); int r = vorbis_encode_init_vbr(&v, 2, 44100, .5f); "
            "vorbis_info_clear(&v); th_info_clear(&t); return r; }\n"
        )
        with tempfile.TemporaryDirectory(
            prefix="g4-wave3b-chain-", dir=self.environment.paths.generated_files_root
        ) as directory:
            source = Path(directory) / "chain.c"
            executable = Path(directory) / "chain.exe"
            source.write_text(source_text, encoding="utf-8")
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    *shlex.split(env.get("CFLAGS", "")),
                    *shlex.split(cflags),
                    str(source),
                    *shlex.split(libs),
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
                    "libtheora",
                    "Ogg/Vorbis/Theora chain link failed: "
                    + (result.stderr or result.stdout)[-4000:],
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                "libtheora", executable
            )


def build_or_reuse(
    component: Component,
    builder: Any,
    manager: Any,
    validator: Wave3BTargetValidator,
    force: bool = False,
    on_rebuild: Callable[[], None] | None = None,
) -> bool:
    prior = manager.get().components.get(component.name)
    if (
        not force
        and prior
        and prior.status == ComponentStatus.COMPLETED
        and validator.manifest_matches(component)
    ):
        try:
            validator.validate_component(component)
            validator.validate_prefix()
            return False
        except BuildError:
            pass
    try:
        if on_rebuild:
            on_rebuild()
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-3B outputs require build or revalidation",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="target COFF, metadata and link acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-3B build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-3B build or acceptance failed: {exc}") from exc


def run_wave_components(
    ordered: Sequence[Component], builder: Any, manager: Any, validator: Wave3BTargetValidator
) -> tuple[list[str], list[str]]:
    if [component.name for component in ordered] != list(G4_WAVE3B_COMPONENTS):
        raise BuildError("G4 Wave 3B", "Execution must contain only libvorbis then libtheora.")
    built: list[str] = []
    reused: list[str] = []

    def invalidate_theora() -> None:
        manager.mark_component_status(
            "libtheora",
            ComponentStatus.PENDING,
            ordered[1].version,
            detail="Vorbis dependency requires rebuilding",
        )

    for item in ordered:
        changed = build_or_reuse(
            item,
            builder,
            manager,
            validator,
            force=item.name == "libtheora" and "libvorbis" in built,
            on_rebuild=invalidate_theora if item.name == "libvorbis" else None,
        )
        (built if changed else reused).append(item.name)
        print(
            f"Wave-3B {item.name}: {'built' if changed else 'revalidated and reused'}", flush=True
        )
    return built, reused


def run_g4_wave3b(app: Any, config: BuildConfig) -> int:
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW or config.allow_network_downloads:
        raise BuildError("G4 Wave 3B", "Cross backend and offline project sources are required.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    sources = verify_wave_sources(app.registry, archives)
    print(f"Verified Wave-3B source checksums: {sources}", flush=True)
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
        if (
            environment.paths.workspace_root.resolve() != session.workspace.resolve()
            or environment.paths.target_prefix.resolve() != (session.workspace / "prefix").resolve()
        ):
            raise BuildError("G4 Wave 3B", "Cross environment does not match identity prefix.")
        state = app.state_manager.get()
        accepted = []
        for names, validator_type in (
            (G4_WAVE1_COMPONENTS, Wave1TargetValidator),
            (G4_WAVE2_COMPONENTS, Wave2TargetValidator),
            (G4_WAVE3A_COMPONENTS, Wave3ATargetValidator),
        ):
            prior_validator = validator_type(environment, session.identity.build_key)
            for name in names:
                item = app.registry.get_by_name(name)
                status = state.components.get(name)
                if (
                    item is None
                    or status is None
                    or status.status != ComponentStatus.COMPLETED
                    or not prior_validator.manifest_matches(item)
                ):
                    raise BuildError(
                        name, "Accepted prior-wave output is not reusable for this identity."
                    )
                prior_validator.validate_component(item)
                accepted.append(name)
        ordered = order_wave_components(
            app.registry, app._get_component_selection(config).target_components_to_build, accepted
        )
        Wave2TargetValidator(environment, session.identity.build_key).validate_prefix()
    except BuildError as exc:
        print(f"BLOCKED by prior acceptance or selection: {exc}", flush=True)
        return 2
    validator = Wave3BTargetValidator(environment, session.identity.build_key)
    builder = FFmpegBuilder(
        config,
        app.workspace,
        app.packages,
        app.state_manager,
        app.platform_detector,
        toolchain_env=app._component_toolchain_env(config, ordered),
        cross_environment=environment,
    )
    try:
        built, reused = run_wave_components(ordered, builder, app.state_manager, validator)
        validator.validate_prefix()
        manifest = validator._read_manifest()
        if manifest is None:
            raise BuildError("G4 Wave 3B", "Wave-3B acceptance manifest is missing.")
        manifest.update(
            dependency_graph={item.name: list(item.depends_on) for item in ordered},
            source_sha256=sources,
            libogg_reused=True,
            built=built,
            reused=reused,
            wave4_started=False,
            ffmpeg_built=False,
        )
        validator._write_manifest(manifest)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-3B acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 3B built={built}, reused={reused}; libogg reused.", flush=True)
    return 0
