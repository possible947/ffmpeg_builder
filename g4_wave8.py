"""Bounded Windows-target CUDA libvmaf build and acceptance."""

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
from .config import TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave1 import Wave1TargetValidator
from .g4_wave2 import Wave2TargetValidator
from .g4_wave6 import validate_prior_manifests
from .g4_wave7 import Wave7Validator
from .state import ComponentStatus
from .windows_probe import WindowsInterop, WindowsProbeError

SPEC = _OutputSpec(
    "libvmaf",
    ("include/libvmaf/libvmaf.h", "include/libvmaf/libvmaf_cuda.h"),
    ("lib/libvmaf.a",),
    ("include/libvmaf", "lib/libvmaf.a", "lib/pkgconfig/libvmaf.pc"),
    "",
)
NV_CODEC_SPEC = _OutputSpec(
    "",
    ("include/ffnvcodec/dynlink_cuda.h", "include/ffnvcodec/dynlink_loader.h"),
    (),
    ("include/ffnvcodec",),
    "",
)
SMOKE = r"""
#include <libvmaf/libvmaf.h>
#include <libvmaf/libvmaf_cuda.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
int main(void) {
    VmafContext *vmaf = NULL;
    VmafCudaState *cuda = NULL;
    VmafConfiguration cfg = {0};
    if (vmaf_init(&vmaf, cfg)) return 1;
    if (vmaf_cuda_state_init(&cuda, (VmafCudaConfiguration){0})) return 2;
    if (vmaf_cuda_import_state(vmaf, cuda)) return 3;
    if (vmaf_use_feature(vmaf, "motion_cuda", NULL)) return 4;
    if (vmaf_use_feature(vmaf, "adm_cuda", NULL)) return 12;
    if (vmaf_use_feature(vmaf, "vif_cuda", NULL)) return 13;
    for (unsigned i = 0; i < 3; i++) {
        VmafPicture ref, dist;
        if (vmaf_picture_alloc(&ref, VMAF_PIX_FMT_YUV420P, 8, 64, 64)) return 5;
        if (vmaf_picture_alloc(&dist, VMAF_PIX_FMT_YUV420P, 8, 64, 64)) return 6;
        for (unsigned y = 0; y < 64; y++) {
            memset((char *)ref.data[0] + y * ref.stride[0], i * 40, 64);
            memset((char *)dist.data[0] + y * dist.stride[0], i * 40, 64);
        }
        if (vmaf_read_pictures(vmaf, &ref, &dist, i)) return 7;
    }
    if (vmaf_read_pictures(vmaf, NULL, NULL, 0)) return 8;
    double score = -1;
    if (vmaf_feature_score_at_index(vmaf, "VMAF_integer_feature_motion_score", &score, 1)) return 9;
    printf("CUDA motion score: %f\n", score);
    if (!isfinite(score) || score <= 0) return 10;
    if (vmaf_feature_score_at_index(vmaf, "VMAF_integer_feature_adm2_score", &score, 1) || !isfinite(score)) return 14;
    printf("CUDA adm score: %f\n", score);
    if (vmaf_feature_score_at_index(vmaf, "VMAF_integer_feature_vif_scale0_score", &score, 1) || !isfinite(score)) return 15;
    printf("CUDA vif score: %f\n", score);
    if (vmaf_close(vmaf)) return 11;
    return 0;
}
"""


