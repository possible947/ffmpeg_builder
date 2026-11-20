"""Bounded Windows-target acceptance for zimg."""

from __future__ import annotations

import hashlib
import re
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

G4_WAVE4C_COMPONENTS = ("zimg",)
_SPEC = _OutputSpec(
    "zimg",
    ("include/zimg.h", "include/zimg++.hpp"),
    ("lib/libzimg.a",),
    (
        "include/zimg.h",
        "include/zimg++.hpp",
        "lib/libzimg.a",
        "lib/libzimg.la",
        "lib/pkgconfig/zimg.pc",
    ),
    "#include <zimg.h>\nint main(void) { zimg_image_format f; "
    "zimg_image_format_default(&f, ZIMG_API_VERSION); "
    "zimg_filter_graph *g = zimg_filter_graph_build(&f, &f, 0); "
    "zimg_filter_graph_free(g); return 0; }\n",
    rejected_outputs=("bin/zimg.dll", "lib/libzimg.dll.a", "lib/libzimg.so"),
)
_CPP_SMOKE = (
    "#include <zimg++.hpp>\nint main() { zimgxx::zimage_format f; "
    "zimg_filter_graph *g = zimg_filter_graph_build(&f, &f, nullptr); "
    "zimgxx::FilterGraph graph(g); return 0; }\n"
)


def verify_source(registry: ComponentRegistry, archives: Path) -> dict[str, str]:
    component = registry.get_by_name("zimg")
    if component is None or not component.sha256:
        raise BuildError("zimg", "Missing registered source/checksum.")
    archive = archives / component.get_archive_filename()
    try:
        with archive.open("rb") as stream:
            header = stream.read(128)
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise BuildError("zimg", f"Project source unavailable: {archive}: {exc}") from exc
    if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise BuildError("zimg", f"Unmaterialised Git LFS source: {archive}")
    if digest.lower() != component.sha256.lower():
        raise BuildError("zimg", f"Source checksum mismatch: {archive}")
    return {"zimg": digest}


def select_components(
    registry: ComponentRegistry, selected: Sequence[Component]
) -> list[Component]:
    available = {item.name: item for item in selected}
    if len(available) != len(selected):
        raise BuildError("G4 Wave 4C", "Duplicate configured components.")
    component = available.get("zimg")
    if component is None or component.build_role != BuildRole.TARGET_COMPONENT:
        raise BuildError("zimg", "Component is not configured for the target.")
    if component.depends_on or registry.get_by_name("zimg") is None:
        raise BuildError("zimg", "Unexpected dependencies or missing registry entry.")
    return [component]


