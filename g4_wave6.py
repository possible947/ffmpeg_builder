"""Bounded Windows-target rav1e build and installed-target acceptance."""

from __future__ import annotations

import hashlib
import json
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
from .downloader import Downloader
from .g4_wave2 import Wave2TargetValidator
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave3b import Wave3BTargetValidator
from .g4_wave1 import Wave1TargetValidator
from .state import ComponentStatus
from .windows_probe import WindowsInterop, WindowsProbeError

G4_WAVE6_COMPONENTS = ("rav1e",)
_SPEC = _OutputSpec(
    "rav1e",
    ("include/rav1e/rav1e.h",),
    ("lib/librav1e.a",),
    ("include/rav1e/rav1e.h", "lib/librav1e.a", "lib/pkgconfig/rav1e.pc"),
    "#include <rav1e.h>\n"
    "int main(void) {\n"
    "  RaConfig *config = rav1e_config_default();\n"
    "  if (!config) return 1;\n"
    "  RaContext *context = rav1e_context_new(config);\n"
    "  if (!context) { rav1e_config_unref(config); return 2; }\n"
    "  rav1e_context_unref(context);\n"
    "  rav1e_config_unref(config);\n"
    "  return 0;\n"
    "}\n",
    rejected_outputs=(
        "bin/rav1e.dll",
        "lib/librav1e.dll.a",
        "lib/librav1e.so",
        "lib/librav1e.dylib",
    ),
)
_SYSTEM_DLLS = {
    "advapi32.dll",
    "bcrypt.dll",
    "cfgmgr32.dll",
    "combase.dll",
    "dbghelp.dll",
    "gdi32.dll",
    "kernel32.dll",
    "msvcrt.dll",
    "ntdll.dll",
    "ole32.dll",
    "oleaut32.dll",
    "secur32.dll",
    "shell32.dll",
    "user32.dll",
    "userenv.dll",
    "version.dll",
    "winmm.dll",
    "ws2_32.dll",
}
_RAV1E_RUNTIME_LIBS = {
    "-lkernel32",
    "-lntdll",
    "-luserenv",
    "-lws2_32",
    "-ldbghelp",
    "-lunwind",
}


def verify_source(
    registry: ComponentRegistry, archives: Path, downloader: Downloader | None = None
) -> dict[str, str]:
    component = registry.get_by_name("rav1e")
    if component is None or not component.sha256:
        raise BuildError("rav1e", "Missing registered source/checksum.")
    path = archives / component.get_archive_filename()
    try:
        path = (
            downloader.download(
                component.get_url(),
                component.get_archive_filename(),
                expected_sha256=component.sha256,
                show_progress=False,
            )
            if downloader is not None
            else path
        )
        with path.open("rb") as stream:
            header = stream.read(128)
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, RuntimeError) as exc:
        raise BuildError("rav1e", f"Project-managed source unavailable: {path}: {exc}") from exc
    if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise BuildError("rav1e", "Source is an unresolved Git LFS pointer.")
    if digest.lower() != component.sha256.lower():
        raise BuildError("rav1e", "Source checksum mismatch.")
    return {"rav1e": digest}


def select_components(
    registry: ComponentRegistry, selected: Sequence[Component]
) -> list[Component]:
    matches = [item for item in selected if item.name == "rav1e"]
    component = registry.get_by_name("rav1e")
    if (
        len(matches) != 1
        or matches[0].build_role != BuildRole.TARGET_COMPONENT
        or component is None
        or matches[0].depends_on
    ):
        raise BuildError(
            "rav1e", "Target component not configured exactly once without dependencies."
        )
    return matches


