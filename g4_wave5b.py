"""Bounded Windows-target libzmq acceptance."""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import BuildRole, Component, ComponentRegistry
from .config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .cross_build_contracts import compose_target_link_metadata
from .g4_wave1 import Wave1TargetValidator
from .g4_wave2 import Wave2TargetValidator
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave3b import Wave3BTargetValidator
from .g4_wave5a import Wave5ATargetValidator
from .state import ComponentStatus
from .windows_probe import WindowsInterop, WindowsProbeError

G4_WAVE5B_COMPONENTS = ("libzmq",)
_SPEC = _OutputSpec(
    "libzmq",
    ("include/zmq.h", "include/zmq_utils.h"),
    ("lib/libzmq.a",),
    (
        "include/zmq.h",
        "include/zmq_utils.h",
        "lib/libzmq.a",
        "lib/libzmq.la",
        "lib/pkgconfig/libzmq.pc",
    ),
    "#include <zmq.h>\nint main(void) { char value = 0; void *ctx = zmq_ctx_new(); "
    "if (!ctx) return 1; void *a = zmq_socket(ctx, ZMQ_PAIR); void *b = zmq_socket(ctx, ZMQ_PAIR); "
    'if (!a || !b || zmq_bind(a, "inproc://wave5b") || zmq_connect(b, "inproc://wave5b")) return 2; '
    "if (zmq_send(b, \"x\", 1, 0) != 1 || zmq_recv(a, &value, 1, 0) != 1 || value != 'x') return 3; "
    "return zmq_close(a) || zmq_close(b) || zmq_ctx_term(ctx); }\n",
    rejected_outputs=(
        "bin/libzmq.dll",
        "lib/libzmq.dll.a",
        "lib/libzmq.so",
        "bin/curve_keygen.exe",
    ),
)


def verify_source(registry: ComponentRegistry, archives: Path) -> dict[str, str]:
    component = registry.get_by_name("libzmq")
    if component is None or not component.sha256:
        raise BuildError("libzmq", "Missing registered source/checksum.")
    path = archives / component.get_archive_filename()
    try:
        with path.open("rb") as stream:
            header = stream.read(128)
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise BuildError("libzmq", f"Project-managed source unavailable: {path}: {exc}") from exc
    if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise BuildError("libzmq", "Source is an unresolved Git LFS pointer.")
    if digest.lower() != component.sha256.lower():
        raise BuildError("libzmq", "Source checksum mismatch.")
    return {"libzmq": digest}


def select_components(
    registry: ComponentRegistry, selected: Sequence[Component]
) -> list[Component]:
    matches = [item for item in selected if item.name == "libzmq"]
    if len(matches) != 1 or matches[0].build_role != BuildRole.TARGET_COMPONENT:
        raise BuildError("libzmq", "Target component not configured exactly once.")
    if matches[0].depends_on or registry.get_by_name("libzmq") is None:
        raise BuildError("libzmq", "Unexpected dependencies or missing registry entry.")
    return matches


