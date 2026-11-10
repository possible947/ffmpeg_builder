"""Resolution and host-side validation for an external Vulkan SDK."""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .build_types import CATEGORY_EXTERNAL_PREREQUISITE, BuildError
from .config import BuildConfig
from .external_prerequisites import (
    ExternalPrerequisiteValidation,
    ResolvedExternalPrerequisite,
    resolve_external_prerequisite,
)

VULKAN_SDK_ENVIRONMENT_VARIABLE = "VULKAN_SDK"

CAPABILITY_SHADER_COMPILATION = "shader_compilation"
CAPABILITY_SPIRV_VALIDATION = "spirv_validation"
CAPABILITY_SPIRV_OPTIMIZATION = "spirv_optimization"
SUPPORTED_CAPABILITIES = frozenset(
    {
        CAPABILITY_SHADER_COMPILATION,
        CAPABILITY_SPIRV_VALIDATION,
        CAPABILITY_SPIRV_OPTIMIZATION,
    }
)

_SHADER_SOURCE = """#version 450
layout(local_size_x = 1) in;
void main() {}
"""
_MAX_DIAGNOSTIC_LENGTH = 4000


@dataclass(frozen=True)
class ResolvedVulkanSdk:
    """Selected external SDK root, required host tools, and verified capabilities."""

    selected_root: Path
    effective_sdk_root: Path
    resolution_source: str
    shader_compiler_path: Optional[Path]
    spirv_validator_path: Optional[Path]
    spirv_optimizer_path: Optional[Path]
    validated_capabilities: frozenset[str]
    tool_diagnostics: Mapping[str, str]
    diagnostics: tuple[str, ...]
    diagnostic_metadata: Mapping[str, Any]

    @property
    def configured_root(self) -> Optional[Path]:
        """Return the configured SDK root when config, rather than the environment, won."""
        if self.resolution_source == "config":
            return self.selected_root
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return a serialization-friendly diagnostic representation."""
        return {
            "selected_root": str(self.selected_root),
            "configured_root": str(self.configured_root) if self.configured_root else None,
            "effective_sdk_root": str(self.effective_sdk_root),
            "resolution_source": self.resolution_source,
            "shader_compiler_path": (
                str(self.shader_compiler_path) if self.shader_compiler_path else None
            ),
            "spirv_validator_path": (
                str(self.spirv_validator_path) if self.spirv_validator_path else None
            ),
            "spirv_optimizer_path": (
                str(self.spirv_optimizer_path) if self.spirv_optimizer_path else None
            ),
            "validated_capabilities": sorted(self.validated_capabilities),
            "tool_diagnostics": dict(self.tool_diagnostics),
            "diagnostics": list(self.diagnostics),
            "diagnostic_metadata": dict(self.diagnostic_metadata),
        }


def _external_error(code: str, message: str, cause: Optional[BaseException] = None) -> BuildError:
    error = BuildError(
        "Vulkan SDK",
        message,
        category=CATEGORY_EXTERNAL_PREREQUISITE,
        code=code,
    )
    if cause is not None:
        error.__cause__ = cause
    return error


def _run(
    executable: Path,
    arguments: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = 15,
    code: str,
    operation: str,
    root: Path,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _external_error(
            code,
            f"Selected SDK tool {executable} could not {operation} for SDK root {root}: {exc}",
            exc,
        ) from exc
    return result


def _output(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if not output:
        return f"process exited with status {result.returncode} and no output"
    if len(output) > _MAX_DIAGNOSTIC_LENGTH:
        return output[:_MAX_DIAGNOSTIC_LENGTH] + "\n[diagnostic truncated]"
    return output


def _select_effective_root(root: Path, architecture: str) -> Path:
    architecture_root = root / architecture
    candidates = [
        candidate for candidate in (root, architecture_root) if (candidate / "bin").is_dir()
    ]
    if len(candidates) > 1:
        raise _external_error(
            "vulkan_sdk_layout_invalid",
            f"Selected Vulkan SDK root {root} has ambiguous effective layouts: "
            f"{', '.join(str(path) for path in candidates)}. Select one SDK root or "
            f"its {architecture} architecture root explicitly.",
        )
    if not candidates:
        raise _external_error(
            "vulkan_sdk_layout_invalid",
            f"Selected Vulkan SDK root {root} has no bin/ directory or "
            f"{architecture}/bin/ architecture layout.",
        )
    effective_root = candidates[0].resolve(strict=True)
    try:
        effective_root.relative_to(root)
    except ValueError as exc:
        raise _external_error(
            "vulkan_sdk_layout_invalid",
            f"Vulkan SDK architecture root {effective_root} resolves outside selected "
            f"SDK root {root}.",
            exc,
        ) from exc
    header = effective_root / "include" / "vulkan" / "vulkan.h"
    if not header.is_file():
        raise _external_error(
            "vulkan_sdk_layout_invalid",
            f"Selected Vulkan SDK root {root} normalises to {effective_root}, but the "
            f"expected SDK layout marker {header} is missing.",
        )
    return effective_root


def _select_tool(
    effective_root: Path,
    tool_names: Sequence[str],
    capability: str,
    *,
    optional: bool = False,
) -> Optional[Path]:
    bin_dir = effective_root / "bin"
    for tool_name in tool_names:
        candidate = bin_dir / tool_name
        if not candidate.exists():
            continue
        try:
            tool = candidate.resolve(strict=True)
            tool.relative_to(effective_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _external_error(
                "vulkan_tool_invalid",
                f"Vulkan {capability} tool {candidate} is missing, broken, or outside "
                f"selected SDK root {effective_root}: {exc}",
                exc,
            ) from exc
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise _external_error(
                "vulkan_tool_invalid",
                f"Vulkan {capability} tool {tool} under {effective_root} is not an executable file.",
            )
        return tool
    if optional:
        return None
    code = {
        CAPABILITY_SHADER_COMPILATION: "vulkan_shader_compiler_missing",
        CAPABILITY_SPIRV_VALIDATION: "vulkan_spirv_validator_missing",
        CAPABILITY_SPIRV_OPTIMIZATION: "vulkan_spirv_optimizer_missing",
    }[capability]
    expected = " or ".join(tool_names)
    raise _external_error(
        code,
        f"Selected Vulkan SDK root {effective_root} does not provide the required "
        f"{capability} tool ({expected}) in its bin/ directory.",
    )


def _validate_vulkan_root(
    root: Path,
    capabilities: frozenset[str],
    architecture: str,
) -> ExternalPrerequisiteValidation:
    effective_root = _select_effective_root(root, architecture)
    tools: dict[str, Path] = {}
    tool_diagnostics: dict[str, str] = {}

    if CAPABILITY_SHADER_COMPILATION in capabilities:
        shader_compiler = _select_tool(
            effective_root,
            ("glslc", "glslangValidator"),
            CAPABILITY_SHADER_COMPILATION,
        )
        assert shader_compiler is not None
        tools["shader_compiler"] = shader_compiler
    else:
        shader_compiler = None

    if CAPABILITY_SPIRV_VALIDATION in capabilities:
        spirv_validator = _select_tool(effective_root, ("spirv-val",), CAPABILITY_SPIRV_VALIDATION)
        assert spirv_validator is not None
        tools["spirv_validator"] = spirv_validator
    else:
        spirv_validator = None

    if CAPABILITY_SPIRV_OPTIMIZATION in capabilities:
        spirv_optimizer = _select_tool(
            effective_root, ("spirv-opt",), CAPABILITY_SPIRV_OPTIMIZATION
        )
        assert spirv_optimizer is not None
        tools["spirv_optimizer"] = spirv_optimizer
    else:
        spirv_optimizer = None

    for tool_key, tool_path in tools.items():
        version_arguments = ("-v",) if tool_path.name == "glslangValidator" else ("--version",)
        result = _run(
            tool_path,
            version_arguments,
            code="vulkan_tool_execution_failed",
            operation="report its version",
            root=effective_root,
        )
        if result.returncode != 0 or not (result.stdout or result.stderr).strip():
            raise _external_error(
                "vulkan_tool_execution_failed",
                f"Selected Vulkan SDK tool {tool_path} failed its version probe "
                f"(status {result.returncode}) for {tool_key}: {_output(result)}",
            )
        tool_diagnostics[tool_key] = _output(result)

    validated: set[str] = set()
    diagnostics: list[str] = []
    if shader_compiler is not None:
        try:
            temporary_directory = tempfile.TemporaryDirectory(prefix="ffmpeg-builder-vulkan-")
        except OSError as exc:
            raise _external_error(
                "vulkan_probe_unavailable",
                f"Could not create an isolated directory for Vulkan SDK host-tool "
                f"validation at {effective_root}: {exc}",
                exc,
            ) from exc

        with temporary_directory as directory:
            work_dir = Path(directory)
            source = work_dir / "probe.comp"
            spirv = work_dir / "probe.spv"
            source.write_text(_SHADER_SOURCE, encoding="utf-8")
            compile_args: tuple[str, ...]
            if shader_compiler.name == "glslc":
                compile_args = ("-fshader-stage=compute", "-o", str(spirv), str(source))
            else:
                compile_args = ("-V", "-S", "comp", "-o", str(spirv), str(source))
            compile_result = _run(
                shader_compiler,
                compile_args,
                cwd=work_dir,
                timeout=30,
                code="vulkan_shader_compile_failed",
                operation="compile the Vulkan SDK shader probe",
                root=effective_root,
            )
            if compile_result.returncode != 0:
                raise _external_error(
                    "vulkan_shader_compile_failed",
                    f"Vulkan shader compilation failed using {shader_compiler} from "
                    f"{effective_root} (status {compile_result.returncode}): "
                    f"{_output(compile_result)}",
                )
            if not spirv.is_file() or spirv.stat().st_size == 0:
                raise _external_error(
                    "vulkan_shader_output_invalid",
                    f"Vulkan shader compiler {shader_compiler} reported success but "
                    f"did not produce non-empty SPIR-V at {spirv}.",
                )
            with spirv.open("rb") as spirv_file:
                if spirv_file.read(4) != b"\x03\x02#\x07":
                    raise _external_error(
                        "vulkan_shader_output_invalid",
                        f"Vulkan shader compiler {shader_compiler} produced output "
                        f"without the SPIR-V magic number at {spirv}.",
                    )
            validated.add(CAPABILITY_SHADER_COMPILATION)
            diagnostics.append(f"Shader probe compiled to {spirv.stat().st_size} bytes of SPIR-V.")

            if spirv_validator is not None:
                validate_result = _run(
                    spirv_validator,
                    ("--target-env", "vulkan1.0", str(spirv)),
                    cwd=work_dir,
                    timeout=30,
                    code="vulkan_spirv_validation_failed",
                    operation="validate generated SPIR-V",
                    root=effective_root,
                )
                if validate_result.returncode != 0:
                    raise _external_error(
                        "vulkan_spirv_validation_failed",
                        f"SPIR-V validation failed using {spirv_validator} from "
                        f"{effective_root} (status {validate_result.returncode}): "
                        f"{_output(validate_result)}",
                    )
                validated.add(CAPABILITY_SPIRV_VALIDATION)

            if spirv_optimizer is not None:
                assert spirv_validator is not None
                optimized = work_dir / "probe-optimized.spv"
                optimize_result = _run(
                    spirv_optimizer,
                    ("--strip-debug", "-o", str(optimized), str(spirv)),
                    cwd=work_dir,
                    timeout=30,
                    code="vulkan_spirv_optimization_failed",
                    operation="optimise generated SPIR-V",
                    root=effective_root,
                )
                if optimize_result.returncode != 0:
                    raise _external_error(
                        "vulkan_spirv_optimization_failed",
                        f"SPIR-V optimisation failed using {spirv_optimizer} from "
                        f"{effective_root} (status {optimize_result.returncode}): "
                        f"{_output(optimize_result)}",
                    )
                if not optimized.is_file() or optimized.stat().st_size == 0:
                    raise _external_error(
                        "vulkan_spirv_optimization_failed",
                        f"SPIR-V optimiser {spirv_optimizer} reported success but did "
                        f"not produce non-empty output at {optimized}.",
                    )
                optimized_validation = _run(
                    spirv_validator,
                    ("--target-env", "vulkan1.0", str(optimized)),
                    cwd=work_dir,
                    timeout=30,
                    code="vulkan_spirv_validation_failed",
                    operation="validate optimised SPIR-V",
                    root=effective_root,
                )
                if optimized_validation.returncode != 0:
                    raise _external_error(
                        "vulkan_spirv_validation_failed",
                        f"Optimised SPIR-V validation failed using {spirv_validator} "
                        f"(status {optimized_validation.returncode}): "
                        f"{_output(optimized_validation)}",
                    )
                validated.add(CAPABILITY_SPIRV_OPTIMIZATION)
                diagnostics.append(
                    f"Optimised and revalidated SPIR-V ({optimized.stat().st_size} bytes)."
                )

    diagnostics.append("No Vulkan GPU/runtime operation was performed.")
    effective_values: dict[str, Any] = {
        "effective_sdk_root": str(effective_root),
        "validated_capabilities": sorted(validated),
        "tool_diagnostics": dict(tool_diagnostics),
    }
    return ExternalPrerequisiteValidation(
        host_tools=tools,
        diagnostics=tuple(diagnostics),
        diagnostic_metadata={
            "effective_sdk_root": str(effective_root),
            "tool_diagnostics": dict(tool_diagnostics),
        },
        effective_values=effective_values,
    )


def resolve_wsl_mingw_vulkan_sdk(
    config: BuildConfig,
    *,
    required: bool = True,
    required_capabilities: Sequence[str] = (
        CAPABILITY_SHADER_COMPILATION,
        CAPABILITY_SPIRV_VALIDATION,
    ),
    architecture: str = "x86_64",
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[ResolvedVulkanSdk]:
    """Resolve Vulkan host tools from the selected external SDK without setup-env.sh."""
    capabilities = frozenset(required_capabilities)
    unknown_capabilities = capabilities - SUPPORTED_CAPABILITIES
    if unknown_capabilities:
        raise ValueError(
            "Unsupported Vulkan SDK capabilities: " + ", ".join(sorted(unknown_capabilities))
        )
    if not architecture or "/" in architecture or architecture in (".", ".."):
        raise ValueError("Vulkan SDK architecture must be a single directory name")
    if CAPABILITY_SPIRV_OPTIMIZATION in capabilities and not {
        CAPABILITY_SHADER_COMPILATION,
        CAPABILITY_SPIRV_VALIDATION,
    }.issubset(capabilities):
        raise ValueError(
            "SPIR-V optimization validation requires shader_compilation and "
            "spirv_validation capabilities"
        )

    try:
        external: Optional[ResolvedExternalPrerequisite] = resolve_external_prerequisite(
            "vulkan",
            config.external_prerequisite_paths.get("vulkan"),
            environment_variable=VULKAN_SDK_ENVIRONMENT_VARIABLE,
            required=required,
            environment=environment,
            validator=lambda root: _validate_vulkan_root(root, capabilities, architecture),
        )
    except BuildError as exc:
        if exc.category != CATEGORY_EXTERNAL_PREREQUISITE:
            raise
        codes = {
            "prerequisite_missing": "vulkan_sdk_missing",
            "prerequisite_invalid_path": "vulkan_sdk_invalid_path",
            "prerequisite_validation_failed": "vulkan_sdk_validation_failed",
        }
        code = codes.get(exc.code or "", exc.code or "vulkan_sdk_validation_failed")
        message = str(exc)
        if message.startswith("Vulkan SDK: "):
            message = message[len("Vulkan SDK: ") :]
        raise _external_error(code, message, exc) from exc
    if external is None:
        return None

    effective_root = Path(str(external.effective_values["effective_sdk_root"]))
    validated_capabilities = frozenset(external.effective_values["validated_capabilities"])
    resolution_source = str(
        external.diagnostic_metadata.get("environment_variable") or external.resolution_source
    )
    return ResolvedVulkanSdk(
        selected_root=external.root,
        effective_sdk_root=effective_root,
        resolution_source=resolution_source,
        shader_compiler_path=external.host_tools.get("shader_compiler"),
        spirv_validator_path=external.host_tools.get("spirv_validator"),
        spirv_optimizer_path=external.host_tools.get("spirv_optimizer"),
        validated_capabilities=validated_capabilities,
        tool_diagnostics=dict(external.effective_values["tool_diagnostics"]),
        diagnostics=external.diagnostics,
        diagnostic_metadata=dict(external.diagnostic_metadata),
    )