def validate_prior_manifests(
    workspace: Path, prefix: Path, identity: str, current_wave: str = "g4-wave6-acceptance.json"
) -> int:
    """Verify prior-wave hashes without treating this wave's manifest as prior."""
    paths: set[str] = set()
    manifests = [
        *workspace.glob("g4-wave*-acceptance.json"),
        *workspace.glob("cross-*-acceptance.json"),
    ]
    for path in sorted(manifests):
        if path.name in (current_wave, "g4-wave2-openmp-acceptance.json"):
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest root is not an object")
            if manifest.get("identity_key") != identity or manifest.get("target_prefix") != str(
                prefix
            ):
                raise BuildError("G4 Wave 6", f"Prior manifest identity mismatch: {path}")
            components = manifest.get("components", {})
            if not isinstance(components, dict):
                raise ValueError("components is not an object")
            for entry in components.values():
                if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), dict):
                    raise ValueError("component output hashes are invalid")
                for relative, expected in entry["sha256"].items():
                    if not isinstance(relative, str) or not isinstance(expected, str):
                        raise ValueError("component output hash entry is invalid")
                    target = prefix / relative
                    if (
                        not target.resolve().is_relative_to(prefix.resolve())
                        or not target.is_file()
                    ):
                        raise BuildError("G4 Wave 6", f"Prior output missing: {target}")
                    with target.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != expected:
                        raise BuildError("G4 Wave 6", f"Prior output changed: {target}")
                    paths.add(relative)
        except (OSError, ValueError, TypeError) as exc:
            raise BuildError("G4 Wave 6", f"Invalid prior manifest {path}: {exc}") from exc
    return len(paths)