class Wave5BTargetValidator(Wave3BTargetValidator):
    def __init__(self, environment: Any, identity_key: str):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave5b-acceptance.json"

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        if component.name != "libzmq":
            raise BuildError(component.name, "No Wave-5B output contract.")
        return _SPEC

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        entry = manifest.get("components", {}).get(component.name, {}) if manifest else {}
        return entry.get("recipe") == "wave5b-v1" and Wave3ATargetValidator.manifest_matches(
            self, component
        )

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        if manifest is None:
            raise BuildError(component.name, "Acceptance manifest missing.")
        manifest["components"][component.name]["recipe"] = "wave5b-v1"
        self._write_manifest(manifest)

    def validate_prefix(self) -> None:
        Wave5ATargetValidator(self.environment, self.identity_key).validate_prefix()

    def validate_component(self, component: Component) -> list[str]:
        for relative in _SPEC.owned_paths:
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing target output: {relative}")
            self._require_inside_prefix(component.name, path)
        for relative in _SPEC.rejected_outputs:
            if (self.prefix / relative).exists():
                raise BuildError(component.name, f"Unexpected target output: {relative}")
        archive = self.prefix / "lib/libzmq.a"
        self._validate_archive(component.name, archive)
        tools = self.environment.target_tools
        members = subprocess.run(
            [str(tools.ar), "t", str(archive)],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout.splitlines()
        formats = subprocess.run(
            [str(tools.readobj), "--file-headers", str(archive)],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
        if (
            not members
            or len(re.findall(r"^Format:\s*COFF-x86-64$", formats, re.MULTILINE)) != len(members)
            or any(not item.endswith((".o", ".obj")) for item in members)
        ):
            raise BuildError(component.name, "Archive contains invalid target members.")
        symbols = subprocess.run(
            [str(tools.nm), "-u", str(archive)],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
        if re.search(r"__kmpc|GOMP_|cygwin|msys-2\.0", symbols, re.IGNORECASE):
            raise BuildError(component.name, "Unexpected archive runtime references.")
        print(f"Wave-5B: {len(members)} COFF members", flush=True)
        for relative in ("lib/pkgconfig/libzmq.pc", "lib/libzmq.la"):
            Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(
                self.prefix / relative
            )
        libtool = (self.prefix / "lib/libzmq.la").read_text()
        if any(value in libtool for value in ("-fopenmp", "-lomp", "-lstdc++", "-lrpcrt4")):
            raise BuildError(component.name, "Unexpected libtool runtime metadata.")
        fields = self._read_pc_fields(self.prefix / "lib/pkgconfig/libzmq.pc", component.name)
        if (
            fields.get("Version") != component.version
            or fields.get("Requires")
            or fields.get("Requires.private")
        ):
            raise BuildError(component.name, "Unexpected pkg-config version or dependencies.")
        env = dict(self.environment.compiler_environment)
        for variable in ("prefix", "includedir", "libdir"):
            value = self._pkg_config(
                component.name, [f"--variable={variable}", "libzmq"], env
            ).strip()
            path = Path(value)
            if not path.is_absolute() or not self._inside(path, self.prefix):
                raise BuildError(component.name, f"Non-target pkg-config {variable}: {value!r}")
            if variable == "prefix" and path.resolve() != self.prefix:
                raise BuildError(component.name, "Incorrect pkg-config prefix.")
        flags = self._pkg_config(component.name, ["--static", "--cflags", "--libs", "libzmq"], env)
        self._validate_target_flags(component.name, flags)
        expected = {
            "-lzmq",
            "-DZMQ_STATIC",
            "-lc++",
            "-lunwind",
            "-lpthread",
            "-liphlpapi",
            "-lws2_32",
        }
        tokens = set(shlex.split(flags))
        if not expected.issubset(tokens) or tokens.intersection(
            {"-lstdc++", "-lomp", "-fopenmp", "-lsodium", "-lssl", "-ldl"}
        ):
            raise BuildError(component.name, f"Incorrect target metadata: {flags}")
        allowed_libs = {value for value in expected if value.startswith("-l")}
        if {value for value in tokens if value.startswith("-l")} != allowed_libs:
            raise BuildError(component.name, f"Unexpected target libraries: {flags}")
        if not (expected - {"-lzmq", "-DZMQ_STATIC"}).issubset(
            component.target_link_metadata.consumer_link_requirements
        ):
            raise BuildError(component.name, "Registry target runtime metadata incomplete.")
        expected_runtime = {"libc++.dll", "libunwind.dll", "libwinpthread-1.dll"}
        registered_runtime = {
            entry.name
            for entry in component.target_link_metadata.runtime_dependencies
            if entry.classification == "toolchain-runtime-dll"
            and entry.source_identity == self.environment.target_tools.prefix.name
        }
        if registered_runtime != expected_runtime:
            raise BuildError(
                component.name, "Registry target runtime DLL classification incorrect."
            )
        source = self.environment.paths.packages_root / component.get_target_dir()
        log = (source / "config.log").read_text()
        platform = (source / "src/platform.hpp").read_text()
        makefile = (source / "Makefile").read_text()
        if not re.search(
            r"checking whether we are cross compiling\nconfigure:\d+: result: yes", log
        ):
            raise BuildError(component.name, "Configure did not establish cross compilation.")
        for value in (
            "#define ZMQ_HAVE_WINDOWS 1",
            "#define ZMQ_HAVE_MINGW 1",
            "#define ZMQ_POLL_BASED_ON_SELECT 1",
        ):
            if value not in platform:
                raise BuildError(component.name, f"Missing target platform selection: {value}")
        if re.search(
            r"^#define (?:ZMQ_BUILD_DRAFT_API|ZMQ_USE_LIBSODIUM|ZMQ_HAVE_LIBBSD|HAVE_LIBUNWIND|HAVE_LIBGSSAPI_KRB5|ZMQ_HAVE_OPENPGM|ZMQ_HAVE_NORM|ZMQ_HAVE_WS)\b",
            platform,
            re.MULTILINE,
        ):
            raise BuildError(component.name, "Unexpected optional target feature enabled.")
        for value in (str(tools.cc), str(tools.cxx), "-march=native", "-mtune=native"):
            if value not in makefile:
                raise BuildError(component.name, f"Missing target build setting: {value}")
        self._link_and_runtime(component, flags, env)
        return self._owned_outputs(component)

    def _link_and_runtime(self, component: Component, flags: str, env: dict[str, str]) -> None:
        with tempfile.TemporaryDirectory(
            prefix="g4-wave5b-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            directory = Path(tmp)
            source = directory / "smoke.c"
            executable = directory / "smoke.exe"
            source.write_text(_SPEC.smoke_source)
            command = [
                str(self.environment.target_tools.cc),
                *[f for f in shlex.split(env.get("CFLAGS", "")) if f != "-fopenmp"],
                str(source),
                *shlex.split(flags),
                "-o",
                str(executable),
            ]
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=120, check=False
            )
            if result.returncode:
                raise BuildError(component.name, f"Target link failed: {result.stderr[-5000:]}")
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component.name, executable
            )
            requirements = compose_target_link_metadata(
                component.target_link_metadata
            ).consumer_link_requirements
            registry_command = [part for part in command if part not in requirements]
            registry_command[-2:-2] = list(requirements)
            registry_result = subprocess.run(
                registry_command, env=env, capture_output=True, text=True, timeout=120, check=False
            )
            if registry_result.returncode:
                raise BuildError(
                    component.name,
                    f"Composed target metadata link failed: {registry_result.stderr[-5000:]}",
                )
            for requirement, symbol in (
                ("-lc++", r"std::|__cxa|operator new"),
                ("-lpthread", r"pthread_"),
                ("-liphlpapi", r"if_nametoindex|if_indextoname|GetAdaptersAddresses"),
                ("-lws2_32", r"WSA|socket|recv|send"),
            ):
                result = subprocess.run(
                    [part for part in command if part != requirement],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if not result.returncode:
                    raise BuildError(
                        component.name, f"Target link unexpectedly succeeds without {requirement}."
                    )
                if "undefined symbol" not in result.stderr or not re.search(symbol, result.stderr):
                    raise BuildError(
                        component.name, f"Unexpected negative-link failure without {requirement}."
                    )
            # Rebuild the positive executable after negative links.
            subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=120, check=True
            )
            imports = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(executable)],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            ).stdout
            dlls = set(re.findall(r"^\s*Name: (.+)$", imports, re.MULTILINE))
            print(f"Wave-5B PE imports: {sorted(dlls)}", flush=True)
            required = {"libc++.dll", "libunwind.dll", "libwinpthread-1.dll"}
            if not required.issubset(dlls):
                raise BuildError(
                    component.name, f"Unexpected target runtime imports: {sorted(dlls)}"
                )
            runtime = (
                Path(self.environment.target_tools.cc).parent.parent / "x86_64-w64-mingw32/bin"
            )
            for dll in required:
                shutil.copyfile(runtime / dll, directory / dll)
            path = subprocess.run(
                ["wslpath", "-w", str(executable)],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            quoted = path.replace("'", "''")
            try:
                runtime_result = WindowsInterop().run_powershell(
                    f"$ErrorActionPreference='Stop'; & '{quoted}'; exit $LASTEXITCODE",
                    timeout=60,
                )
            except WindowsProbeError as exc:
                raise BuildError(component.name, f"Windows runtime smoke failed: {exc}") from exc
            if runtime_result.returncode:
                raise BuildError(
                    component.name, f"Windows runtime smoke failed: {runtime_result.stderr!r}"
                )


def build_or_reuse(
    component: Component, builder: Any, manager: Any, validator: Wave5BTargetValidator
) -> bool:
    prior = manager.get().components.get(component.name)
    if prior and prior.status == ComponentStatus.COMPLETED:
        if not validator.manifest_matches(component):
            raise BuildError(component.name, "Accepted output changed; refusing reuse.")
        validator.validate_component(component)
        validator.validate_prefix()
        return False
    paths = [validator.prefix / relative for relative in _SPEC.owned_paths]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise BuildError(
            component.name, "Unaccepted libzmq outputs already exist; refusing to overwrite."
        )
    try:
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-5B target build",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="Wave-5B target acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        for path in paths:
            if path.is_file() or path.is_symlink():
                path.unlink()
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-5B build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-5B build or acceptance failed: {exc}") from exc


def run_g4_wave5b(app: Any, config: BuildConfig) -> int:
    if (
        config.target_backend != TARGET_BACKEND_CROSS_MINGW
        or config.allow_network_downloads
        or not config.native_build
        or not config.openmp
    ):
        raise BuildError(
            "G4 Wave 5B",
            "Cross backend, offline sources and primary native/OpenMP profile required.",
        )
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    sources = verify_source(app.registry, archives)
    print(f"Verified Wave-5B sources: {sources}", flush=True)
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        env = app.prepare_cross_environment(config, session)
        if env.paths.target_prefix.resolve() != (session.workspace / "prefix").resolve():
            raise BuildError("G4 Wave 5B", "Cross environment does not match identity.")
        selected = select_components(
            app.registry, app._get_component_selection(config).target_components_to_build
        )
        validator = Wave5BTargetValidator(env, session.identity.build_key)
        validator.validate_prefix()
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, selected),
            cross_environment=env,
        )
        changed = build_or_reuse(selected[0], builder, app.state_manager, validator)
        manifest = validator._read_manifest()
        if manifest is None:
            raise BuildError("G4 Wave 5B", "Acceptance manifest missing.")
        manifest.update(
            source_sha256=sources,
            built=["libzmq"] if changed else [],
            reused=[] if changed else ["libzmq"],
            ffmpeg_built=False,
        )
        validator._write_manifest(manifest)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-5B build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 5B {'built' if changed else 'revalidated and reused'} libzmq", flush=True)
    return 0
