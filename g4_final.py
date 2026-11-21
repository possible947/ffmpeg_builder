"""Final FFmpeg integration against the accepted Windows target stack."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .builders.graphics.glslang_sources import install_spirv_headers
from .config import TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g4_wave2 import Wave2TargetValidator
from .g4_wave6 import validate_prior_manifests
from .g4_wave9 import select_components as validate_target_capabilities
from .state import ComponentStatus
from .target_inputs import install_nv_codec_pkg_config
from .windows_probe import WindowsInterop, WindowsProbeError


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _accepted_names(workspace: Path) -> set[str]:
    result: set[str] = set()
    for path in workspace.glob("g4-wave*-acceptance.json"):
        if path.name == "g4-wave2-openmp-acceptance.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        result.update(data.get("components", {}))
    return result


def _installed_hashes(prefix: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in (
        "avcodec",
        "avdevice",
        "avfilter",
        "avformat",
        "avutil",
        "swresample",
        "swscale",
        "postproc",
    ):
        paths = [
            prefix / "lib" / f"lib{name}.a",
            prefix / "lib/pkgconfig" / f"lib{name}.pc",
        ]
        include = prefix / "include" / f"lib{name}"
        if include.is_dir():
            paths.extend(path for path in include.rglob("*") if path.is_file())
        for path in paths:
            if path.is_file():
                result[path.relative_to(prefix).as_posix()] = _digest(path)
    if "lib/libavcodec.a" not in result or "include/libavcodec/avcodec.h" not in result:
        raise BuildError("ffmpeg", "FFmpeg target library or headers are missing.")
    return result


def _interface_hashes(prefix: Path) -> dict[str, str]:
    paths = [prefix / "lib/pkgconfig/ffnvcodec.pc"]
    spirv = prefix / "include/spirv"
    if spirv.is_dir():
        paths.extend(path for path in spirv.rglob("*") if path.is_file())
    return {path.relative_to(prefix).as_posix(): _digest(path) for path in paths if path.is_file()}


def _imports(environment: Any, binary: Path) -> set[str]:
    result = subprocess.run(
        [str(environment.target_tools.readobj), "--coff-imports", str(binary)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return set(re.findall(r"^\s*Name: (.+)$", result.stdout, re.MULTILINE))


def _validate_config(source: Path, flags: list[str], cuda: bool, vmaf_cuda: bool) -> None:
    config = (source / "config.h").read_text(encoding="utf-8")
    names = {
        flag.removeprefix("--enable-").replace("-", "_").upper()
        for flag in flags
        if flag.startswith("--enable-")
    }
    if cuda:
        names.update({"CUDA_NVCC", "FFNVCODEC", "CUVID", "NVDEC", "NVENC"})
    missing = [
        name for name in sorted(names) if not re.search(rf"^#define CONFIG_{name} 1$", config, re.M)
    ]
    if missing:
        raise BuildError("ffmpeg", f"Configured FFmpeg features missing: {', '.join(missing)}")
    if "--enable-vulkan" in flags and not re.search(
        r"^#define HAVE_SPIRV_(?:HEADERS|UNIFIED1)_SPIRV_H 1$", config, re.M
    ):
        raise BuildError("ffmpeg", "Vulkan SPIR-V headers were not detected.")
    expected_components: set[str] = set()
    if cuda:
        expected_components.update(
            {"SCALE_CUDA_FILTER", "H264_NVENC_ENCODER", "H264_CUVID_DECODER"}
        )
    if vmaf_cuda:
        expected_components.add("LIBVMAF_CUDA_FILTER")
    if "--enable-vulkan" in flags:
        expected_components.add("SCALE_VULKAN_FILTER")
    if "--enable-libplacebo" in flags:
        expected_components.add("LIBPLACEBO_FILTER")
    if "--enable-opencl" in flags:
        expected_components.add("OVERLAY_OPENCL_FILTER")
    components = (source / "config_components.h").read_text(encoding="utf-8")
    for name in sorted(expected_components):
        if not re.search(rf"^#define CONFIG_{name} 1$", components, re.M):
            raise BuildError("ffmpeg", f"Configured FFmpeg component missing: {name}")


def _windows_smoke(environment: Any, binaries: list[Path], version: str) -> dict[str, list[str]]:
    validator = Wave2TargetValidator(environment, "")
    imports: set[str] = set()
    for binary in binaries:
        validator._validate_executable("ffmpeg", binary)
        imports.update(_imports(environment, binary))
    runtime_dir = environment.target_tools.prefix / "x86_64-w64-mingw32/bin"
    available = {path.name.lower(): path for path in runtime_dir.glob("*.dll")}
    copied: set[str] = set()
    pending = list(imports)
    with tempfile.TemporaryDirectory(
        prefix="g4-ffmpeg-", dir=environment.paths.generated_files_root
    ) as scratch:
        root = Path(scratch)
        for binary in binaries:
            shutil.copy2(binary, root / binary.name)
        while pending:
            name = pending.pop()
            key = name.lower()
            if key in copied or key not in available:
                continue
            copied.add(key)
            dll = available[key]
            shutil.copy2(dll, root / dll.name)
            pending.extend(_imports(environment, dll))
        windows_root = (
            subprocess.run(
                ["wslpath", "-w", str(root)],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            .stdout.strip()
            .replace("'", "''")
        )
        script = (
            f"Set-Location -LiteralPath '{windows_root}'; "
            "$v = & .\\ffmpeg.exe -hide_banner -version; "
            f"if ($LASTEXITCODE -or !$v[0].StartsWith('ffmpeg version {version} ')) {{ exit 1 }}; "
            "Write-Output $v; "
            "$v = & .\\ffprobe.exe -hide_banner -version; "
            f"if ($LASTEXITCODE -or !$v[0].StartsWith('ffprobe version {version} ')) {{ exit 2 }}; "
            "Write-Output $v; "
            "& .\\ffmpeg.exe -v error -f lavfi -i testsrc2=size=128x96:rate=5 "
            "-frames:v 8 -c:v libx264 -preset ultrafast -y smoke.mp4; "
            "if ($LASTEXITCODE) { exit 3 }; "
            "$codec = & .\\ffprobe.exe -v error -select_streams v:0 "
            "-show_entries stream=codec_name -of csv=p=0 smoke.mp4; "
            "if ($LASTEXITCODE -or $codec -ne 'h264') { exit 4 }; "
            "& .\\ffmpeg.exe -v error -i smoke.mp4 -f null -; "
            "if ($LASTEXITCODE) { exit 5 }; Write-Output 'G4 software smoke passed'; exit 0"
        )
        try:
            result = WindowsInterop().run_powershell(script, timeout=180)
        except WindowsProbeError as exc:
            raise BuildError("ffmpeg", f"Windows acceptance failed: {exc}") from exc
        if result.returncode or b"G4 software smoke passed" not in result.stdout:
            raise BuildError(
                "ffmpeg",
                f"Windows acceptance failed ({result.returncode}): "
                f"{result.stdout!r} {result.stderr!r}",
            )
        print(result.stdout.decode(errors="replace").strip(), flush=True)
    return {"imports": sorted(imports), "toolchain_dlls": sorted(copied)}


def run_g4_final(app: Any, config: BuildConfig) -> int:
    """Build and accept FFmpeg only after all selected G4 inputs are accepted."""
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW:
        raise BuildError("G4", "Final acceptance requires the linux-cross-mingw backend.")
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
    except BuildError as exc:
        print(f"BLOCKED by identity guard or cross-environment preparation: {exc}", flush=True)
        return 2
    builder = None
    component = None
    build_attempted = False
    try:
        selection = app._get_component_selection(config)
        validate_target_capabilities(app, config)
        selected = (
            selection.target_inputs
            + selection.target_components_to_build
            + selection.mixed_components
        )
        ffmpeg = [item for item in selected if item.name == "ffmpeg"]
        if len(ffmpeg) != 1:
            raise BuildError("G4", "FFmpeg was not selected exactly once.")
        component = ffmpeg[0]
        dependencies = [item for item in selected if item.name != "ffmpeg"]
        accepted = _accepted_names(session.workspace)
        for item in dependencies:
            if item.name not in accepted or not app.state_manager.is_component_completed(
                item.name, item.version
            ):
                raise BuildError(item.name, "Selected target dependency has not been accepted.")
        prior_count = validate_prior_manifests(
            session.workspace, environment.paths.target_prefix, session.identity.build_key, ""
        )
        print(f"G4: verified {prior_count} accepted dependency files", flush=True)
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, [component]),
            cross_environment=environment,
        )
        if "glslang" in {item.name for item in dependencies}:
            install_spirv_headers(builder)
        for nv in [item for item in dependencies if item.name == "nv-codec"]:
            archive = builder.downloader.download(
                nv.get_url(),
                nv.get_archive_filename(),
                expected_sha256=nv.sha256,
                show_progress=False,
            )
            install_nv_codec_pkg_config(
                nv,
                Path(archive),
                environment.paths.target_prefix,
                environment.target_input_manifest,
            )
        archive = builder.downloader.download(
            component.get_url(),
            component.get_archive_filename(),
            expected_sha256=component.sha256,
            show_progress=False,
        )
        source_hash = _digest(Path(archive))
        manifest_path = session.workspace / "g4-final-acceptance.json"
        binaries = [session.workspace / "bin" / name for name in ("ffmpeg.exe", "ffprobe.exe")]
        source = app.packages / component.get_target_dir()
        names = [item.name for item in dependencies]
        flags = builder.registry.get_ffmpeg_configure_flags(
            names,
            config.gpl_enabled,
            builder.platform,
            app.platform_info,
            config.ffmpeg_version,
            config.target_backend,
        )
        if (
            config.enable_libplacebo_vulkan
            and "libplacebo" in names
            and "--enable-vulkan" not in flags
        ):
            flags.append("--enable-vulkan")
        reused = app.state_manager.is_component_completed(component.name, component.version)
        if reused:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = {path.name: _digest(path) for path in binaries}
            if (
                manifest.get("identity_key") != session.identity.build_key
                or manifest.get("source_sha256") != source_hash
                or manifest.get("binaries") != expected
                or manifest.get("dependencies") != names
                or manifest.get("installed") != _installed_hashes(environment.paths.target_prefix)
                or manifest.get("interfaces") != _interface_hashes(environment.paths.target_prefix)
            ):
                raise BuildError(
                    "ffmpeg", "Accepted FFmpeg outputs or inputs changed; refusing reuse."
                )
        else:
            build_attempted = True
            builder.build_component(component)
        cuda = environment.cuda_host_tools.nvcc is not None and (
            config.gpl_enabled or config.allow_nonfree_cuda
        )
        _validate_config(
            source, flags, cuda, cuda and config.enable_libvmaf_cuda and config.enable_libvmaf
        )
        runtime = _windows_smoke(environment, binaries, component.version)
        if (
            validate_prior_manifests(
                session.workspace, environment.paths.target_prefix, session.identity.build_key, ""
            )
            != prior_count
        ):
            raise BuildError(
                "G4", "Accepted dependency inventory changed during FFmpeg integration."
            )
        manifest = {
            "identity_key": session.identity.build_key,
            "source_sha256": source_hash,
            "dependencies": names,
            "binaries": {path.name: _digest(path) for path in binaries},
            "installed": _installed_hashes(environment.paths.target_prefix),
            "interfaces": _interface_hashes(environment.paths.target_prefix),
            "runtime": runtime,
            "configure_flags": flags,
        }
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(manifest_path)
        app.state_manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="G4 FFmpeg Windows acceptance passed",
        )
        print(f"PASS: G4 FFmpeg {'reused' if reused else 'built'}; runtime={runtime}", flush=True)
        return 0
    except (
        BuildError,
        SkipComponent,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        if component is not None and build_attempted:
            app.state_manager.mark_component_status(
                component.name,
                ComponentStatus.FAILED,
                component.version,
                str(exc),
                detail="G4 FFmpeg acceptance failed",
            )
        print(f"FAIL: G4 FFmpeg integration: {exc}", flush=True)
        return 1
    finally:
        if builder is not None:
            builder.shutdown_downloads(wait=False)
