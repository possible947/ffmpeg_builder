"""Bounded Windows-target acceptance for the bespoke software codecs."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
from collections import Counter
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

G4_WAVE4B_COMPONENTS = ("x264", "x265", "libvpx")
_OUTPUTS = {
    "x264": _OutputSpec(
        "x264",
        ("include/x264.h", "include/x264_config.h"),
        ("lib/libx264.a",),
        ("include/x264.h", "include/x264_config.h", "lib/libx264.a", "lib/pkgconfig/x264.pc"),
        "#include <stdint.h>\n#include <x264.h>\n"
        "int main(void) { x264_param_t p; x264_param_default(&p); return 0; }\n",
    ),
    "x265": _OutputSpec(
        "x265",
        ("include/x265.h", "include/x265_config.h"),
        ("lib/libx265.a",),
        (
            "include/x265.h",
            "include/x265_config.h",
            "include/hdr10plus.h",
            "lib/libx265.a",
            "lib/libhdr10plus.a",
            "lib/pkgconfig/x265.pc",
        ),
        "#include <stdint.h>\n#include <x265.h>\n"
        "int main(void) { const x265_api *a = x265_api_get(10); "
        "return a == 0 || x265_api_get(12) == 0; }\n",
    ),
    "libvpx": _OutputSpec(
        "vpx",
        ("include/vpx/vpx_codec.h", "include/vpx/vp8cx.h", "include/vpx/vp8dx.h"),
        ("lib/libvpx.a",),
        ("include/vpx", "lib/libvpx.a", "lib/pkgconfig/vpx.pc"),
        "#include <vpx/vpx_codec.h>\n#include <vpx/vp8cx.h>\n#include <vpx/vp8dx.h>\n"
        "int main(void) { return !vpx_codec_vp8_cx() || !vpx_codec_vp9_dx(); }\n",
    ),
}


def verify_sources(registry: ComponentRegistry, archives: Path) -> dict[str, str]:
    sources = {}
    for name in G4_WAVE4B_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None or not component.sha256:
            raise BuildError(name, "Missing registered source/checksum.")
        archive = archives / component.get_archive_filename()
        try:
            with archive.open("rb") as stream:
                header = stream.read(128)
                stream.seek(0)
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise BuildError(name, f"Project source unavailable: {archive}: {exc}") from exc
        if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise BuildError(name, f"Unmaterialised Git LFS source: {archive}")
        if digest.lower() != component.sha256.lower():
            raise BuildError(name, f"Source checksum mismatch: {archive}")
        sources[name] = digest
    return sources


def select_components(
    registry: ComponentRegistry, selected: Sequence[Component]
) -> list[Component]:
    available = {component.name: component for component in selected}
    if len(available) != len(selected):
        raise BuildError("G4 Wave 4B", "Duplicate configured components.")
    result = []
    for name in G4_WAVE4B_COMPONENTS:
        component = available.get(name)
        if component is None or component.build_role != BuildRole.TARGET_COMPONENT:
            raise BuildError(name, "Component is not configured for the target.")
        if component.depends_on or registry.get_by_name(name) is None:
            raise BuildError(name, "Unexpected dependencies or missing registry entry.")
        result.append(component)
    return result


class Wave4BTargetValidator(Wave3BTargetValidator):
    def __init__(self, environment: Any, identity_key: str):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave4b-acceptance.json"

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        try:
            return _OUTPUTS[component.name]
        except KeyError as exc:
            raise BuildError(component.name, "No Wave-4B output contract.") from exc

    def manifest_matches(self, component: Component) -> bool:
        if not Wave3ATargetValidator.manifest_matches(self, component):
            return False
        if component.name == "x265":
            manifest = self._read_manifest()
            if manifest is None:
                return False
            try:
                entry = manifest["components"][component.name]
                return bool(
                    entry["variants_sha256"]
                    == {str(path): self._sha256(path) for path in self._variant_archives(component)}
                )
            except (KeyError, OSError, TypeError):
                return False
        return True

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        if component.name == "x265":
            manifest = self._read_manifest()
            if manifest is None:
                raise BuildError(component.name, "Cannot read acceptance manifest.")
            manifest["components"][component.name]["variants_sha256"] = {
                str(path): self._sha256(path) for path in self._variant_archives(component)
            }
            self._write_manifest(manifest)

    def _variant_archives(self, component: Component) -> tuple[Path, ...]:
        root = (
            self.environment.paths.packages_root
            / component.get_target_dir()
            / "build"
            / "cross-mingw"
        )
        return (
            root / "8bit" / "libx265_main.a",
            root / "10bit" / "libx265.a",
            root / "12bit" / "libx265.a",
        )

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        for relative in (*spec.headers, *spec.libraries, f"lib/pkgconfig/{spec.pkg_config}.pc"):
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
            )
            path = Path(value.strip())
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
        self._link_smoke(component.name, spec)
        return self._owned_outputs(component)

    def _validate_build_configuration(self, component: Component) -> None:
        source = self.environment.paths.packages_root / component.get_target_dir()
        if component.name == "x265":
            root = source / "build" / "cross-mingw"
            for bitdepth in ("12bit", "10bit", "8bit"):
                directory = root / bitdepth
                try:
                    cache = (directory / "CMakeCache.txt").read_text(encoding="utf-8")
                    system = next(directory.glob("CMakeFiles/*/CMakeSystem.cmake")).read_text()
                except (OSError, StopIteration) as exc:
                    raise BuildError(
                        component.name, f"Missing cross-build evidence: {exc}"
                    ) from exc
                if (
                    'set(CMAKE_SYSTEM_NAME "Windows")' not in system
                    or 'set(CMAKE_CROSSCOMPILING "TRUE")' not in system
                    or "CMAKE_ASM_NASM_OBJECT_FORMAT:UNINITIALIZED=win64" not in cache
                    or str(self.environment.cmake_toolchain_file) not in cache
                ):
                    raise BuildError(component.name, f"Invalid {bitdepth} CMake target evidence.")
                for flag in ("-march=native", "-mtune=native"):
                    if (
                        flag in self.environment.compiler_environment.get("CFLAGS", "")
                        and flag not in cache
                    ):
                        raise BuildError(
                            component.name, f"Missing {bitdepth} target CPU flag {flag}."
                        )
                archive = directory / ("libx265_main.a" if bitdepth == "8bit" else "libx265.a")
                self._validate_archive(component.name, archive)
                commands = subprocess.run(
                    ["ninja", "-C", str(directory), "-t", "commands"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assembly = [line for line in commands.stdout.splitlines() if "nasm " in line]
                if (
                    commands.returncode
                    or not assembly
                    or any("-f win64" not in line for line in assembly)
                ):
                    raise BuildError(component.name, f"No Windows NASM commands for {bitdepth}.")

            def members(path: Path) -> list[str]:
                result = subprocess.run(
                    [str(self.environment.target_tools.ar), "t", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode or not result.stdout.strip():
                    raise BuildError(component.name, f"Cannot enumerate archive: {path}")
                return result.stdout.splitlines()

            expected = Counter(
                member for path in self._variant_archives(component) for member in members(path)
            )
            if Counter(members(self.prefix / "lib/libx265.a")) != expected:
                raise BuildError(component.name, "Installed x265 archive differs from variants.")
            with tempfile.TemporaryDirectory(prefix="merge-check-", dir=root / "8bit") as temporary:
                rebuilt = Path(temporary) / "combined.a"
                script = (
                    "CREATE combined.a\n"
                    "ADDLIB ../libx265_main.a\n"
                    "ADDLIB ../../10bit/libx265.a\n"
                    "ADDLIB ../../12bit/libx265.a\n"
                    "SAVE\nEND\n"
                )
                result = subprocess.run(
                    [str(self.environment.target_tools.ar), "-M"],
                    cwd=temporary,
                    input=script,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if (
                    result.returncode
                    or not rebuilt.is_file()
                    or self._sha256(rebuilt) != self._sha256(self.prefix / "lib/libx265.a")
                ):
                    raise BuildError(component.name, "x265 archive merge is not reproducible.")
        else:
            config = source / (
                "config.mak" if component.name == "x264" else "libs-x86_64-win64-gcc.mk"
            )
            try:
                text = config.read_text(encoding="utf-8")
            except OSError as exc:
                raise BuildError(
                    component.name, f"Missing target configure evidence: {exc}"
                ) from exc
            if "-f win64" not in text or str(self.environment.target_tools.cc) not in text:
                raise BuildError(component.name, "Missing target compiler or NASM win64 evidence.")
            if component.name == "libvpx":
                try:
                    log = (source / "config.log").read_text(encoding="utf-8")
                except OSError as exc:
                    raise BuildError(component.name, f"Missing configure probe log: {exc}") from exc
                probes = [
                    Path(line.removeprefix("BEGIN ").strip())
                    for line in log.splitlines()
                    if line.startswith("BEGIN ") and "vpx-conf-" in line
                ]
                if not probes or any(
                    not self._inside(path, self.environment.paths.generated_files_root)
                    or path.parent.exists()
                    for path in probes
                ):
                    raise BuildError(component.name, "Configure probes escaped identity or remain.")
        if component.name == "x265":
            text = (source / "build/cross-mingw/8bit/CMakeCache.txt").read_text()
        for flag in ("-march=native", "-mtune=native"):
            if flag in self.environment.compiler_environment.get("CFLAGS", "") and flag not in text:
                raise BuildError(component.name, f"Missing target CPU flag {flag}.")

    def _link_smoke(self, component_name: str, spec: _OutputSpec) -> None:
        env = dict(self.environment.compiler_environment)
        flags = self._pkg_config(component_name, ["--static", "--cflags", spec.pkg_config], env)
        libs = self._pkg_config(component_name, ["--static", "--libs", spec.pkg_config], env)
        cflags = [flag for flag in shlex.split(env.get("CFLAGS", "")) if flag != "-fopenmp"]
        with tempfile.TemporaryDirectory(
            prefix="g4-wave4b-", dir=self.environment.paths.generated_files_root
        ) as temporary:
            program, executable = Path(temporary) / "smoke.c", Path(temporary) / "smoke.exe"
            program.write_text(spec.smoke_source)
            command = [
                str(self.environment.target_tools.cc),
                *cflags,
                *shlex.split(flags),
                str(program),
                *shlex.split(libs),
                "-o",
                str(executable),
            ]
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=120, check=False
            )
            if result.returncode:
                raise BuildError(
                    component_name, "Static target link failed: " + result.stderr[-5000:]
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component_name, executable
            )
            imports = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(executable)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if imports.returncode:
                raise BuildError(component_name, "Cannot inspect target runtime imports.")
            dlls = sorted(
                line.strip().split(": ", 1)[1]
                for line in imports.stdout.splitlines()
                if line.strip().startswith("Name: ")
            )
            print(f"Wave-4B {component_name} target imports: {dlls}", flush=True)
            if component_name in ("x265", "libvpx"):
                omitted = ("-lc++", "-lunwind") if component_name == "x265" else ("-lpthread",)
                negative = subprocess.run(
                    [item for item in command if item not in omitted],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if not negative.returncode:
                    raise BuildError(component_name, f"Link succeeded without required {omitted}.")

    def combined_link(self) -> None:
        env = dict(self.environment.compiler_environment)
        names = [spec.pkg_config for spec in _OUTPUTS.values()]
        flags = self._pkg_config("G4 Wave 4B", ["--static", "--cflags", *names], env)
        libs = self._pkg_config("G4 Wave 4B", ["--static", "--libs", *names], env)
        self._validate_target_flags("G4 Wave 4B", flags + " " + libs)
        source = (
            "#include <stdint.h>\n#include <x264.h>\n#include <x265.h>\n"
            "#include <vpx/vpx_codec.h>\n#include <vpx/vp8cx.h>\n"
            "int main(void) { x264_param_t p; x264_param_default(&p); "
            "return !x265_api_get(10) || !vpx_codec_vp9_cx(); }\n"
        )
        with tempfile.TemporaryDirectory(
            prefix="g4-wave4b-", dir=self.environment.paths.generated_files_root
        ) as temporary:
            program, executable = Path(temporary) / "smoke.c", Path(temporary) / "smoke.exe"
            program.write_text(source)
            target_flags = [
                flag for flag in shlex.split(env.get("CFLAGS", "")) if flag != "-fopenmp"
            ]
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    *target_flags,
                    *shlex.split(flags),
                    str(program),
                    *shlex.split(libs),
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
                    "G4 Wave 4B", f"Combined target link failed: {result.stderr[-5000:]}"
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                "G4 Wave 4B", executable
            )


def build_or_reuse(component: Component, builder: Any, manager: Any, validator: Any) -> bool:
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
                detail="Wave-4B reuse validation failed",
            )
            raise
        return False
    try:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-4B target outputs require build",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="Wave-4B COFF, metadata and link acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-4B build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-4B build or acceptance failed: {exc}") from exc


def run_wave_components(
    ordered: Sequence[Component], builder: Any, manager: Any, validator: Wave4BTargetValidator
) -> tuple[list[str], list[str]]:
    if tuple(component.name for component in ordered) != G4_WAVE4B_COMPONENTS:
        raise BuildError("G4 Wave 4B", "Execution must contain only x264, x265 and libvpx.")
    built: list[str] = []
    reused: list[str] = []
    for component in ordered:
        changed = build_or_reuse(component, builder, manager, validator)
        (built if changed else reused).append(component.name)
        print(
            f"Wave-4B {component.name}: {'built' if changed else 'revalidated and reused'}",
            flush=True,
        )
    return built, reused


def run_g4_wave4b(app: Any, config: BuildConfig) -> int:
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW or config.allow_network_downloads:
        raise BuildError("G4 Wave 4B", "Cross backend and offline sources are required.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    sources = verify_sources(app.registry, archives)
    print(f"Verified Wave-4B sources: {sources}", flush=True)
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
        if (
            environment.paths.workspace_root.resolve() != session.workspace.resolve()
            or environment.paths.target_prefix.resolve() != (session.workspace / "prefix").resolve()
        ):
            raise BuildError("G4 Wave 4B", "Cross environment does not match the identity.")
        ordered = select_components(
            app.registry, app._get_component_selection(config).target_components_to_build
        )
        validator = Wave4BTargetValidator(environment, session.identity.build_key)
        validator.validate_prefix()
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, ordered),
            cross_environment=environment,
        )
        built, reused = run_wave_components(ordered, builder, app.state_manager, validator)
        validator.combined_link()
        validator.validate_prefix()
        manifest = validator._read_manifest()
        if manifest is None:
            raise BuildError("G4 Wave 4B", "Acceptance manifest missing.")
        manifest.update(source_sha256=sources, built=built, reused=reused, ffmpeg_built=False)
        validator._write_manifest(manifest)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-4B build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 4B built={built}, reused={reused}", flush=True)
    return 0
