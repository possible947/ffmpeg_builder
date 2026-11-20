"""Bounded production CMake acceptance for the first two AV1 codecs."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import BuildRole, Component, ComponentRegistry
from .config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g4_wave1 import Wave1TargetValidator
from .g4_wave2 import Wave2TargetValidator
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave3b import Wave3BTargetValidator
from .state import ComponentStatus

G4_WAVE4A_COMPONENTS = ("svtav1", "av1")
_OUTPUTS = {
    "svtav1": _OutputSpec(
        "SvtAv1Enc",
        ("include/svt-av1/EbSvtAv1Enc.h",),
        ("lib/libSvtAv1Enc.a",),
        (
            "include/svt-av1",
            "lib/libSvtAv1Enc.a",
            "lib/pkgconfig/SvtAv1Enc.pc",
            "lib/cmake/SVT-AV1",
        ),
        "#include <EbSvtAv1Enc.h>\nint main(void) { EbComponentType *h=0; "
        "EbSvtAv1EncConfiguration c; return (int)svt_av1_enc_init_handle(&h, &c); }\n",
    ),
    "av1": _OutputSpec(
        "aom",
        (
            "include/aom/aom_codec.h",
            "include/aom/aom_encoder.h",
            "include/aom/aomcx.h",
            "include/aom/aom_decoder.h",
            "include/aom/aomdx.h",
        ),
        ("lib/libaom.a",),
        ("include/aom", "lib/libaom.a", "lib/pkgconfig/aom.pc", "lib/cmake/AOM"),
        "#include <aom/aom_encoder.h>\n#include <aom/aomcx.h>\n"
        "int main(void) { aom_codec_ctx_t c; aom_codec_enc_cfg_t cfg; "
        "return aom_codec_enc_config_default(aom_codec_av1_cx(), &cfg, 0) "
        "|| aom_codec_enc_init(&c, aom_codec_av1_cx(), &cfg, 0); }\n",
    ),
}


def verify_sources(registry: ComponentRegistry, archives: Path) -> dict[str, str]:
    result = {}
    for name in G4_WAVE4A_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None or not component.sha256:
            raise BuildError(name, "Missing registered source/checksum.")
        path = archives / component.get_archive_filename()
        try:
            with path.open("rb") as stream:
                header = stream.read(128)
                stream.seek(0)
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise BuildError(name, f"Project source unavailable: {path}: {exc}") from exc
        if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise BuildError(name, f"Source is an unresolved Git LFS pointer: {path}")
        if digest.lower() != component.sha256.lower():
            raise BuildError(name, f"Source checksum mismatch: {path}")
        result[name] = digest
    return result


def select_components(
    registry: ComponentRegistry, selected: Sequence[Component]
) -> list[Component]:
    available = {item.name: item for item in selected}
    if len(available) != len(selected):
        raise BuildError("G4 Wave 4A", "Duplicate configured components.")
    result = []
    for name in G4_WAVE4A_COMPONENTS:
        component = available.get(name)
        if component is None or component.build_role != BuildRole.TARGET_COMPONENT:
            raise BuildError(name, "Component is not configured for the target.")
        if component.depends_on:
            raise BuildError(name, f"Unexpected declared dependencies: {component.depends_on}")
        if registry.get_by_name(name) is None:
            raise BuildError(name, "Component not registered.")
        result.append(component)
    return result


class Wave4ATargetValidator(Wave3BTargetValidator):
    def __init__(self, environment: Any, identity_key: str):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave4a-acceptance.json"

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        if component.name not in _OUTPUTS:
            raise BuildError(component.name, "No Wave-4A output contract.")
        return _OUTPUTS[component.name]

    def manifest_matches(self, component: Component) -> bool:
        return Wave3ATargetValidator.manifest_matches(self, component)

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        cmake = f"lib/cmake/{'SVT-AV1/SVT-AV1Config.cmake' if component.name == 'svtav1' else 'AOM/AOMConfig.cmake'}"
        for relative in (
            *spec.headers,
            *spec.libraries,
            f"lib/pkgconfig/{spec.pkg_config}.pc",
            cmake,
        ):
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing required target output: {path}")
            self._require_inside_prefix(component.name, path)
        for relative in spec.libraries:
            self._validate_archive(component.name, self.prefix / relative)
        pc = self.prefix / "lib/pkgconfig" / f"{spec.pkg_config}.pc"
        Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(pc)
        env = dict(self.environment.compiler_environment)
        for variable in ("prefix", "includedir", "libdir"):
            value = self._pkg_config(
                component.name, [f"--variable={variable}", spec.pkg_config], env
            ).strip()
            path = Path(value)
            if not path.is_absolute() or not self._inside(path, self.prefix):
                raise BuildError(component.name, f"Non-target pkg-config {variable}: {value!r}")
        for option in ("--cflags", "--libs"):
            flags = self._pkg_config(component.name, ["--static", option, spec.pkg_config], env)
            self._validate_target_flags(component.name, flags)
            if option == "--libs":
                missing = set(component.target_link_metadata.consumer_link_requirements) - set(
                    shlex.split(flags)
                )
                if missing:
                    raise BuildError(component.name, f"Static metadata omits {sorted(missing)}")
        self._validate_build_configuration(component)
        self._link_with_metadata(component, spec, env)
        return self._owned_outputs(component)

    def _validate_build_configuration(self, component: Component) -> None:
        build_dir = (
            self.environment.paths.packages_root
            / component.get_target_dir()
            / (component.workdir or "")
        )
        try:
            cache = (build_dir / "CMakeCache.txt").read_text(encoding="utf-8")
            system = next(build_dir.glob("CMakeFiles/*/CMakeSystem.cmake")).read_text(
                encoding="utf-8"
            )
        except (OSError, StopIteration) as exc:
            raise BuildError(component.name, f"Missing CMake cross-build evidence: {exc}") from exc
        if (
            'set(CMAKE_SYSTEM_NAME "Windows")' not in system
            or 'set(CMAKE_CROSSCOMPILING "TRUE")' not in system
            or "CMAKE_ASM_NASM_OBJECT_FORMAT:UNINITIALIZED=win64" not in cache
            or str(self.environment.cmake_toolchain_file) not in cache
        ):
            raise BuildError(
                component.name, "CMake did not use the Windows cross/assembler contract."
            )
        expected_flags = shlex.split(self.environment.compiler_environment.get("CFLAGS", ""))
        for flag in ("-march=native", "-mtune=native"):
            if flag in expected_flags and flag not in cache:
                raise BuildError(component.name, f"Missing target CPU flag {flag}.")
        commands = subprocess.run(
            ["ninja", "-C", str(build_dir), "-t", "commands"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if commands.returncode or not any(
            "nasm " in line and "-f win64" in line for line in commands.stdout.splitlines()
        ):
            raise BuildError(component.name, "No host NASM win64 build command found.")
        objects = list(build_dir.rglob("*.asm.obj"))
        if not objects:
            raise BuildError(component.name, "No generated assembly object found.")
        result = subprocess.run(
            [str(self.environment.target_tools.readobj), "--file-headers", str(objects[0])],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode or "Format: COFF-x86-64" not in result.stdout:
            raise BuildError(component.name, "Generated NASM object is not Windows x86-64 COFF.")

    def _link_with_metadata(
        self, component: Component, spec: _OutputSpec, env: dict[str, str]
    ) -> None:
        flags = self._pkg_config(component.name, ["--static", "--cflags", spec.pkg_config], env)
        libs = self._pkg_config(component.name, ["--static", "--libs", spec.pkg_config], env)
        # Do not let the globally enabled OpenMP flag hide codec-specific requirements.
        cflags = [v for v in shlex.split(env.get("CFLAGS", "")) if v != "-fopenmp"]
        ldflags = [v for v in shlex.split(env.get("LDFLAGS", "")) if v != "-fopenmp"]
        with tempfile.TemporaryDirectory(
            prefix="g4-wave4a-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            source, executable = Path(tmp) / "smoke.c", Path(tmp) / "smoke.exe"
            source.write_text(spec.smoke_source, encoding="utf-8")
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    *cflags,
                    *shlex.split(flags),
                    str(source),
                    *shlex.split(libs),
                    *ldflags,
                    "-o",
                    str(executable),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode:
                raise BuildError(
                    component.name,
                    "Static target link smoke failed: " + (result.stderr or result.stdout)[-5000:],
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component.name, executable
            )
            result = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(executable)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode:
                raise BuildError(component.name, "Cannot inspect target executable imports.")
            actual = {
                line.strip().split(": ", 1)[1].lower()
                for line in result.stdout.splitlines()
                if line.strip().startswith("Name: ")
            }
            required = {
                dep.name.lower() for dep in component.target_link_metadata.runtime_dependencies
            }
            if not required.issubset(actual):
                raise BuildError(
                    component.name, f"Target runtime imports missing: {sorted(required - actual)}"
                )
            if component.name == "av1":
                negative_libs = [x for x in shlex.split(libs) if x != "-lpthread"]
                negative = subprocess.run(
                    [
                        str(self.environment.target_tools.cc),
                        *cflags,
                        *shlex.split(flags),
                        str(source),
                        *negative_libs,
                        *ldflags,
                        "-o",
                        str(executable),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if not negative.returncode:
                    raise BuildError(
                        component.name, "Link unexpectedly succeeded without target pthread."
                    )


def build_or_reuse(
    component: Component, builder: Any, manager: Any, validator: Wave4ATargetValidator
) -> bool:
    prior = manager.get().components.get(component.name)
    if (
        prior
        and prior.status == ComponentStatus.COMPLETED
        and validator.manifest_matches(component)
    ):
        try:
            validator.validate_component(component)
            validator.validate_prefix()
        except BuildError as exc:
            manager.mark_component_status(
                component.name,
                ComponentStatus.FAILED,
                component.version,
                str(exc),
                detail="Wave-4A reuse validation failed",
            )
            raise
        return False
    try:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-4A target outputs require build",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="Wave-4A COFF, metadata and link acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-4A build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-4A build or acceptance failed: {exc}") from exc


def run_wave_components(
    ordered: Sequence[Component], builder: Any, manager: Any, validator: Wave4ATargetValidator
) -> tuple[list[str], list[str]]:
    if [component.name for component in ordered] != list(G4_WAVE4A_COMPONENTS):
        raise BuildError("G4 Wave 4A", "Execution must contain only svtav1 and av1.")
    built: list[str] = []
    reused: list[str] = []
    for component in ordered:
        changed = build_or_reuse(component, builder, manager, validator)
        (built if changed else reused).append(component.name)
        print(
            f"Wave-4A {component.name}: {'built' if changed else 'revalidated and reused'}",
            flush=True,
        )
    return built, reused


def run_g4_wave4a(app: Any, config: BuildConfig) -> int:
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW or config.allow_network_downloads:
        raise BuildError("G4 Wave 4A", "Cross backend and offline sources are required.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    sources = verify_sources(app.registry, archives)
    print(f"Verified Wave-4A sources: {sources}", flush=True)
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
        if (
            environment.paths.workspace_root.resolve() != session.workspace.resolve()
            or environment.paths.target_prefix.resolve() != (session.workspace / "prefix").resolve()
        ):
            raise BuildError("G4 Wave 4A", "Cross environment does not match the identity.")
        ordered = select_components(
            app.registry, app._get_component_selection(config).target_components_to_build
        )
        validator = Wave4ATargetValidator(environment, session.identity.build_key)
        validator.validate_prefix()
    except BuildError as exc:
        print(f"BLOCKED by cross environment or selection: {exc}", flush=True)
        return 2
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
            raise BuildError("G4 Wave 4A", "Acceptance manifest missing.")
        manifest.update(
            source_sha256=sources,
            built=built,
            reused=reused,
            wave4b_started=False,
            ffmpeg_built=False,
        )
        validator._write_manifest(manifest)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-4A build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 4A built={built}, reused={reused}", flush=True)
    return 0