class Wave8Validator(Wave7Validator):
    def __init__(self, environment: Any, identity: str, sources: dict[str, str]):
        super().__init__(environment, identity, sources)
        self.manifest_path = self.workspace / "g4-wave8-acceptance.json"

    @staticmethod
    def _spec(component: Any) -> _OutputSpec:
        return NV_CODEC_SPEC if component.name == "nv-codec" else SPEC

    def manifest_matches(self, component: Any) -> bool:
        manifest = self._read_manifest() or {}
        entry = manifest.get("components", {}).get(component.name, {})
        return (
            entry.get("recipe") == "wave8-v1"
            and entry.get("sources") == self.sources
            and Wave3ATargetValidator.manifest_matches(self, component)
        )

    def record_component(self, component: Any, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        assert manifest is not None
        manifest["components"][component.name].update(recipe="wave8-v1", sources=self.sources)
        self._write_manifest(manifest)

    def validate_component(self, component: Any) -> list[str]:
        spec = self._spec(component)
        paths = (*spec.headers, *spec.libraries)
        if component.name == "libvmaf":
            paths += ("lib/pkgconfig/libvmaf.pc",)
        for relative in paths:
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing target output: {relative}")
            self._require_inside_prefix(component.name, path)
        if component.name == "libvmaf":
            self._validate_archive(component.name, self.prefix / "lib/libvmaf.a")
            self._smoke(component)
        return self._owned_outputs(component)

    def _smoke(self, component: Any) -> None:
        env = dict(self.environment.compiler_environment)
        pc = self.prefix / "lib/pkgconfig/libvmaf.pc"
        Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(pc)
        flags = self._pkg_config(component.name, ["--static", "--cflags", "--libs", "libvmaf"], env)
        self._validate_target_flags(component.name, flags)
        if not {"-lvmaf", "-lc++", "-lunwind"}.issubset(shlex.split(flags)):
            raise BuildError(component.name, f"Incomplete static metadata: {flags}")
        with tempfile.TemporaryDirectory(
            prefix="g4-wave8-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            root = Path(tmp)
            source, exe = root / "consumer.c", root / "consumer.exe"
            source.write_text(SMOKE, encoding="utf-8")
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    *shlex.split(env.get("CFLAGS", "")),
                    str(source),
                    *shlex.split(flags),
                    *shlex.split(env.get("LDFLAGS", "")),
                    "-o",
                    str(exe),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode:
                raise BuildError(
                    component.name, f"Target consumer link failed: {result.stderr[-6000:]}"
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component.name, exe
            )
            output = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(exe)],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            ).stdout
            dlls = set(re.findall(r"^\s*Name: (.+)$", output, re.MULTILINE))
            print(f"Wave-8 PE imports: {sorted(dlls)}", flush=True)
            runtime = self.environment.target_tools.prefix / "x86_64-w64-mingw32/bin"
            for name in dlls:
                path = runtime / name
                if path.is_file():
                    shutil.copyfile(path, root / name)
            windows_path = (
                subprocess.run(
                    ["wslpath", "-w", str(exe)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                .stdout.strip()
                .replace("'", "''")
            )
            try:
                runtime_result = WindowsInterop().run_powershell(
                    f"$ErrorActionPreference='Stop'; & '{windows_path}'; exit $LASTEXITCODE",
                    timeout=90,
                )
            except WindowsProbeError as exc:
                raise BuildError(component.name, f"Windows CUDA smoke failed: {exc}") from exc
            if runtime_result.returncode:
                raise BuildError(
                    component.name,
                    f"Windows CUDA smoke failed ({runtime_result.returncode}): "
                    f"{runtime_result.stdout!r} {runtime_result.stderr!r}",
                )
            print(
                f"Wave-8 Windows CUDA smoke: {runtime_result.stdout.decode(errors='replace').strip()}",
                flush=True,
            )


def run_g4_wave8(app: Any, config: BuildConfig) -> int:
    if (
        config.target_backend != TARGET_BACKEND_CROSS_MINGW
        or not config.native_build
        or not config.openmp
        or not config.enable_libvmaf
        or not config.enable_libvmaf_cuda
    ):
        raise BuildError("G4 Wave 8", "Cross native/OpenMP profile with CUDA libvmaf required.")
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        env = app.prepare_cross_environment(config, session)
        selection = app._get_component_selection(config)
        components = [c for c in selection.target_components_to_build if c.name == "libvmaf"]
        inputs = [c for c in selection.target_inputs if c.name == "nv-codec"]
        if len(components) != 1 or len(inputs) != 1:
            raise BuildError("G4 Wave 8", "Configured libvmaf or nv-codec input is missing.")
        component = components[0]
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, components),
            cross_environment=env,
        )
        sources = {}
        for source in (inputs[0], app.registry.get_nv_codec_component(), component):
            path = builder.downloader.download(
                source.get_url(),
                source.get_archive_filename(),
                expected_sha256=source.sha256,
                show_progress=False,
            )
            with Path(path).open("rb") as stream:
                sources[f"{source.name}-{source.version}"] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
        validator = Wave8Validator(env, session.identity.build_key, sources)
        validator.validate_prefix()
        count = validate_prior_manifests(
            validator.workspace,
            validator.prefix,
            validator.identity_key,
            validator.manifest_path.name,
        )
        print(f"Wave-8: verified {count} prior manifest-recorded files", flush=True)
        nv_codec = inputs[0]
        nv_state = app.state_manager.get().components.get(nv_codec.name)
        if nv_state and nv_state.status == ComponentStatus.COMPLETED:
            if not validator.manifest_matches(nv_codec):
                raise BuildError(nv_codec.name, "Accepted target input changed; refusing reuse.")
            validator.validate_component(nv_codec)
            input_result = "reused"
        else:
            # prepare_cross_environment already validates the G2 target-input manifest.
            outputs = validator.validate_component(nv_codec)
            validator.record_component(nv_codec, outputs)
            app.state_manager.mark_component_status(
                nv_codec.name,
                ComponentStatus.COMPLETED,
                nv_codec.version,
                detail="Wave-8 target input acceptance passed",
            )
            input_result = "accepted"
        print(f"Wave-8 nv-codec target input: {input_result}", flush=True)
        state = app.state_manager.get().components.get(component.name)
        if state and state.status == ComponentStatus.COMPLETED:
            if not validator.manifest_matches(component):
                raise BuildError(
                    component.name, "Accepted output or source changed; refusing reuse."
                )
            validator.validate_component(component)
            built, reused = [], [component.name]
        else:
            if validator._owned_outputs(component):
                raise BuildError(component.name, "Unaccepted outputs exist; refusing overwrite.")
            created: list[Path] = []
            setattr(builder, "_wave7_created_outputs", created)
            try:
                app.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.PENDING,
                    component.version,
                    detail="Wave-8 target build",
                )
                builder.build_component(component)
                outputs = validator.validate_component(component)
                validator.validate_prefix()
                validator.record_component(component, outputs)
                app.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.COMPLETED,
                    component.version,
                    detail="Wave-8 acceptance passed",
                )
            except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
                for path in created:
                    path.unlink(missing_ok=True)
                app.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.FAILED,
                    component.version,
                    str(exc),
                    detail="Wave-8 build or acceptance failed",
                )
                raise
            built, reused = [component.name], []
        if (
            validate_prior_manifests(
                validator.workspace,
                validator.prefix,
                validator.identity_key,
                validator.manifest_path.name,
            )
            != count
        ):
            raise BuildError("G4 Wave 8", "Prior manifest inventory changed.")
        manifest = validator._read_manifest()
        assert manifest is not None
        manifest.update(
            source_sha256=sources,
            built=built,
            reused=reused,
            target_input=input_result,
            ffmpeg_built=False,
        )
        validator._write_manifest(manifest)
    except (BuildError, SkipComponent, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-8 build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 8 built={built}, reused={reused}", flush=True)
    return 0