class Wave6TargetValidator(Wave3BTargetValidator):
    def __init__(self, environment: Any, identity_key: str, source_sha256: str):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave6-acceptance.json"
        self.source_sha256 = source_sha256

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        if component.name != "rav1e":
            raise BuildError(component.name, "No Wave-6 output contract.")
        return _SPEC

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        entry = manifest.get("components", {}).get(component.name, {}) if manifest else {}
        return (
            entry.get("recipe") == "wave6-v1"
            and entry.get("source_sha256") == self.source_sha256
            and Wave3ATargetValidator.manifest_matches(self, component)
        )

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        if manifest is None:
            raise BuildError(component.name, "Acceptance manifest missing.")
        entry = manifest["components"][component.name]
        entry.update(recipe="wave6-v1", source_sha256=self.source_sha256)
        self._write_manifest(manifest)

    def validate_prefix(self) -> None:
        super().validate_prefix()

    def validate_component(self, component: Component) -> list[str]:
        for relative in (*_SPEC.headers, *_SPEC.libraries, "lib/pkgconfig/rav1e.pc"):
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing target output: {relative}")
            self._require_inside_prefix(component.name, path)
        for relative in _SPEC.rejected_outputs:
            if (self.prefix / relative).exists():
                raise BuildError(component.name, f"Unexpected shared target output: {relative}")

        archive = self.prefix / "lib/librav1e.a"
        Wave2TargetValidator(self.environment, self.identity_key)._validate_archive(
            component.name, archive
        )
        tools = self.environment.target_tools
        members_result = subprocess.run(
            [str(tools.ar), "t", str(archive)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        inspected = subprocess.run(
            [str(tools.readobj), "--file-headers", str(archive)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        members = members_result.stdout.splitlines()
        formats = re.findall(r"^Format:\s*(\S+)", inspected.stdout, re.MULTILINE)
        valid_members = len(members) == len(formats) and all(
            name
            and Path(name).name == name
            and re.fullmatch(r"[A-Za-z0-9_.+-]+", name)
            and member_format in {"COFF-import-file-x86-64", "COFF-x86-64"}
            for name, member_format in zip(members, formats)
        )
        if members_result.returncode or inspected.returncode or not members or not valid_members:
            raise BuildError(
                component.name,
                "rav1e archive contains invalid Windows COFF members: "
                f"members={members[:8]}, formats={formats[:8]}",
            )
        print(f"Wave-6 rav1e archive: {len(members)} COFF members", flush=True)

        pc_file = self.prefix / "lib/pkgconfig/rav1e.pc"
        self._validate_metadata_paths(pc_file)
        fields = self._read_pc_fields(pc_file, component.name)
        if fields.get("Version") != component.version:
            raise BuildError(component.name, "rav1e pkg-config version does not match registry.")
        for field in ("Libs", "Libs.private"):
            if not _RAV1E_RUNTIME_LIBS.issubset(set(shlex.split(fields.get(field, "")))):
                raise BuildError(
                    component.name, f"rav1e pkg-config {field} misses target runtime libraries."
                )
        env = dict(self.environment.compiler_environment)
        for variable in ("prefix", "includedir", "libdir"):
            value = self._pkg_config(
                component.name, [f"--variable={variable}", "rav1e"], env
            ).strip()
            path = Path(value)
            if not path.is_absolute() or not self._inside(path, self.prefix):
                raise BuildError(component.name, f"Non-target pkg-config {variable}: {value!r}")
            if variable == "prefix" and path.resolve() != self.prefix:
                raise BuildError(component.name, "Incorrect pkg-config prefix.")
        flags = self._pkg_config(component.name, ["--static", "--cflags", "--libs", "rav1e"], env)
        self._validate_target_flags(component.name, flags)
        tokens = shlex.split(flags)
        if (
            "-lrav1e" not in tokens
            or not _RAV1E_RUNTIME_LIBS.issubset(tokens)
            or "-lstdc++" in tokens
            or "-fopenmp" in tokens
            or any(token.startswith("-Wl,-rpath") for token in tokens)
        ):
            raise BuildError(component.name, f"Incorrect static rav1e pkg-config flags: {flags}")
        self._link_and_run(component, flags, env)
        return self._owned_outputs(component)

    def _validate_metadata_paths(self, path: Path) -> None:
        Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(path)

    def _link_and_run(self, component: Component, flags: str, env: dict[str, str]) -> None:
        generated = self.environment.paths.generated_files_root
        with tempfile.TemporaryDirectory(prefix="g4-wave6-", dir=generated) as tmp:
            directory = Path(tmp)
            source = directory / "rav1e-consumer.c"
            executable = directory / "rav1e-consumer.exe"
            source.write_text(_SPEC.smoke_source, encoding="utf-8")
            command = [
                str(self.environment.target_tools.cc),
                *shlex.split(env.get("CFLAGS", "")),
                str(source),
                *shlex.split(flags),
                *shlex.split(env.get("LDFLAGS", "")),
                "-o",
                str(executable),
            ]
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=180, check=False
            )
            if result.returncode:
                raise BuildError(
                    component.name, f"Target C-interface link failed: {result.stderr[-5000:]}"
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component.name, executable
            )
            imports_result = subprocess.run(
                [
                    str(self.environment.target_tools.readobj),
                    "--file-headers",
                    "--coff-imports",
                    str(executable),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if (
                imports_result.returncode
                or "Format: COFF-x86-64" not in imports_result.stdout
                or not re.search(r"Magic:\s*0x20[Bb]", imports_result.stdout)
                or re.search(r"Name: .*\.so(?:\.|$)", imports_result.stdout, re.IGNORECASE)
            ):
                raise BuildError(
                    component.name, "C-interface consumer is not a Windows PE32+ image."
                )
            dlls = set(re.findall(r"^\s*Name: (.+)$", imports_result.stdout, re.MULTILINE))
            print(f"Wave-6 rav1e PE imports: {sorted(dlls)}", flush=True)
            runtime_dirs = [
                Path(self.environment.target_tools.prefix) / "x86_64-w64-mingw32" / "bin",
                Path(self.environment.target_tools.prefix) / "bin",
            ]
            for name in dlls:
                if name.lower() in _SYSTEM_DLLS or name.lower().startswith(
                    ("api-ms-win-", "ext-ms-win-")
                ):
                    continue
                runtime = next(
                    (root / name for root in runtime_dirs if (root / name).is_file()), None
                )
                if runtime is None:
                    raise BuildError(
                        component.name, f"Missing target runtime DLL for PE import: {name}"
                    )
                shutil.copyfile(runtime, directory / name)
            try:
                windows_path = subprocess.run(
                    ["wslpath", "-w", str(executable)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                ).stdout.strip()
                runtime_result = WindowsInterop().run_powershell(
                    f"$ErrorActionPreference='Stop'; & '{windows_path.replace(chr(39), chr(39) * 2)}'; "
                    "exit $LASTEXITCODE",
                    timeout=90,
                )
            except (WindowsProbeError, OSError, subprocess.SubprocessError) as exc:
                raise BuildError(
                    component.name, f"Windows C-interface runtime failed: {exc}"
                ) from exc
            if runtime_result.returncode:
                raise BuildError(
                    component.name,
                    f"Windows C-interface runtime failed: {runtime_result.stderr!r}",
                )


def build_or_reuse(
    component: Component, builder: Any, manager: Any, validator: Wave6TargetValidator
) -> bool:
    prior = manager.get().components.get(component.name)
    if prior and prior.status == ComponentStatus.COMPLETED:
        if not validator.manifest_matches(component):
            raise BuildError(component.name, "Accepted output or source changed; refusing reuse.")
        validator.validate_component(component)
        validator.validate_prefix()
        return False
    if validator._owned_outputs(component):
        raise BuildError(
            component.name, "Unaccepted rav1e outputs already exist; refusing overwrite."
        )
    try:
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-6 target build",
        )
        builder._wave6_created_outputs = []
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="Wave-6 target acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        for path in getattr(builder, "_wave6_created_outputs", []):
            path = Path(path)
            if path.is_file() or path.is_symlink():
                path.unlink()
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-6 build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-6 build or acceptance failed: {exc}") from exc


def run_g4_wave6(app: Any, config: BuildConfig) -> int:
    if (
        config.target_backend != TARGET_BACKEND_CROSS_MINGW
        or not config.native_build
        or not config.openmp
    ):
        raise BuildError(
            "G4 Wave 6",
            "Cross backend, offline sources and primary native/OpenMP profile required.",
        )
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        env = app.prepare_cross_environment(config, session)
        if env.paths.target_prefix.resolve() != (session.workspace / "prefix").resolve():
            raise BuildError("G4 Wave 6", "Cross environment does not match build identity.")
        selected = select_components(
            app.registry, app._get_component_selection(config).target_components_to_build
        )
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, selected),
            cross_environment=env,
        )
        sources = verify_source(app.registry, archives, builder.downloader)
        validator = Wave6TargetValidator(env, session.identity.build_key, sources["rav1e"])
        validator.validate_prefix()
        prior_count = validate_prior_manifests(
            validator.workspace, validator.prefix, validator.identity_key
        )
        print(f"Wave-6: verified {prior_count} prior manifest-recorded files", flush=True)
        changed = build_or_reuse(selected[0], builder, app.state_manager, validator)
        manifest = validator._read_manifest()
        if manifest is None:
            raise BuildError("G4 Wave 6", "Acceptance manifest missing.")
        manifest.update(
            source_sha256=sources,
            built=["rav1e"] if changed else [],
            reused=[] if changed else ["rav1e"],
            ffmpeg_built=False,
        )
        validator._write_manifest(manifest)
        validator.validate_prefix()
        if (
            validate_prior_manifests(validator.workspace, validator.prefix, validator.identity_key)
            != prior_count
        ):
            raise BuildError("G4 Wave 6", "Prior manifest inventory changed during build.")
    except (BuildError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-6 build or acceptance: {exc}", flush=True)
        return 1
    print(
        f"PASS: Wave 6 built={['rav1e'] if changed else []}, "
        f"reused={[] if changed else ['rav1e']}",
        flush=True,
    )
    return 0