class Wave4CTargetValidator(Wave3BTargetValidator):
    def __init__(self, environment: Any, identity_key: str):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave4c-acceptance.json"

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        if component.name != "zimg":
            raise BuildError(component.name, "No Wave-4C output contract.")
        return _SPEC

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        entry = manifest.get("components", {}).get(component.name, {}) if manifest else {}
        return entry.get("recipe") == "zimg-static-v3" and Wave3ATargetValidator.manifest_matches(
            self, component
        )

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        if manifest is None:
            raise BuildError(component.name, "Cannot read Wave-4C acceptance manifest.")
        manifest["components"][component.name]["recipe"] = "zimg-static-v3"
        self._write_manifest(manifest)

    def validate_component(self, component: Component) -> list[str]:
        for relative in (
            *_SPEC.headers,
            *_SPEC.libraries,
            "lib/libzimg.la",
            "lib/pkgconfig/zimg.pc",
        ):
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing required target output: {path}")
            self._require_inside_prefix(component.name, path)
        for relative in _SPEC.rejected_outputs:
            if (self.prefix / relative).exists():
                raise BuildError(component.name, f"Unexpected shared output: {relative}")
        archive = self.prefix / "lib/libzimg.a"
        self._validate_archive(component.name, archive)
        result = subprocess.run(
            [str(self.environment.target_tools.ar), "t", str(archive)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        members = result.stdout.splitlines()
        if (
            result.returncode
            or not members
            or any(not name.endswith((".o", ".obj")) for name in members)
        ):
            raise BuildError(component.name, "Invalid archive member list.")
        inspected = subprocess.run(
            [str(self.environment.target_tools.readobj), "--file-headers", str(archive)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if inspected.returncode or len(
            re.findall(r"^Format:\s*COFF-x86-64$", inspected.stdout, re.MULTILINE)
        ) != len(members):
            raise BuildError(component.name, "Not every archive member is Windows x86-64 COFF.")
        symbols = subprocess.run(
            [str(self.environment.target_tools.nm), "-u", str(archive)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if symbols.returncode or re.search(r"cygwin|msys-2\.0", symbols.stdout, re.IGNORECASE):
            raise BuildError(
                component.name, "Archive contains unsupported MSYS runtime references."
            )
        print(f"Wave-4C zimg: {len(members)} COFF archive members", flush=True)
        pc = self.prefix / "lib/pkgconfig/zimg.pc"
        Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(pc)
        la = self.prefix / "lib/libzimg.la"
        Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(la)
        dependency_line = next(
            (line for line in la.read_text().splitlines() if line.startswith("dependency_libs=")),
            "",
        )
        if (
            "-lc++" not in dependency_line
            or "-lunwind" not in dependency_line
            or "-lstdc++" in dependency_line
        ):
            raise BuildError(component.name, "Invalid libtool C++ runtime metadata.")
        fields = self._read_pc_fields(pc, component.name)
        if (
            fields.get("Version") != component.version
            or fields.get("Requires")
            or fields.get("Requires.private")
        ):
            raise BuildError(component.name, "Unexpected zimg pkg-config version or dependencies.")
        env = dict(self.environment.compiler_environment)
        for variable in ("prefix", "includedir", "libdir"):
            value = self._pkg_config(
                component.name, [f"--variable={variable}", "zimg"], env
            ).strip()
            path = Path(value)
            if not path.is_absolute() or not self._inside(path, self.prefix):
                raise BuildError(component.name, f"Non-target pkg-config {variable}: {value!r}")
        flags = self._pkg_config(component.name, ["--static", "--cflags", "zimg"], env)
        libs = self._pkg_config(component.name, ["--static", "--libs", "zimg"], env)
        self._validate_target_flags(component.name, flags + " " + libs)
        if not {"-lzimg", "-lc++", "-lunwind"}.issubset(shlex.split(libs)) or "-lstdc++" in libs:
            raise BuildError(component.name, f"Invalid static runtime metadata: {libs}")
        if not {"-lc++", "-lunwind"}.issubset(
            component.target_link_metadata.consumer_link_requirements
        ):
            raise BuildError(component.name, "Registry omits static C++ runtime requirements.")
        self._validate_configuration(component)
        self._links(component, flags, libs, env)
        return self._owned_outputs(component)

    def _validate_configuration(self, component: Component) -> None:
        source = self.environment.paths.packages_root / component.get_target_dir()
        try:
            log = (source / "config.log").read_text()
            makefile = (source / "Makefile").read_text()
        except OSError as exc:
            raise BuildError(component.name, f"Missing configure evidence: {exc}") from exc
        if (
            not re.search(
                r"checking whether we are cross compiling\nconfigure:\d+: result: yes", log
            )
            or "ZIMG_X86" not in makefile
        ):
            raise BuildError(component.name, "Cross configuration or x86 SIMD missing.")
        for flag in ("-march=native", "-mtune=native"):
            if (
                flag in self.environment.compiler_environment.get("CXXFLAGS", "")
                and flag not in makefile
            ):
                raise BuildError(component.name, f"Target CPU flag missing: {flag}")
        if str(self.environment.target_tools.cxx) not in makefile:
            raise BuildError(component.name, "Target C++ compiler missing.")

    def _links(self, component: Component, flags: str, libs: str, env: dict[str, str]) -> None:
        with tempfile.TemporaryDirectory(
            prefix="g4-wave4c-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            for language, source_text, compiler, variable in (
                ("c", _SPEC.smoke_source, self.environment.target_tools.cc, "CFLAGS"),
                ("cpp", _CPP_SMOKE, self.environment.target_tools.cxx, "CXXFLAGS"),
            ):
                source = Path(tmp) / f"consumer.{language}"
                exe = Path(tmp) / f"consumer-{language}.exe"
                source.write_text(source_text)
                target_flags = [f for f in shlex.split(env.get(variable, "")) if f != "-fopenmp"]
                command = [
                    str(compiler),
                    *target_flags,
                    *shlex.split(flags),
                    str(source),
                    *shlex.split(libs),
                    "-o",
                    str(exe),
                ]
                result = subprocess.run(
                    command, env=env, capture_output=True, text=True, timeout=120, check=False
                )
                if result.returncode:
                    raise BuildError(
                        component.name, f"{language} target link failed: {result.stderr[-5000:]}"
                    )
                Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                    component.name, exe
                )
                imports = subprocess.run(
                    [str(self.environment.target_tools.readobj), "--coff-imports", str(exe)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if imports.returncode:
                    raise BuildError(component.name, "Cannot inspect target imports.")
                dlls = re.findall(r"^\s*Name: (.+)$", imports.stdout, re.MULTILINE)
                print(f"Wave-4C {language} target imports: {sorted(dlls)}", flush=True)
                if language == "c":
                    requirements = component.target_link_metadata.consumer_link_requirements
                    registry_command = [
                        item for item in command if item not in ("-lc++", "-lunwind")
                    ]
                    registry_command[-2:-2] = list(requirements)
                    registry_link = subprocess.run(
                        registry_command,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                    )
                    if registry_link.returncode:
                        raise BuildError(
                            component.name,
                            "Registry runtime metadata link failed: "
                            + registry_link.stderr[-5000:],
                        )
                    Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                        component.name, exe
                    )
                    negative = subprocess.run(
                        [item for item in command if item not in ("-lc++", "-lunwind")],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                    )
                    if not negative.returncode:
                        raise BuildError(
                            component.name, "C link succeeded without C++ runtime metadata."
                        )


def build_or_reuse(
    component: Component, builder: Any, manager: Any, validator: Wave4CTargetValidator
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
                detail="Wave-4C reuse validation failed",
            )
            raise
        return False
    try:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-4C target outputs require build",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="Wave-4C COFF, metadata and link acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-4C build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-4C build or acceptance failed: {exc}") from exc


def run_wave_components(
    ordered: Sequence[Component], builder: Any, manager: Any, validator: Wave4CTargetValidator
) -> tuple[list[str], list[str]]:
    if tuple(item.name for item in ordered) != G4_WAVE4C_COMPONENTS:
        raise BuildError("G4 Wave 4C", "Execution must contain only zimg.")
    built: list[str] = []
    reused: list[str] = []
    for component in ordered:
        changed = build_or_reuse(component, builder, manager, validator)
        (built if changed else reused).append(component.name)
        print(
            f"Wave-4C {component.name}: {'built' if changed else 'revalidated and reused'}",
            flush=True,
        )
    return built, reused


def run_g4_wave4c(app: Any, config: BuildConfig) -> int:
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW or config.allow_network_downloads:
        raise BuildError("G4 Wave 4C", "Cross backend and offline sources are required.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    sources = verify_source(app.registry, archives)
    print(f"Verified Wave-4C sources: {sources}", flush=True)
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
        if (
            environment.paths.workspace_root.resolve() != session.workspace.resolve()
            or environment.paths.target_prefix.resolve() != (session.workspace / "prefix").resolve()
        ):
            raise BuildError("G4 Wave 4C", "Cross environment does not match the identity.")
        ordered = select_components(
            app.registry, app._get_component_selection(config).target_components_to_build
        )
        validator = Wave4CTargetValidator(environment, session.identity.build_key)
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
        manifest = validator._read_manifest()
        if manifest is None:
            raise BuildError("G4 Wave 4C", "Acceptance manifest missing.")
        manifest.update(source_sha256=sources, built=built, reused=reused, ffmpeg_built=False)
        validator._write_manifest(manifest)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-4C build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 4C built={built}, reused={reused}", flush=True)
    return 0
