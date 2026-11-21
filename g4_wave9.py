"""Accept the configured physical-Windows hardware interfaces."""

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
from .components import Component
from .config import TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g4_wave2 import Wave2TargetValidator
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave6 import validate_prior_manifests
from .g4_wave7 import Wave7Validator
from .state import ComponentStatus
from .windows_probe import FeatureState, WindowsInterop, WindowsProbeError

SPEC = _OutputSpec(
    "",
    ("include/CL/cl.h", "include/CL/cl_platform.h"),
    ("lib/libOpenCL.a",),
    ("include/CL", "lib/libOpenCL.a"),
    "",
)

SMOKE = r"""
#define CL_TARGET_OPENCL_VERSION 120
#include <CL/cl.h>
#include <stdio.h>
int main(void) {
    cl_platform_id platforms[32];
    cl_uint count = 0;
    cl_device_id device = NULL;
    cl_int err;
    if (clGetPlatformIDs(32, platforms, &count) || !count || count > 32) return 1;
    for (cl_uint i = 0; i < count && !device; ++i) {
        cl_device_id devices[64];
        cl_uint n = 0;
        if (clGetDeviceIDs(platforms[i], DEVICE_TYPE, 64, devices, &n)) continue;
        for (cl_uint j = 0; j < n && j < 64; ++j) {
            cl_uint vendor = 0;
            if (!clGetDeviceInfo(devices[j], CL_DEVICE_VENDOR_ID, sizeof(vendor), &vendor, NULL)
                && (EXPECTED_VENDOR == 0 || vendor == EXPECTED_VENDOR)) {
                device = devices[j];
                break;
            }
        }
    }
    if (!device) return 2;
    char name[256] = {0};
    if (clGetDeviceInfo(device, CL_DEVICE_NAME, sizeof(name), name, NULL)) return 3;
    cl_context context = clCreateContext(NULL, 1, &device, NULL, NULL, &err);
    if (!context || err) return 4;
    cl_command_queue queue = clCreateCommandQueue(context, device, 0, &err);
    if (!queue || err) return 5;
    const char *code = "__kernel void test(__global int *x) { x[get_global_id(0)] += 7; }";
    cl_program program = clCreateProgramWithSource(context, 1, &code, NULL, &err);
    if (!program || err || clBuildProgram(program, 1, &device, NULL, NULL, NULL)) return 6;
    cl_kernel kernel = clCreateKernel(program, "test", &err);
    if (!kernel || err) return 7;
    int data[4] = {1, 2, 3, 4};
    cl_mem buffer = clCreateBuffer(context, CL_MEM_READ_WRITE | CL_MEM_COPY_HOST_PTR,
                                   sizeof(data), data, &err);
    if (!buffer || err || clSetKernelArg(kernel, 0, sizeof(buffer), &buffer)) return 8;
    size_t size = 4;
    if (clEnqueueNDRangeKernel(queue, kernel, 1, NULL, &size, NULL, 0, NULL, NULL)
        || clEnqueueReadBuffer(queue, buffer, CL_TRUE, 0, sizeof(data), data, 0, NULL, NULL))
        return 9;
    if (data[0] != 8 || data[1] != 9 || data[2] != 10 || data[3] != 11) return 10;
    clReleaseMemObject(buffer);
    clReleaseKernel(kernel);
    clReleaseProgram(program);
    clReleaseCommandQueue(queue);
    clReleaseContext(context);
    printf("OpenCL device: %s; output: %d %d %d %d\n", name, data[0], data[1], data[2], data[3]);
    return 0;
}
"""


def select_components(app: Any, config: BuildConfig) -> list[Component]:
    state = app.windows_target_state
    if state is None or state.opencl.state == FeatureState.UNAVAILABLE:
        raise BuildError("G4 Wave 9", "Physical Windows OpenCL capability is unknown.")
    unsupported = [
        gpu.name
        for gpu in state.gpus
        if gpu.adapter_type == "hardware" and (gpu.vendor_id or "").lower() in {"1002", "8086"}
    ]
    if unsupported:
        raise BuildError(
            "G4 Wave 9",
            "Target AMD/Intel GPU interfaces require selection and implementation: "
            + ", ".join(unsupported),
        )
    selection = app._get_component_selection(config)
    if state.opencl.state != FeatureState.PRESENT:
        return []
    conditional = {c.name for c in selection.conditional_components}
    components = [c for c in selection.target_inputs if c.name == "opencl-headers"]
    if len(components) != 1 or "opencl-headers" in conditional:
        raise BuildError("opencl-headers", "Windows OpenCL target input was not selected.")
    return components


