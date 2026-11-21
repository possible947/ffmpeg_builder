"""Bounded Windows-target libjxl and VapourSynth acceptance."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .builders.codecs.jxl import _LIBRARIES, normalize_cross_metadata
from .builders.codecs.jxl_sources import DEPENDENCIES
from .components import BuildRole, Component, ComponentRegistry
from .config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .downloader import Downloader
from .g4_wave1 import Wave1TargetValidator
from .g4_wave2 import Wave2TargetValidator
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave3b import Wave3BTargetValidator
from .state import ComponentStatus
from .windows_probe import WindowsInterop, WindowsProbeError

G4_WAVE5C_COMPONENTS = ("libjxl", "VapourSynth")
_JXL_PC = (
    "libjxl",
    "libjxl_threads",
    "libjxl_cms",
    "libhwy",
    "libbrotlicommon",
    "libbrotlidec",
    "libbrotlienc",
)
_SPECS = {
    "libjxl": _OutputSpec(
        "libjxl",
        ("include/jxl/decode.h", "include/jxl/encode.h", "include/jxl/thread_parallel_runner.h"),
        tuple(f"lib/{name}" for name in _LIBRARIES),
        (
            "include/jxl",
            "include/hwy",
            "include/brotli",
            *(f"lib/{name}" for name in _LIBRARIES),
            *(f"lib/pkgconfig/{name}.pc" for name in _JXL_PC),
            "lib/cmake/hwy",
            "lib/cmake/libjxl",
            "lib/cmake/brotli",
        ),
        "",
    ),
    "VapourSynth": _OutputSpec(
        "",
        ("include/vapoursynth/VSScript4.h", "include/vapoursynth/VapourSynth4.h"),
        (),
        ("include/vapoursynth",),
        "",
    ),
}
_SMOKE = """#include <jxl/decode.h>
#include <jxl/encode.h>
#include <jxl/thread_parallel_runner.h>
int main(void) {
  JxlDecoder *d = JxlDecoderCreate(0);
  JxlEncoder *e = JxlEncoderCreate(0);
  void *r = JxlThreadParallelRunnerCreate(0, 2);
  if (!d || !e || !r) return 1;
  JxlThreadParallelRunnerDestroy(r);
  JxlEncoderDestroy(e);
  JxlDecoderDestroy(d);
  return 0;
}
"""


def verify_sources(
    registry: ComponentRegistry, archives: Path, downloader: Downloader | None = None
) -> dict[str, str]:
    result = {}
    for name in G4_WAVE5C_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None or not component.sha256:
            raise BuildError(name, "Missing registered source checksum.")
        try:
            path = (
                downloader.download(
                    component.get_url(),
                    component.get_archive_filename(),
                    expected_sha256=component.sha256,
                    show_progress=False,
                )
                if downloader is not None
                else archives / component.get_archive_filename()
            )
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, RuntimeError) as exc:
            raise BuildError(name, f"Source unavailable: {exc}") from exc
        if digest != component.sha256:
            raise BuildError(name, f"Source checksum mismatch: {path}")
        result[name] = digest
    return result


def select_components(
    registry: ComponentRegistry, selected: Sequence[Component]
) -> list[Component]:
    available = {item.name: item for item in selected}
    if len(available) != len(selected):
        raise BuildError("G4 Wave 5C", "Duplicate configured components.")
    result = []
    for name in G4_WAVE5C_COMPONENTS:
        component = available.get(name)
        if (
            component is None
            or component.build_role != BuildRole.TARGET_COMPONENT
            or registry.get_by_name(name) is None
        ):
            raise BuildError(name, "Target component not configured.")
        result.append(component)
    return result


def validate_prior_manifests(workspace: Path, prefix: Path, identity: str) -> int:
    paths: set[str] = set()
    for path in sorted(workspace.glob("g4-wave*-acceptance.json")):
        if path.name in ("g4-wave5c-acceptance.json", "g4-wave2-openmp-acceptance.json"):
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("identity_key") != identity or manifest.get("target_prefix") != str(
                prefix
            ):
                raise BuildError("G4 Wave 5C", f"Prior manifest identity mismatch: {path}")
            for entry in manifest.get("components", {}).values():
                for relative, expected in entry.get("sha256", {}).items():
                    target = prefix / relative
                    if (
                        not target.resolve().is_relative_to(prefix.resolve())
                        or not target.is_file()
                    ):
                        raise BuildError("G4 Wave 5C", f"Prior output missing: {target}")
                    with target.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != expected:
                        raise BuildError("G4 Wave 5C", f"Prior output changed: {target}")
                    paths.add(relative)
        except (OSError, ValueError, TypeError) as exc:
            raise BuildError("G4 Wave 5C", f"Invalid prior manifest {path}: {exc}") from exc
    return len(paths)


class Wave5CTargetValidator(Wave3BTargetValidator):
    def __init__(
        self,
        environment: Any,
        identity_key: str,
        archives: Path | None = None,
        downloader: Downloader | None = None,
    ):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave5c-acceptance.json"
        self.archives = archives or PROJECT_ROOT / "third_party/sources"
        self.downloader = downloader

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        return _SPECS[component.name]

    def dependency_identity(self) -> dict[str, Any]:
        wave2 = Wave2TargetValidator(self.environment, self.identity_key)
        component = ComponentRegistry().get_by_name("lcms2")
        if component is None or not wave2.manifest_matches(component):
            raise BuildError("lcms2", "Prior accepted target lcms2 manifest does not match.")
        wave2.validate_component(component)
        manifest = wave2._read_manifest()
        if manifest is None:
            raise BuildError("lcms2", "Prior accepted target lcms2 manifest is missing.")
        return {
            "lcms2": manifest["components"]["lcms2"]["sha256"],
            "sources": {
                item.name: {"revision": item.revision, "sha256": item.sha256}
                for item in DEPENDENCIES
            },
            "recipe": "wave5c-v1",
        }

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        entry = manifest.get("components", {}).get(component.name, {}) if manifest else {}
        if entry.get("recipe") != "wave5c-v1":
            return False
        if (
            component.name == "libjxl"
            and entry.get("dependency_identity") != self.dependency_identity()
        ):
            return False
        return Wave3ATargetValidator.manifest_matches(self, component)

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        assert manifest is not None
        entry = manifest["components"][component.name]
        entry["recipe"] = "wave5c-v1"
        if component.name == "libjxl":
            entry["dependency_identity"] = self.dependency_identity()
        else:
            entry["runtime_prerequisite"] = "external VSScript.dll"
        self._write_manifest(manifest)

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        for relative in (*spec.headers, *spec.libraries):
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing target output: {relative}")
            self._require_inside_prefix(component.name, path)
        if component.name == "VapourSynth":
            self._validate_vapoursynth(component)
        else:
            for relative in spec.libraries:
                archive = self.prefix / relative
                self._validate_archive(component.name, archive)
                listed = subprocess.run(
                    [str(self.environment.target_tools.ar), "t", str(archive)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=120,
                ).stdout.splitlines()
                inspected = subprocess.run(
                    [str(self.environment.target_tools.readobj), "--file-headers", str(archive)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=120,
                ).stdout
                if not listed or len(
                    re.findall(r"^Format:\s*COFF-x86-64$", inspected, re.MULTILINE)
                ) != len(listed):
                    raise BuildError(component.name, f"Invalid archive members: {archive}")
                symbols = subprocess.run(
                    [str(self.environment.target_tools.nm), "-u", str(archive)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=120,
                ).stdout
                if re.search(r"__kmpc|GOMP_|msys-2\.0|cygwin", symbols, re.IGNORECASE):
                    raise BuildError(component.name, f"Unexpected runtime reference in {archive}")
                print(f"Wave-5C {archive.name}: {len(listed)} COFF members", flush=True)
            for name in _JXL_PC:
                path = self.prefix / "lib/pkgconfig" / f"{name}.pc"
                if not path.is_file():
                    raise BuildError(component.name, f"Missing pkg-config metadata: {path}")
                Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(
                    path
                )
            self._link_and_run(component)
        return self._owned_outputs(component)

    def _validate_vapoursynth(self, component: Component) -> None:
        ffmpeg = ComponentRegistry().get_by_name("ffmpeg")
        if ffmpeg is None:
            raise BuildError(component.name, "Pinned FFmpeg source is not registered.")
        ffmpeg = ffmpeg.with_version("9.0")
        try:
            archive = (
                self.downloader.download(
                    ffmpeg.get_url(),
                    ffmpeg.get_archive_filename(),
                    expected_sha256=ffmpeg.sha256,
                    show_progress=False,
                )
                if self.downloader is not None
                else self.archives / ffmpeg.get_archive_filename()
            )
            with archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != ffmpeg.sha256:
                raise BuildError(component.name, "FFmpeg source checksum mismatch.")
            with tarfile.open(archive, "r:*") as tar:
                config_file = tar.extractfile("FFmpeg-n9.0/configure")
                source_file = tar.extractfile("FFmpeg-n9.0/libavformat/vapoursynth.c")
                if config_file is None or source_file is None:
                    raise BuildError(component.name, "Pinned FFmpeg source files are missing.")
                configure = config_file.read().decode()
                source = source_file.read().decode()
        except (OSError, RuntimeError, KeyError, tarfile.TarError) as exc:
            raise BuildError(
                component.name, f"Cannot inspect pinned FFmpeg contract: {exc}"
            ) from exc
        if (
            'require_headers "vapoursynth/VSScript4.h vapoursynth/VapourSynth4.h"' not in configure
            or "VSScript.dll" not in source
            or "getVSScriptAPI" not in source
        ):
            raise BuildError(component.name, "Pinned FFmpeg VapourSynth contract changed.")
        env = dict(self.environment.compiler_environment)
        result = subprocess.run(
            [
                str(self.environment.target_tools.cc),
                "-E",
                "-x",
                "c",
                "-I",
                str(self.prefix / "include"),
                "-",
            ],
            input="#include <vapoursynth/VSScript4.h>\n#include <vapoursynth/VapourSynth4.h>\n",
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise BuildError(component.name, f"FFmpeg header probe failed: {result.stderr[-3000:]}")
        print(
            "Wave-5C VapourSynth: FFmpeg header probe passed; VSScript.dll is external", flush=True
        )

    def _link_and_run(self, component: Component) -> None:
        env = dict(self.environment.compiler_environment)
        flags = self._pkg_config(
            component.name, ["--static", "--cflags", "--libs", "libjxl_threads", "libjxl"], env
        )
        self._validate_target_flags(component.name, flags)
        tokens = shlex.split(flags)
        if "-lstdc++" in tokens or not {
            "-ljxl",
            "-ljxl_threads",
            "-ljxl_cms",
            "-lhwy",
            "-lbrotlienc",
            "-lbrotlidec",
            "-lbrotlicommon",
            "-llcms2",
            "-lc++",
            "-lunwind",
        }.issubset(tokens):
            raise BuildError(component.name, "Host C++ runtime in target metadata.")
        thread_flags = self._pkg_config(
            component.name, ["--static", "--cflags", "--libs", "libjxl_threads"], env
        )
        if not {"-ljxl_threads", "-lc++", "-lunwind"}.issubset(shlex.split(thread_flags)):
            raise BuildError(component.name, "Thread runner static metadata is incomplete.")
        with tempfile.TemporaryDirectory(
            prefix="g4-wave5c-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            directory = Path(tmp)
            source = directory / "smoke.c"
            executable = directory / "smoke.exe"
            source.write_text(_SMOKE)
            thread_source = directory / "threads.c"
            thread_source.write_text(
                "#include <jxl/thread_parallel_runner.h>\n"
                "int main(void) { void *r = JxlThreadParallelRunnerCreate(0, 1); "
                "if (!r) return 1; JxlThreadParallelRunnerDestroy(r); return 0; }\n"
            )
            thread_result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    str(thread_source),
                    *shlex.split(thread_flags),
                    "-o",
                    str(directory / "threads.exe"),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if thread_result.returncode:
                raise BuildError(
                    component.name, f"Thread metadata link failed: {thread_result.stderr[-3000:]}"
                )
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    *[v for v in shlex.split(env.get("CFLAGS", "")) if v != "-fopenmp"],
                    str(source),
                    *shlex.split(flags),
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
                    component.name, f"Target metadata link failed: {result.stderr[-5000:]}"
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component.name, executable
            )
            imports = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(executable)],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            ).stdout
            dlls = set(re.findall(r"^\s*Name: (.+)$", imports, re.MULTILINE))
            print(f"Wave-5C PE imports: {sorted(dlls)}", flush=True)
            runtime = self.environment.target_tools.prefix / "x86_64-w64-mingw32/bin"
            if not {"libc++.dll", "libunwind.dll"}.issubset(dlls):
                raise BuildError(
                    component.name, "Expected LLVM-MinGW C++ runtime imports are missing."
                )
            for name in dlls:
                path = runtime / name
                if path.is_file():
                    shutil.copyfile(path, directory / name)
                elif name in {"libc++.dll", "libunwind.dll"}:
                    raise BuildError(component.name, f"Missing target runtime DLL: {path}")
            path = (
                subprocess.run(
                    ["wslpath", "-w", str(executable)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                .stdout.strip()
                .replace("'", "''")
            )
            try:
                runtime_result = WindowsInterop().run_powershell(
                    f"$ErrorActionPreference='Stop'; & '{path}'; exit $LASTEXITCODE", timeout=60
                )
            except WindowsProbeError as exc:
                raise BuildError(component.name, f"Windows smoke failed: {exc}") from exc
            if runtime_result.returncode:
                raise BuildError(component.name, f"Windows smoke failed: {runtime_result.stderr!r}")


def build_or_reuse(
    component: Component, builder: Any, manager: Any, validator: Wave5CTargetValidator
) -> bool:
    prior = manager.get().components.get(component.name)
    if prior and prior.status == ComponentStatus.COMPLETED:
        if not validator.manifest_matches(component):
            raise BuildError(
                component.name, "Accepted output or dependency changed; refusing reuse."
            )
        metadata_paths = (
            [
                validator.prefix / "lib/pkgconfig" / f"{name}.pc"
                for name in ("libjxl", "libjxl_threads")
            ]
            if component.name == "libjxl"
            else []
        )
        old_metadata = {path: path.read_bytes() for path in metadata_paths}
        try:
            changed_metadata = (
                normalize_cross_metadata(validator.prefix) if metadata_paths else False
            )
            validator.validate_component(component)
            if changed_metadata:
                validator.record_component(component, validator._owned_outputs(component))
        except Exception:
            for path, data in old_metadata.items():
                path.write_bytes(data)
            raise
        return False
    if validator._owned_outputs(component):
        raise BuildError(component.name, "Unaccepted outputs already exist; refusing overwrite.")
    try:
        builder._wave5c_created_outputs = []
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-5C target build",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="Wave-5C target acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        for path in builder._wave5c_created_outputs:
            if path.is_file() or path.is_symlink():
                path.unlink()
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-5C build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-5C build or acceptance failed: {exc}") from exc


def run_g4_wave5c(app: Any, config: BuildConfig) -> int:
    if (
        config.target_backend != TARGET_BACKEND_CROSS_MINGW
        or not config.native_build
        or not config.openmp
    ):
        raise BuildError("G4 Wave 5C", "Cross backend and native/OpenMP profile required.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        env = app.prepare_cross_environment(config, session)
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
        sources = verify_sources(app.registry, archives, builder.downloader)
        validator = Wave5CTargetValidator(
            env, session.identity.build_key, archives, builder.downloader
        )
        validator.validate_prefix()
        prior_count = validate_prior_manifests(
            validator.workspace, validator.prefix, validator.identity_key
        )
        print(f"Wave-5C: verified {prior_count} prior manifest-recorded files", flush=True)
        validator.dependency_identity()
        for dependency in DEPENDENCIES:
            builder.downloader.download(
                dependency.url,
                dependency.filename,
                expected_sha256=dependency.sha256,
                show_progress=False,
            )
        built: list[str] = []
        reused: list[str] = []
        for component in selected:
            changed = build_or_reuse(component, builder, app.state_manager, validator)
            (built if changed else reused).append(component.name)
            print(
                f"Wave-5C {component.name}: {'built' if changed else 'revalidated and reused'}",
                flush=True,
            )
        manifest = validator._read_manifest()
        assert manifest is not None
        manifest.update(source_sha256=sources, built=built, reused=reused, ffmpeg_built=False)
        validator._write_manifest(manifest)
        validator.validate_prefix()
        if (
            validate_prior_manifests(validator.workspace, validator.prefix, validator.identity_key)
            != prior_count
        ):
            raise BuildError("G4 Wave 5C", "Prior manifest inventory changed during build.")
    except (BuildError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-5C build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 5C built={built}, reused={reused}", flush=True)
    return 0
