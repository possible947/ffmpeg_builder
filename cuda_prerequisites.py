"""WSL-Mingw CUDA SDK and project-managed NVCC host compiler resolution."""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from .build_types import (
    CATEGORY_CUDA_COMPATIBILITY,
    CATEGORY_EXTERNAL_PREREQUISITE,
    CATEGORY_TARGET_DATA,
    CATEGORY_TOOLCHAIN_INSTALL,
    BuildError,
)
from .config import BuildConfig
from .external_prerequisites import (
    ExternalPrerequisiteValidation,
    RESOLUTION_CONFIG,
    resolve_external_prerequisite,
)
from .toolchain_registry import ToolchainRegistry
from .toolchain_store import STATUS_ABSENT, STATUS_COMPATIBLE, ToolchainStore
from .windows_probe import FeatureState, WindowsTargetState

CUDA_ENVIRONMENT_VARIABLES = ("CUDA_HOME", "CUDA_PATH")


@dataclass(frozen=True)
class ResolvedCudaBuildHostInputs:
    """Selected external CUDA tools and project-owned NVCC host compiler."""

    cuda_root: Path
    nvcc_path: Path
    detected_cuda_version: str
    ccbin_path: Path
    ccbin_profile: str
    ccbin_profile_version: str
    ccbin_provenance: Mapping[str, Any]
    resolution_source: str
    diagnostics: tuple[str, ...]
    nvcc_version: str = ""
    compute_architecture: Optional[str] = None
    probe_status: str = "not_run"
    effective_flags: tuple[str, ...] = ()
    compatibility_workaround: Optional[str] = None
    ccbin_version: Optional[str] = None
    probe_diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a serialization-friendly diagnostic representation."""
        return {
            "cuda_root": str(self.cuda_root),
            "nvcc_path": str(self.nvcc_path),
            "detected_cuda_version": self.detected_cuda_version,
            "ccbin_path": str(self.ccbin_path),
            "ccbin_profile": self.ccbin_profile,
            "ccbin_profile_version": self.ccbin_profile_version,
            "ccbin_provenance": dict(self.ccbin_provenance),
            "resolution_source": self.resolution_source,
            "diagnostics": list(self.diagnostics),
            "nvcc_version": self.nvcc_version,
            "compute_architecture": self.compute_architecture,
            "probe_status": self.probe_status,
            "effective_flags": list(self.effective_flags),
            "compatibility_workaround": self.compatibility_workaround,
            "ccbin_version": self.ccbin_version,
            "probe_diagnostics": list(self.probe_diagnostics),
        }


def _validate_cuda_root(root: Path) -> ExternalPrerequisiteValidation:
    """Validate the selected root's NVCC executable and reported toolkit version."""
    candidate = root / "bin" / "nvcc"
    try:
        nvcc = candidate.resolve(strict=True)
        nvcc.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return ExternalPrerequisiteValidation(
            usable=False,
            diagnostics=(f"expected executable {candidate} within the selected CUDA root: {exc}",),
        )
    if not nvcc.is_file():
        return ExternalPrerequisiteValidation(
            usable=False, diagnostics=(f"selected NVCC path {nvcc} is not a file",)
        )
    if not os.access(nvcc, os.X_OK):
        return ExternalPrerequisiteValidation(
            usable=False, diagnostics=(f"selected NVCC path {nvcc} is not executable",)
        )

    try:
        result = subprocess.run(
            [str(nvcc), "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ExternalPrerequisiteValidation(
            usable=False, diagnostics=(f"could not run {nvcc} --version: {exc}",)
        )
    output = (result.stdout or "") + (result.stderr or "")
    match = re.search(r"release\s+(\d+\.\d+)", output)
    if result.returncode != 0 or match is None:
        detail = output.strip() or f"nvcc --version exited with status {result.returncode}"
        return ExternalPrerequisiteValidation(
            usable=False,
            diagnostics=(f"could not identify the selected CUDA toolkit version: {detail}",),
        )

    version = match.group(1)
    full_version_match = re.search(r"\bV(\d+\.\d+(?:\.\d+)*)\b", output)
    nvcc_version = full_version_match.group(1) if full_version_match else version
    return ExternalPrerequisiteValidation(
        host_tools={"nvcc": nvcc},
        diagnostic_metadata={
            "nvcc_path": str(nvcc),
            "reported_cuda_version": version,
            "nvcc_version": nvcc_version,
        },
        effective_values={"cuda_version": version, "nvcc_version": nvcc_version},
    )


def _toolchain_error(code: str, message: str) -> BuildError:
    return BuildError(
        "CUDA host compiler",
        message,
        category=CATEGORY_TOOLCHAIN_INSTALL,
        code=code,
    )


def resolve_wsl_mingw_cuda_inputs(
    config: BuildConfig,
    registry: ToolchainRegistry,
    store: ToolchainStore,
    *,
    required: bool = True,
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[ResolvedCudaBuildHostInputs]:
    """Resolve CUDA from its configured root and GCC only from ToolchainStore."""
    external = resolve_external_prerequisite(
        "cuda",
        config.external_prerequisite_paths.get("cuda"),
        required=required,
        validator=_validate_cuda_root,
        environment=environment,
        environment_variables=CUDA_ENVIRONMENT_VARIABLES,
    )
    if external is None:
        return None

    version = str(external.effective_values["cuda_version"])
    nvcc_path = external.host_tools["nvcc"]
    profile_name = registry.toolchain_for_cuda(version)
    context = (
        f"CUDA {version} at {external.root}, NVCC {nvcc_path} (resolved from "
        f"{external.diagnostic_metadata['environment_variable'] or RESOLUTION_CONFIG})"
    )
    if profile_name is None:
        raise _toolchain_error(
            "cuda_host_mapping_missing",
            f"{context} has no project-managed host compiler mapping in toolchains.yaml; "
            "add a compatible CUDA-to-toolchain mapping.",
        )
    if registry.get_profile(profile_name) is None:
        raise _toolchain_error(
            "cuda_host_profile_missing",
            f"{context} maps to undeclared toolchain profile {profile_name!r}.",
        )

    status, reason = store.installed_status(registry, profile_name)
    if status != STATUS_COMPATIBLE:
        if status == STATUS_ABSENT:
            code = "cuda_host_toolchain_not_installed"
            action = f"Provision {profile_name!r} with the Toolchain Store."
        else:
            code = "cuda_host_toolchain_invalid"
            action = f"Re-acquire or verify {profile_name!r} with the Toolchain Store."
        raise _toolchain_error(
            code,
            f"{context} requires project-managed profile {profile_name!r}, but its "
            f"installation is {status}: {reason}. {action}",
        )

    description = store.describe(registry, profile_name)
    if description is None:
        raise _toolchain_error(
            "cuda_host_provenance_missing",
            f"{context} selected {profile_name!r}, but its compatible installation "
            "could not be described.",
        )
    ccbin_value = store.profile_paths(registry, profile_name).get("CC")
    if not ccbin_value:
        raise _toolchain_error(
            "cuda_host_compiler_not_declared",
            f"{context} selected {profile_name!r}, which has no materialized CC executable.",
        )

    prefix_value = description.get("prefix")
    try:
        prefix = Path(str(prefix_value)).resolve(strict=True)
        ccbin = Path(ccbin_value).resolve(strict=True)
        ccbin.relative_to(prefix)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _toolchain_error(
            "cuda_host_compiler_invalid",
            f"{context} selected {profile_name!r}, but its project-managed CC path "
            f"{ccbin_value!r} is missing or outside its installed prefix: {exc}.",
        ) from exc
    if not ccbin.is_file() or not os.access(ccbin, os.X_OK):
        raise _toolchain_error(
            "cuda_host_compiler_invalid",
            f"{context} selected {profile_name!r}, but {ccbin} is not an executable file.",
        )

    source = (
        RESOLUTION_CONFIG
        if external.resolution_source == RESOLUTION_CONFIG
        else str(external.diagnostic_metadata["environment_variable"])
    )
    diagnostics = [
        f"CUDA root {external.root} resolved from {source}.",
        f"NVCC {nvcc_path} reports CUDA {version}.",
        (
            f"NVCC -ccbin is project-managed {profile_name} "
            f"{description.get('version', '')}: {ccbin}."
        ),
    ]
    env_values = os.environ if environment is None else environment
    if env_values.get("CUDA_NVCC_CCBIN"):
        diagnostics.append(
            "CUDA_NVCC_CCBIN is ignored for linux-cross-mingw; the Toolchain Store "
            "compiler is authoritative."
        )

    return ResolvedCudaBuildHostInputs(
        cuda_root=external.root,
        nvcc_path=nvcc_path,
        detected_cuda_version=version,
        ccbin_path=ccbin,
        ccbin_profile=profile_name,
        ccbin_profile_version=str(description.get("version", "")),
        ccbin_provenance=dict(description),
        resolution_source=source,
        diagnostics=tuple(diagnostics),
        nvcc_version=str(external.effective_values["nvcc_version"]),
    )


def _target_data_error(code: str, message: str) -> BuildError:
    return BuildError("Windows CUDA target", message, category=CATEGORY_TARGET_DATA, code=code)


def _toolchain_probe_error(code: str, message: str) -> BuildError:
    return BuildError("CUDA host compiler", message, category=CATEGORY_TOOLCHAIN_INSTALL, code=code)


def _external_probe_error(code: str, message: str) -> BuildError:
    return BuildError("CUDA SDK", message, category=CATEGORY_EXTERNAL_PREREQUISITE, code=code)


def _compatibility_error(code: str, message: str) -> BuildError:
    return BuildError(
        "CUDA PTX compatibility",
        message,
        category=CATEGORY_CUDA_COMPATIBILITY,
        code=code,
    )


def _target_compute_architecture(target: WindowsTargetState) -> tuple[str, tuple[str, ...]]:
    """Choose the lowest valid capability so emitted PTX targets every reported NVIDIA GPU."""
    nvidia = target.nvidia
    if nvidia.state != FeatureState.PRESENT or not nvidia.devices:
        detail = nvidia.error or f"NVIDIA target state is {nvidia.state.value}"
        code = (
            "cuda_target_gpu_missing"
            if nvidia.state == FeatureState.ABSENT
            else "cuda_target_gpu_unavailable"
        )
        raise _target_data_error(
            code,
            f"CUDA is required, but the G1 Windows target probe did not provide an "
            f"available NVIDIA device: {detail}. Check Windows GPU drivers or provide "
            "a complete windows.target_override.",
        )

    capabilities: list[tuple[int, str, str]] = []
    for device in nvidia.devices:
        raw = device.compute_capability
        match = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", raw or "")
        if match is None or len(match.group(2)) != 1:
            raise _target_data_error(
                "cuda_target_compute_capability_invalid",
                f"G1 reported invalid NVIDIA compute capability {raw!r} for "
                f"{device.name!r}; provide valid target data in windows.target_override "
                "or repair the Windows probe.",
            )
        major, minor = int(match.group(1)), int(match.group(2))
        if major < 1:
            raise _target_data_error(
                "cuda_target_compute_capability_invalid",
                f"G1 reported invalid NVIDIA compute capability {raw!r} for {device.name!r}.",
            )
        architecture = f"{major}{minor}"
        capabilities.append((major * 10 + minor, architecture, device.name))

    selected = min(capabilities, key=lambda value: value[0])
    architecture = f"compute_{selected[1]}"
    devices = ", ".join(f"{name}={value}" for _numeric, value, name in sorted(capabilities))
    diagnostic = (
        f"G1 target NVIDIA capabilities: {devices}; selected {architecture} "
        "to support every reported NVIDIA device."
    )
    return architecture, (diagnostic,)


def _diagnostic_text(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return output or f"process exited with status {result.returncode} and no output"


def _is_known_gnu_source_conflict(output: str, cuda_version: str) -> bool:
    """Recognize the documented CUDA 12.4/glibc host-header declaration conflict only."""
    if cuda_version != "12.4" or re.search(r"\bfatal\s*:", output, re.IGNORECASE):
        return False
    conflict = re.compile(
        r"/usr/include/(?:[^/\s]+/)*bits/mathcalls\.h(?:\(\d+\)|:\d+(?::\d+)?)"
        r":\s*error:\s*exception specification is incompatible with that of previous function "
        r"""["'](?:cospi|sinpi|rsqrt)f?["']"""
        r"\s+\(declared at line \d+ of [^\r\n]*/include/crt/math_functions\.h\)",
        re.IGNORECASE,
    )
    errors = [line for line in output.splitlines() if re.search(r"\berror\b", line, re.I)]
    return bool(errors) and all(conflict.search(line) for line in errors)


def validate_cuda_ptx_compatibility(
    inputs: ResolvedCudaBuildHostInputs,
    windows_target: WindowsTargetState,
    *,
    timeout_seconds: int = 60,
) -> ResolvedCudaBuildHostInputs:
    """Compile and validate a tiny PTX kernel using the resolved CUDA/GCC inputs."""
    architecture, target_diagnostics = _target_compute_architecture(windows_target)
    root = inputs.cuda_root
    nvcc = inputs.nvcc_path
    ccbin = inputs.ccbin_path

    try:
        resolved_root = root.resolve(strict=True)
        resolved_nvcc = nvcc.resolve(strict=True)
        resolved_nvcc.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _external_probe_error(
            "cuda_nvcc_unavailable",
            f"Selected NVCC {nvcc} is no longer available under CUDA root {root}: {exc}.",
        ) from exc
    if not resolved_nvcc.is_file() or not os.access(resolved_nvcc, os.X_OK):
        raise _external_probe_error(
            "cuda_nvcc_unavailable",
            f"Selected NVCC {resolved_nvcc} is no longer an executable file under {root}.",
        )

    try:
        resolved_ccbin = ccbin.resolve(strict=True)
        prefix = Path(str(inputs.ccbin_provenance["prefix"])).resolve(strict=True)
        resolved_ccbin.relative_to(prefix)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise _toolchain_probe_error(
            "cuda_ccbin_unavailable",
            f"Selected project-managed ccbin {ccbin} is no longer available in its "
            f"recorded ToolchainStore prefix: {exc}.",
        ) from exc
    if not resolved_ccbin.is_file() or not os.access(resolved_ccbin, os.X_OK):
        raise _toolchain_probe_error(
            "cuda_ccbin_unavailable",
            f"Selected project-managed ccbin {resolved_ccbin} is no longer executable.",
        )

    try:
        compiler_result = subprocess.run(
            [str(resolved_ccbin), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _toolchain_probe_error(
            "cuda_ccbin_unavailable",
            f"Could not run selected project-managed ccbin {resolved_ccbin} --version: {exc}.",
        ) from exc
    compiler_output = _diagnostic_text(compiler_result)
    if compiler_result.returncode != 0:
        raise _toolchain_probe_error(
            "cuda_ccbin_unavailable",
            f"Selected project-managed ccbin {resolved_ccbin} failed --version: "
            f"{compiler_output}",
        )
    compiler_version_lines = (compiler_result.stdout or compiler_result.stderr or "").splitlines()
    if not compiler_version_lines:
        raise _toolchain_probe_error(
            "cuda_ccbin_unavailable",
            f"Selected project-managed ccbin {resolved_ccbin} returned no version "
            "information from --version.",
        )
    compiler_version = compiler_version_lines[0]

    try:
        nvcc_version_result = subprocess.run(
            [str(resolved_nvcc), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _external_probe_error(
            "cuda_nvcc_execution_failed",
            f"Could not run selected NVCC {resolved_nvcc} --version: {exc}.",
        ) from exc
    nvcc_output = _diagnostic_text(nvcc_version_result)
    version_match = re.search(r"release\s+(\d+\.\d+)", nvcc_output)
    version_detail = re.search(r"\bV(\d+\.\d+(?:\.\d+)*)\b", nvcc_output)
    if nvcc_version_result.returncode != 0 or version_match is None:
        raise _external_probe_error(
            "cuda_nvcc_execution_failed",
            f"Selected NVCC {resolved_nvcc} could not report its toolkit version: "
            f"{nvcc_output}",
        )
    detected_version = version_match.group(1)
    nvcc_version = version_detail.group(1) if version_detail else detected_version
    if detected_version != inputs.detected_cuda_version:
        raise _external_probe_error(
            "cuda_nvcc_changed",
            f"Selected NVCC {resolved_nvcc} now reports CUDA {detected_version}, but "
            f"resolution recorded {inputs.detected_cuda_version}; resolve prerequisites again.",
        )
    probe_context = (
        f"NVCC {resolved_nvcc} ({nvcc_version}); ccbin {resolved_ccbin} "
        f"({compiler_version}); target {architecture}"
    )

    base_flags = (
        "--ptx",
        f"--gpu-architecture={architecture}",
        f"-ccbin={resolved_ccbin}",
    )
    workaround_flag = "-Xcompiler=-U_GNU_SOURCE"
    source = (
        'extern "C" __global__ void ffmpeg_builder_cuda_probe(unsigned int* value) {\n'
        "  const unsigned int index = blockIdx.x * blockDim.x + threadIdx.x;\n"
        "  if (index == 0) *value += 1;\n"
        "}\n"
    )

    try:
        temporary_directory = tempfile.TemporaryDirectory(prefix="ffmpeg-builder-cuda-ptx-")
    except OSError as exc:
        raise _compatibility_error(
            "cuda_ptx_tempdir_unavailable",
            f"Could not create an isolated temporary directory for {probe_context}: {exc}.",
        ) from exc

    with temporary_directory as directory:
        directory_path = Path(directory)
        source_path = directory_path / "probe.cu"
        ptx_path = directory_path / "probe.ptx"
        try:
            source_path.write_text(source, encoding="utf-8")
        except OSError as exc:
            raise _compatibility_error(
                "cuda_ptx_source_unavailable",
                f"Could not write the isolated CUDA probe source for {probe_context}: {exc}.",
            ) from exc

        def _compile(flags: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    str(resolved_nvcc),
                    *flags,
                    "-o",
                    str(ptx_path),
                    str(source_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=directory_path,
            )

        try:
            result = _compile(base_flags)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _external_probe_error(
                "cuda_nvcc_execution_failed",
                f"Could not execute {probe_context}; flags {base_flags}; "
                f"workaround not used: {exc}.",
            ) from exc
        effective_flags: tuple[str, ...] = base_flags
        workaround_used: Optional[str] = None
        retry_note: Optional[str] = None
        if result.returncode != 0:
            initial_output = _diagnostic_text(result)
            if not _is_known_gnu_source_conflict(initial_output, detected_version):
                raise _compatibility_error(
                    "cuda_ptx_probe_failed",
                    f"{probe_context}; effective flags {base_flags}; workaround not used. "
                    f"PTX compatibility probe failed:\n"
                    f"{initial_output}",
                )
            retry_flags = (*base_flags, workaround_flag)
            ptx_path.unlink(missing_ok=True)
            try:
                retry_result = _compile(retry_flags)
            except (OSError, subprocess.SubprocessError) as exc:
                raise _compatibility_error(
                    "cuda_ptx_probe_failed",
                    f"{probe_context}; retry flags {retry_flags}. Known "
                    f"CUDA 12.4/glibc declaration conflict "
                    f"matched; workaround execution failed: {exc}. Original diagnostic:\n"
                    f"{initial_output}",
                ) from exc
            if retry_result.returncode != 0:
                raise _compatibility_error(
                    "cuda_ptx_probe_failed",
                    f"{probe_context}; retry flags {retry_flags}; "
                    f"workaround {workaround_flag} active. Scoped retry failed.\n"
                    f"Original diagnostic:\n{initial_output}\nRetry diagnostic:\n"
                    f"{_diagnostic_text(retry_result)}",
                )
            result = retry_result
            effective_flags = retry_flags
            workaround_used = workaround_flag
            retry_note = (
                "The known CUDA 12.4/glibc declaration conflict matched; the scoped "
                "-U_GNU_SOURCE retry succeeded."
            )

        if not ptx_path.is_file():
            raise _compatibility_error(
                "cuda_ptx_invalid",
                f"{probe_context}; effective flags {effective_flags}; workaround "
                f"{workaround_used or 'not used'}; NVCC returned success but produced no PTX file.",
            )
        try:
            ptx = ptx_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise _compatibility_error(
                "cuda_ptx_invalid",
                f"Could not read NVCC output {ptx_path} for {probe_context}; flags "
                f"{effective_flags}: {exc}.",
            ) from exc
        if not ptx.strip():
            raise _compatibility_error(
                "cuda_ptx_invalid",
                f"{probe_context}; effective flags {effective_flags}; NVCC produced an "
                "empty PTX file.",
            )
        target_match = re.search(
            rf"(?m)^\s*\.target\s+sm_{re.escape(architecture.removeprefix('compute_'))}"
            r"(?:\s|,|$)",
            ptx,
        )
        if (
            re.search(r"(?m)^\s*\.version\s+\d+\.\d+", ptx) is None
            or re.search(r"(?m)^\s*(?:\.visible\s+)?\.entry\s+", ptx) is None
            or target_match is None
        ):
            raise _compatibility_error(
                "cuda_ptx_invalid",
                f"{probe_context}; effective flags {effective_flags}; NVCC produced "
                "malformed or mismatched PTX; expected .version, .entry and matching "
                ".target directives.",
            )

    probe_diagnostics = (
        *target_diagnostics,
        f"NVCC {resolved_nvcc} ({nvcc_version}); ccbin {resolved_ccbin} ({compiler_version}).",
        f"PTX probe passed for {architecture}; effective flags: {' '.join(effective_flags)}.",
    )
    if retry_note:
        probe_diagnostics += (retry_note,)
    return replace(
        inputs,
        nvcc_path=resolved_nvcc,
        ccbin_path=resolved_ccbin,
        nvcc_version=nvcc_version,
        ccbin_version=compiler_version,
        compute_architecture=architecture,
        probe_status="passed",
        effective_flags=effective_flags,
        compatibility_workaround=workaround_used,
        probe_diagnostics=probe_diagnostics,
    )