class Wave9Validator(Wave7Validator):
    def __init__(self, environment: Any, identity: str, sources: dict[str, str], target_state: Any):
        super().__init__(environment, identity, sources)
        self.manifest_path = self.workspace / "g4-wave9-acceptance.json"
        self.target_state = target_state

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        if component.name != "opencl-headers":
            raise BuildError(component.name, "No Wave-9 output contract.")
        return SPEC

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest() or {}
        entry = manifest.get("components", {}).get(component.name, {})
        return (
            entry.get("recipe") == "wave9-v1"
            and entry.get("sources") == self.sources
            and Wave3ATargetValidator.manifest_matches(self, component)
        )

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        assert manifest is not None
        manifest["components"][component.name].update(recipe="wave9-v1", sources=self.sources)
        self._write_manifest(manifest)

    def validate_component(self, component: Component) -> list[str]:
        for relative in (*SPEC.headers, *SPEC.libraries):
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing target output: {relative}")
            self._require_inside_prefix(component.name, path)
        Wave2TargetValidator(self.environment, self.identity_key)._validate_archive(
            component.name, self.prefix / "lib/libOpenCL.a"
        )
        self._smoke(component)
        return self._owned_outputs(component)

    def _smoke(self, component: Component) -> None:
        devices = [d for platform in self.target_state.opencl.platforms for d in platform.devices]
        if not devices:
            raise BuildError(component.name, "Windows OpenCL reports no available device.")
        device = next((d for d in devices if d.device_type == "gpu"), devices[0])
        device_type = "CL_DEVICE_TYPE_GPU" if device.device_type == "gpu" else "CL_DEVICE_TYPE_ALL"
        try:
            vendor = int(device.vendor_id, 16) if device.vendor_id else 0
        except ValueError as exc:
            raise BuildError(component.name, "Invalid Windows OpenCL vendor ID.") from exc
        source_text = SMOKE.replace("DEVICE_TYPE", device_type).replace(
            "EXPECTED_VENDOR", str(vendor)
        )
        env = dict(self.environment.compiler_environment)
        with tempfile.TemporaryDirectory(
            prefix="g4-wave9-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            root = Path(tmp)
            source, exe = root / "consumer.c", root / "consumer.exe"
            source.write_text(source_text, encoding="utf-8")
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    f"-I{self.prefix / 'include'}",
                    *shlex.split(env.get("CFLAGS", "")),
                    str(source),
                    str(self.prefix / "lib/libOpenCL.a"),
                    *shlex.split(env.get("LDFLAGS", "")),
                    "-o",
                    str(exe),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode:
                raise BuildError(
                    component.name, f"OpenCL target link failed: {result.stderr[-6000:]}"
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component.name, exe
            )
            inspected = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(exe)],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            ).stdout
            dlls = re.findall(r"^\s*Name: (.+)$", inspected, re.MULTILINE)
            if "opencl.dll" not in {dll.lower() for dll in dlls}:
                raise BuildError(component.name, f"PE does not import OpenCL.dll: {dlls}")
            print(f"Wave-9 PE imports: {sorted(dlls)}", flush=True)
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
                runtime = WindowsInterop().run_powershell(
                    f"$ErrorActionPreference='Stop'; & '{windows_path}'; exit $LASTEXITCODE",
                    timeout=90,
                )
            except WindowsProbeError as exc:
                raise BuildError(component.name, f"Windows OpenCL smoke failed: {exc}") from exc
            if runtime.returncode:
                raise BuildError(
                    component.name,
                    f"Windows OpenCL smoke failed ({runtime.returncode}): "
                    f"{runtime.stdout!r} {runtime.stderr!r}",
                )
            print(
                f"Wave-9 Windows smoke: {runtime.stdout.decode(errors='replace').strip()}",
                flush=True,
            )


def run_g4_wave9(app: Any, config: BuildConfig) -> int:
    if (
        config.target_backend != TARGET_BACKEND_CROSS_MINGW
        or not config.native_build
        or not config.openmp
    ):
        raise BuildError("G4 Wave 9", "Cross native/OpenMP profile required.")
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        env = app.prepare_cross_environment(config, session)
        components = select_components(app, config)
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
        if components:
            definition = app.registry.get_by_name("opencl-icd-loader")
            if definition is None or not definition.sha256 or not components[0].sha256:
                raise BuildError("opencl-headers", "Missing pinned OpenCL source definitions.")
            for component in (components[0], definition):
                path = builder.downloader.download(
                    component.get_url(),
                    component.get_archive_filename(),
                    expected_sha256=component.sha256,
                    show_progress=False,
                )
                with Path(path).open("rb") as stream:
                    sources[component.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        validator = Wave9Validator(
            env, session.identity.build_key, sources, app.windows_target_state
        )
        validator.validate_prefix()
        count = validate_prior_manifests(
            validator.workspace,
            validator.prefix,
            validator.identity_key,
            validator.manifest_path.name,
        )
        print(f"Wave-9: verified {count} prior manifest-recorded files", flush=True)
        accepted, reused = [], []
        for component in components:
            state = app.state_manager.get().components.get(component.name)
            if state and state.status == ComponentStatus.COMPLETED:
                if not validator.manifest_matches(component):
                    raise BuildError(
                        component.name, "Accepted target input changed; refusing reuse."
                    )
                validator.validate_component(component)
                reused.append(component.name)
            else:
                try:
                    outputs = validator.validate_component(component)
                    validator.record_component(component, outputs)
                    app.state_manager.mark_component_status(
                        component.name,
                        ComponentStatus.COMPLETED,
                        component.version,
                        detail="Wave-9 target interface acceptance passed",
                    )
                except (BuildError, OSError, subprocess.SubprocessError) as exc:
                    app.state_manager.mark_component_status(
                        component.name,
                        ComponentStatus.FAILED,
                        component.version,
                        str(exc),
                        detail="Wave-9 target interface acceptance failed",
                    )
                    raise
                accepted.append(component.name)
        if (
            validate_prior_manifests(
                validator.workspace,
                validator.prefix,
                validator.identity_key,
                validator.manifest_path.name,
            )
            != count
        ):
            raise BuildError("G4 Wave 9", "Prior manifest inventory changed.")
        manifest = validator._read_manifest() or {
            "schema_version": 1,
            "identity_key": validator.identity_key,
            "target_prefix": str(validator.prefix),
            "components": {},
        }
        manifest.update(source_sha256=sources, accepted=accepted, reused=reused, ffmpeg_built=False)
        validator._write_manifest(manifest)
    except (BuildError, SkipComponent, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-9 acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 9 accepted={accepted}, reused={reused}", flush=True)
    return 0
