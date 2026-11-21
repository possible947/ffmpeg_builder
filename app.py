"""Main application class for FFmpeg Builder."""

import os
import platform as _platform
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from rich.console import Console
from rich.live import Live

from .build_types import (
    CATEGORY_EXTERNAL_PREREQUISITE,
    CATEGORY_TARGET_DATA,
    CATEGORY_TOOLCHAIN_INSTALL,
    BuildError,
    SkipComponent,
)
from .builder import FFmpegBuilder
from .components import Component, ComponentRegistry, ComponentSelection
from .cross_environment import (
    CrossEnvironment,
    run_cross_environment_acceptance,
)
from .build_identity import (
    MaterialBuildInputs,
    build_native_identity,
    build_windows_identity,
    compare_identities,
    runtime_identity,
)
from .config import TARGET_BACKEND_CROSS_MINGW, BuildConfig, ConfigManager
from .cuda_prerequisites import (
    ResolvedCudaBuildHostInputs,
    cuda_nv_codec_identity,
    cuda_profile_identity,
    invalidate_cuda_resume,
    resolve_wsl_mingw_cuda_inputs,
    validate_cuda_output_modes,
    validate_cuda_ptx_compatibility,
    validate_required_cuda_components,
)
from .ffmpeg_policy import (
    FfmpegPolicy,
    detected_system_tool_versions,
    evaluate_ffmpeg_policy,
)
from .platform_detect import PlatformDetector
from .platforms.context import PlatformContext
from .platforms.linux_cross_mingw import LinuxCrossMingwPlatform
from .platforms.resolver import PlatformStrategyResolver
from .state import ComponentStatus, StateManager
from .target_guard import (
    IdentityError,
    TargetSession,
    baseline_from_state,
    invalidate_acceptance,
    mark_state_verified,
    prepare_native_target,
    prepare_windows_target,
    recheck_runtime_identity,
)
from .system_report import SystemReportGenerator
from .toolchain_registry import ToolchainRegistry
from .toolchain_store import ToolchainStore, resolve_store_root
from .windows_probe import FeatureState, WindowsTargetState, probe_windows_target
from .vulkan_prerequisites import (
    CAPABILITY_SHADER_COMPILATION,
    CAPABILITY_SPIRV_VALIDATION,
    ResolvedVulkanSdk,
    resolve_wsl_mingw_vulkan_sdk,
)
from .wsl_mingw_preparation import (
    WslMingwPreparation,
    project_material_build_inputs,
    resolve_wsl_mingw_project_toolchains,
)
from .target_inputs import materialize_target_inputs, validate_target_inputs_manifest
from .ui.dashboard import BuildDashboard
from .ui.error_handler import ErrorHandler
from .ui.screens import (
    ConfigScreen,
    FinalReportScreen,
    HelpScreen,
    InfoScreen,
    SystemReportScreen,
    ToolchainPlanScreen,
    ToolchainStoreScreen,
)

# The package is the repository root (flat layout); anchor the default
# workspace/config paths to it so the app works from any CWD.
PROJECT_ROOT = Path(__file__).resolve().parent


class FFmpegBuilderApp:
    """Main application class."""

    def __init__(self, workspace: Optional[Path] = None):
        """Initialize application.

        Args:
            workspace: Workspace directory. If None, uses versioned workspace (e.g. workspace_81).
        """
        self._custom_workspace = workspace
        self.console = Console()
        # Injectable for tests; the real probe talks to the physical Windows
        # host through WSL interop.
        self.windows_probe = probe_windows_target
        self.windows_target_state: Optional[WindowsTargetState] = None
        self.wsl_mingw_preparation: Optional[WslMingwPreparation] = None
        self.cross_environment: Optional[CrossEnvironment] = None

        self.config_manager = ConfigManager(PROJECT_ROOT / "build_config.yaml")
        config = self.config_manager.load()
        self._sync_workspace(config)

        self.platform_detector = PlatformDetector(config=config)
        self.system_info, self.platform_info, self.tools = self.platform_detector.detect_all()

        report_gen = SystemReportGenerator(
            self.system_info,
            self.platform_info,
            self.tools,
            config=config,
        )
        self.system_report = report_gen.generate()

        self.registry = ComponentRegistry()

        self.system_screen = SystemReportScreen(self.console)
        self.config_screen = ConfigScreen(self.console)
        self.info_screen = InfoScreen(self.console)
        self.final_screen = FinalReportScreen(self.console)
        self.toolchain_screen = ToolchainPlanScreen(self.console)
        self.store_screen = ToolchainStoreScreen(self.console)
        self.help_screen = HelpScreen(self.console)
        self.error_handler = ErrorHandler(self.console)

    def _sync_workspace(self, config: BuildConfig) -> None:
        """Synchronize workspace directory and state manager with the current configuration."""
        if self._custom_workspace is not None:
            self.workspace = self._custom_workspace
        else:
            self.workspace = config.get_workspace_path(PROJECT_ROOT)
        self.base_workspace = self.workspace
        self.packages = self.workspace / "packages"
        self.state_manager = StateManager(self.workspace / "build_state.json")

    @staticmethod
    def _is_cross_mingw(config: BuildConfig) -> bool:
        return config.target_backend == TARGET_BACKEND_CROSS_MINGW

    def _probe_windows_target(self, config: BuildConfig) -> WindowsTargetState:
        """Freshly probe the physical Windows target (or apply its explicit override)."""
        windows = config.windows
        self.windows_target_state = self.windows_probe(
            windows.target_override, timeout=windows.probe_timeout_seconds
        )
        return self.windows_target_state

    def current_windows_identity(
        self,
        config: BuildConfig,
        material_inputs: MaterialBuildInputs,
        *,
        target_state: Optional[WindowsTargetState] = None,
        components: Optional[List[Component]] = None,
    ) -> Any:
        """Create a full semantic identity from the current target and resolved inputs."""
        target_state = target_state or self._probe_windows_target(config)
        components = (
            components if components is not None else self._get_buildable_components(config)
        )
        try:
            return build_windows_identity(
                config.to_dict(),
                target_state,
                components={c.name: c.version for c in components},
                toolchains={c.name: c.toolchain for c in components},
                host={"platform": self.platform_detector.get_build_backend_name()},
                material_inputs=material_inputs,
            )
        except (TypeError, ValueError) as exc:
            raise BuildError(
                "WSL-Mingw identity",
                f"Resolved material inputs cannot form a trusted semantic identity: {exc}",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_identity_construction_failed",
            ) from exc

    def _materialize_target_input_manifest(
        self,
        config: BuildConfig,
        project_toolchains: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Generate and verify G2.6 identity metadata in a disposable prefix."""
        self.base_workspace.mkdir(parents=True, exist_ok=True)
        archives = Path(config.source_archives_dir)
        if not archives.is_absolute():
            archives = PROJECT_ROOT / archives
        llvm_prefix = Path(project_toolchains["llvm-mingw"]["prefix"])
        with tempfile.TemporaryDirectory(
            prefix=".target-input-identity-", dir=self.base_workspace
        ) as temporary:
            root = Path(temporary)
            manifest = materialize_target_inputs(
                registry=self.registry,
                target_backend=TARGET_BACKEND_CROSS_MINGW,
                source_archives_dir=archives,
                packages_dir=root / "packages",
                target_prefix=root / "target-prefix",
                toolchain_root=llvm_prefix,
                allow_network_downloads=config.allow_network_downloads,
            )
            return validate_target_inputs_manifest(manifest, root / "target-prefix")

    def prepare_target(self, config: BuildConfig, resume: bool) -> TargetSession:
        """Validate prerequisites and target identity before any state mutation.

        Switches ``workspace``/``packages``/``state_manager`` to the identity
        workspace on the WSL-Mingw route. Raises ``BuildError`` or
        ``IdentityError`` when prerequisites or reuse are unsafe, leaving saved
        state and artifacts untouched.
        """
        if config.cuda_build_mode == "required" and not (
            config.gpl_enabled or config.allow_nonfree_cuda
        ):
            raise BuildError(
                "CUDA",
                "FFmpeg CUDA/NVCC requires nonfree code; set allow_nonfree_cuda "
                "to accept an unredistributable build.",
            )
        if self._is_cross_mingw(config):
            self.windows_target_state = None
            self.wsl_mingw_preparation = None
            self.cross_environment = None
            platform_info = self.platform_detector.platform_info
            if not getattr(platform_info, "is_linux", False):
                raise BuildError(
                    "WSL-Mingw cross environment",
                    "The linux-cross-mingw target requires a Linux/WSL build host.",
                    category=CATEGORY_TARGET_DATA,
                    code="wsl_mingw_build_machine_unsupported",
                )
            target_state = self._probe_windows_target(config)
            components = self._get_buildable_components(config)
            try:
                registry = ToolchainRegistry()
            except (OSError, ValueError) as exc:
                raise BuildError(
                    "WSL-Mingw toolchain registry",
                    f"Toolchain registry could not be loaded: {exc}",
                    category=CATEGORY_TOOLCHAIN_INSTALL,
                    code="wsl_mingw_toolchain_registry_unavailable",
                ) from exc
            store = ToolchainStore(
                resolve_store_root(PROJECT_ROOT),
                workspace=self.base_workspace,
                source_archives_dir=PROJECT_ROOT / config.source_archives_dir,
                allow_network_downloads=config.allow_network_downloads,
                backend=TARGET_BACKEND_CROSS_MINGW,
            )
            project_toolchains = resolve_wsl_mingw_project_toolchains(
                config.ffmpeg_version, registry, store
            )
            cuda_inputs: Optional[ResolvedCudaBuildHostInputs] = None
            cuda_enabled = config.cuda_build_mode != "disabled" and (
                config.cuda_build_mode == "required"
                or (config.enable_libvmaf and config.enable_libvmaf_cuda)
            )
            if cuda_enabled:
                cuda_inputs = resolve_wsl_mingw_cuda_inputs(config, registry, store, required=True)
                if cuda_inputs is None:
                    raise BuildError(
                        "CUDA SDK",
                        "Required CUDA resolution returned no prerequisite.",
                        category=CATEGORY_EXTERNAL_PREREQUISITE,
                        code="cuda_sdk_missing",
                    )
                architecture = config.cuda_compute_capability or os.environ.get(
                    "CUDA_COMPUTE_CAPABILITY"
                )
                if architecture:
                    cuda_inputs = validate_cuda_ptx_compatibility(
                        cuda_inputs, target_state, architecture=architecture
                    )
                else:
                    cuda_inputs = validate_cuda_ptx_compatibility(cuda_inputs, target_state)

            vulkan: Optional[ResolvedVulkanSdk] = None
            if config.enable_libplacebo_vulkan:
                vulkan = resolve_wsl_mingw_vulkan_sdk(
                    config,
                    required=True,
                    required_capabilities=(
                        CAPABILITY_SHADER_COMPILATION,
                        CAPABILITY_SPIRV_VALIDATION,
                    ),
                )
                if vulkan is None:
                    raise BuildError(
                        "Vulkan SDK",
                        "Required Vulkan SDK resolution returned no prerequisite.",
                        category=CATEGORY_EXTERNAL_PREREQUISITE,
                        code="vulkan_sdk_missing",
                    )

            target_input_manifest = self._materialize_target_input_manifest(
                config, project_toolchains
            )
            material_inputs = project_material_build_inputs(
                config,
                components,
                project_toolchains,
                cuda_inputs,
                vulkan,
                target_input_manifest,
            )
            current = self.current_windows_identity(
                config,
                material_inputs,
                target_state=target_state,
                components=components,
            )
            session = prepare_windows_target(self.base_workspace, current, resume)
            self.workspace = session.workspace
            self.packages = self.workspace / "packages"
            self.state_manager = StateManager(self.workspace / "build_state.json")
            self.state_manager.require_verified_identity = True

            platform_info = self.platform_detector.platform_info
            platform_info.cuda_build_host_inputs = cuda_inputs
            platform_info.cuda_available = cuda_inputs is not None
            if cuda_inputs is not None:
                platform_info.cuda_path = str(cuda_inputs.nvcc_path)
                platform_info.cuda_version = cuda_inputs.detected_cuda_version
                platform_info.nvcc_ccbin = str(cuda_inputs.ccbin_path)
                platform_info.nvcc_ccbin_reason = (
                    f"Project-managed {cuda_inputs.ccbin_profile} "
                    f"{cuda_inputs.ccbin_profile_version}; CUDA_NVCC_CCBIN is ignored "
                    "for linux-cross-mingw"
                )
                platform_info.cuda_nvcc_supported = True
                platform_info.cuda_nvcc_reason = "Production PTX compatibility probe passed"
                platform_info.cuda_compute_capability = (
                    cuda_inputs.compute_architecture.removeprefix("compute_")
                    if cuda_inputs.compute_architecture
                    else None
                )
                platform_info.nvcc_ccbin_reason = (
                    "Production PTX compatibility probe passed with the selected "
                    "ToolchainStore compiler."
                )
                platform_info.libvmaf_cuda_supported = True
                platform_info.libvmaf_cuda_reason = "Production PTX compatibility probe passed"
                session.notes.append(
                    f"CUDA host SDK: {cuda_inputs.cuda_root} ({cuda_inputs.resolution_source}); "
                    f"project ccbin: {cuda_inputs.ccbin_path}; "
                    f"Windows-target architecture: {cuda_inputs.compute_architecture}; "
                    "PTX probe passed."
                )
            else:
                platform_info.cuda_path = None
                platform_info.cuda_version = None
                platform_info.nvcc_ccbin = None
                platform_info.nvcc_ccbin_reason = ""
                platform_info.cuda_compute_capability = None
                platform_info.cuda_nvcc_supported = False
                platform_info.cuda_nvcc_reason = "CUDA capability is disabled by configuration."
                platform_info.libvmaf_cuda_supported = False
                platform_info.libvmaf_cuda_reason = "CUDA capability is disabled by configuration."

            platform_info.vulkan_available = vulkan is not None
            platform_info.vulkan_dev_available = vulkan is not None
            platform_info.vulkan_runtime_available = False
            platform_info.vulkan_sdk_available = vulkan is not None
            platform_info.vulkan_sdk_path = (
                str(vulkan.effective_sdk_root) if vulkan is not None else None
            )
            platform_info.vulkan_reason = (
                "External host SDK tools validated; Windows runtime facts remain from G1."
                if vulkan is not None
                else "Vulkan capability is disabled by configuration."
            )
            platform_info.vulkan_detected_via = (
                vulkan.resolution_source if vulkan is not None else None
            )
            platform_info.vulkan_detected_header_paths = []
            platform_info.vulkan_detected_icd_files = []
            if vulkan is not None:
                session.notes.append(
                    f"Vulkan host SDK: {vulkan.effective_sdk_root} "
                    f"({vulkan.resolution_source}); validated host capabilities: "
                    f"{', '.join(sorted(vulkan.validated_capabilities))}. "
                    "Windows runtime/device facts remain separate."
                )
            identity_summary = {
                "schema_version": session.identity.schema_version,
                "digest": session.identity.build_fingerprint,
                "build_key": session.identity.build_key,
                "backend": session.identity.backend,
                "target": dict(session.identity.build.get("target", {})),
                "toolchains": {
                    name: {
                        "profile": value.get("profile", name),
                        "version": value.get("version", value.get("release")),
                        "target": value.get("target"),
                    }
                    for name, value in material_inputs.toolchains.items()
                    if isinstance(value, dict)
                },
                "cuda_architecture": (
                    material_inputs.effective_cuda.get("architecture")
                    if material_inputs.effective_cuda is not None
                    else None
                ),
                "target_input_materialization_id": target_input_manifest["materialization_id"],
                "configured_capabilities": dict(material_inputs.configured_capabilities),
            }
            session.notes.append(
                f"Semantic identity schema {session.identity.schema_version}, "
                f"key {session.identity.build_key}; target inputs "
                f"{target_input_manifest['materialization_id']}."
            )
            self.wsl_mingw_preparation = WslMingwPreparation.create(
                target_state,
                cuda_inputs,
                vulkan,
                project_toolchains,
                identity_summary=identity_summary,
            )
            self.system_report.wsl_mingw_preparation = self.wsl_mingw_preparation.to_dict()
            mark_state_verified(self.state_manager)
            return session
        identity = build_native_identity(
            config.to_dict(),
            self.platform_detector.get_build_backend_name(),
            sys.platform,
            _platform.machine(),
        )
        session = prepare_native_target(self.base_workspace, self.state_manager, identity, resume)
        self._prepare_native_cuda(config, session)
        return session

    def prepare_cross_environment(
        self, config: BuildConfig, session: TargetSession
    ) -> CrossEnvironment:
        """Materialize and validate G3 only after G1/G2 authorize the workspace."""
        if not self._is_cross_mingw(config):
            raise BuildError(
                "WSL-Mingw cross environment",
                "G3 environment preparation was requested for a native backend.",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_cross_environment_backend_mismatch",
            )
        if not session.uses_isolated_workspace or session.workspace != self.workspace:
            raise BuildError(
                "WSL-Mingw cross environment",
                "The target workspace has not been authorized by the G1/G2 identity guard.",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_cross_environment_workspace_unauthorized",
            )
        if self.wsl_mingw_preparation is None:
            raise BuildError(
                "WSL-Mingw cross environment",
                "G2 WSL-Mingw preparation is unavailable; target tools will not be rediscovered.",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_cross_environment_preparation_missing",
            )
        context = PlatformContext(
            config=config,
            workspace=self.workspace,
            packages=self.packages,
            platform_detector=self.platform_detector,
            platform_info=self.platform_detector.platform_info,
            num_jobs=self.platform_detector.get_num_jobs(config.num_jobs),
        )
        strategy = PlatformStrategyResolver().resolve(context)
        if not isinstance(strategy, LinuxCrossMingwPlatform):
            raise BuildError(
                "WSL-Mingw cross environment",
                "The linux-cross-mingw platform strategy is unavailable on this build host.",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_platform_strategy_missing",
            )
        archives = Path(config.source_archives_dir)
        if not archives.is_absolute():
            archives = PROJECT_ROOT / archives
        environment = strategy.prepare_cross_environment(
            self.wsl_mingw_preparation,
            config,
            self.workspace,
            archives,
            self.registry,
        )
        self.cross_environment = environment
        run_cross_environment_acceptance(environment)
        return environment

    def _prepare_native_cuda(self, config: BuildConfig, session: TargetSession) -> None:
        """Resolve and probe one NVCC profile before trusting saved consumers."""
        pi = self.platform_detector.platform_info
        pi.cuda_build_host_inputs = None
        if config.cuda_build_mode == "disabled":
            pi.cuda_nvcc_supported = False
            pi.libvmaf_cuda_supported = False
            pi.libvmaf_cuda_reason = "CUDA compilation disabled by configuration."
            session.notes.append(pi.libvmaf_cuda_reason)
            return
        if not pi.is_linux:
            if config.cuda_build_mode == "required":
                raise BuildError(
                    "CUDA", "Required CUDA compilation is not supported on this backend."
                )
            return
        configured = config.external_prerequisite_paths.get("cuda")
        detected = pi.cuda_path
        available = bool(
            configured or os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH") or detected
        )
        if not available:
            message = "CUDA Toolkit not found; configure external_prerequisite_paths.cuda."
            if config.cuda_build_mode == "required":
                raise BuildError("CUDA", message)
            pi.cuda_nvcc_supported = False
            pi.libvmaf_cuda_supported = False
            pi.libvmaf_cuda_reason = message
            session.notes.append(message)
            return
        try:
            selected_config = config
            if (
                not configured
                and not os.environ.get("CUDA_HOME")
                and not os.environ.get("CUDA_PATH")
            ):
                assert detected is not None
                root = Path(detected).resolve().parent.parent
                selected_config = replace(
                    config,
                    external_prerequisite_paths={
                        **config.external_prerequisite_paths,
                        "cuda": str(root),
                    },
                )
            try:
                registry = ToolchainRegistry()
            except (OSError, ValueError) as exc:
                raise BuildError(
                    "CUDA host compiler",
                    f"Toolchain registry could not be loaded: {exc}",
                    category=CATEGORY_TOOLCHAIN_INSTALL,
                    code="cuda_host_registry_unavailable",
                ) from exc
            store = ToolchainStore(
                resolve_store_root(PROJECT_ROOT),
                workspace=self.workspace,
                source_archives_dir=PROJECT_ROOT / config.source_archives_dir,
                allow_network_downloads=config.allow_network_downloads,
            )
            architecture = (
                config.cuda_compute_capability
                or os.environ.get("CUDA_COMPUTE_CAPABILITY")
                or pi.cuda_compute_capability
            )

            def resolve_and_probe(
                environment: Dict[str, str], source: str
            ) -> ResolvedCudaBuildHostInputs:
                selected = resolve_wsl_mingw_cuda_inputs(
                    selected_config,
                    registry,
                    store,
                    required=True,
                    environment=environment,
                    allow_host_override=True,
                    host_override_source=source,
                )
                assert selected is not None
                selected = validate_cuda_ptx_compatibility(
                    selected, None, architecture=architecture
                )
                return validate_cuda_output_modes(selected)

            environment = dict(os.environ)
            try:
                inputs = resolve_and_probe(environment, "CUDA_NVCC_CCBIN")
            except BuildError as first:
                system_cc = shutil.which("gcc")
                if (
                    environment.get("CUDA_NVCC_CCBIN")
                    or not system_cc
                    or first.code
                    not in {
                        "cuda_host_mapping_missing",
                        "cuda_host_toolchain_not_installed",
                        "cuda_ptx_probe_failed",
                        "cuda_output_probe_failed",
                    }
                ):
                    raise
                environment["CUDA_NVCC_CCBIN"] = system_cc
                try:
                    inputs = resolve_and_probe(environment, "system gcc")
                except BuildError as second:
                    raise BuildError(
                        "CUDA",
                        f"Managed compiler attempt failed:\n{first}\n"
                        f"System compiler attempt failed:\n{second}",
                        category=second.category,
                        code=second.code,
                    ) from second
                session.notes.append(
                    f"Managed CUDA compiler unavailable; using verified system gcc: {first}"
                )
            if any(
                character.isspace()
                for path in (inputs.nvcc_path, inputs.ccbin_path)
                for character in str(path)
            ):
                raise BuildError(
                    "CUDA",
                    "FFmpeg's configure cannot safely use NVCC or host compiler paths "
                    "containing whitespace; select paths without whitespace.",
                )
            if config.enable_libvmaf and config.enable_libvmaf_cuda:
                bin2c = inputs.cuda_root / "bin" / "bin2c"
                if not bin2c.is_file() or not os.access(bin2c, os.X_OK):
                    raise BuildError("CUDA", f"Selected CUDA Toolkit has no executable {bin2c}.")
        except BuildError as exc:
            pi.cuda_nvcc_supported = False
            pi.libvmaf_cuda_supported = False
            pi.libvmaf_cuda_reason = str(exc)
            pi.cuda_nvcc_reason = str(exc)
            explicit_root = configured or os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH")
            if explicit_root and detected:
                root = Path(explicit_root).expanduser().resolve()
                if root != Path(detected).resolve().parent.parent:
                    pi.cuda_available = False
                    pi.cuda_path = None
            if config.cuda_build_mode == "required":
                raise
            session.notes.append(f"CUDA auto mode: CUDA compilation disabled: {exc}")
            return
        pi.cuda_build_host_inputs = inputs
        pi.cuda_available = True
        pi.cuda_path = str(inputs.nvcc_path)
        pi.cuda_version = inputs.detected_cuda_version
        assert inputs.compute_architecture is not None
        pi.cuda_compute_capability = inputs.compute_architecture.removeprefix("compute_")
        pi.nvcc_ccbin = str(inputs.ccbin_path)
        pi.cuda_nvcc_supported = True
        pi.libvmaf_cuda_supported = True
        pi.cuda_nvcc_reason = pi.libvmaf_cuda_reason = "CUDA profile probes passed."
        session.notes.append(
            f"CUDA profile: NVCC {inputs.nvcc_path} ({inputs.nvcc_version}), "
            f"host {inputs.ccbin_path}, {inputs.compute_architecture}, "
            f"flags {' '.join(inputs.common_flags())}."
        )
        if not (config.gpl_enabled or config.allow_nonfree_cuda):
            session.notes.append(
                "FFmpeg CUDA/NVCC disabled: enable allow_nonfree_cuda to accept an "
                "unredistributable build. libvmaf CUDA remains available."
            )

    def recheck_target_runtime(self, config: BuildConfig, session: TargetSession) -> Optional[str]:
        """Re-probe before runtime acceptance; return a warning message if drifted.

        Only the WSL-Mingw route is checked. Full runtime acceptance is a later gate;
        this records invalidation in the state so it can never be trusted stale.
        """
        if not self._is_cross_mingw(config):
            return None
        baseline = baseline_from_state(self.state_manager.get()) or session.identity
        try:
            target = self._probe_windows_target(config)
            current = baseline.with_runtime(runtime_identity(target))
        except BuildError as exc:
            invalidate_acceptance(self.state_manager, f"re-probe failed: {exc}", [])
            return f"Windows target re-probe failed; runtime acceptance invalid: {exc}"
        drift = recheck_runtime_identity(baseline, current)
        if drift.build_unsafe or not drift.acceptance_valid:
            invalidate_acceptance(
                self.state_manager,
                "runtime identity changed after build start",
                [d.path for d in drift.comparison.differences],
            )
            return drift.message
        return None

    def _build_policy(self, config: BuildConfig) -> FfmpegPolicy:
        return evaluate_ffmpeg_policy(config, self.platform_info, self.tools)

    def _refresh_system_report(self, config: BuildConfig) -> None:
        report_gen = SystemReportGenerator(
            self.system_info,
            self.platform_info,
            self.tools,
            config=config,
        )
        self.system_report = report_gen.generate()

    def run(self) -> int:
        """Run the application.

        Returns:
            Exit code.
        """
        try:
            while True:
                config = self.config_manager.get()
                self._sync_workspace(config)
                policy = self._build_policy(config)
                self._refresh_system_report(config)
                state = self.state_manager.load()
                components = self._get_buildable_components(config)

                action = self.system_screen.show(
                    self.system_report,
                    config,
                    state,
                    len(components),
                    policy,
                )

                if action == "build":
                    if not policy.selected_version_allowed:
                        self.console.print(
                            f"[red]Build blocked by FFmpeg policy:[/red] {policy.blocked_reason}"
                        )
                        continue
                    self._run_build(config, resume=False)
                elif action == "resume":
                    if state or self._is_cross_mingw(config):
                        if not policy.selected_version_allowed:
                            self.console.print(
                                f"[red]Resume blocked by FFmpeg policy:[/red] {policy.blocked_reason}"
                            )
                            continue
                        self._run_build(config, resume=True)
                    else:
                        self.console.print("[red]No previous build to resume.[/red]")
                        continue
                elif action == "config":
                    config = self.config_screen.show(config, policy.available_versions)
                    self.config_manager.save(config)
                elif action == "cleanup":
                    self._cleanup()
                elif action == "info":
                    self.info_screen.show(
                        components,
                        self.registry.get_all(),
                        toolchain_reasons=self._component_toolchain_reasons(config),
                    )
                    continue
                elif action == "toolchain":
                    self._show_toolchain_plan(config)
                    continue
                elif action == "store":
                    self._show_toolchain_store(config)
                    continue
                elif action == "help":
                    self.help_screen.show()
                    continue
                elif action == "exit":
                    return 0

                self.console.print()
                self.console.input("Press Enter to continue...")

        except KeyboardInterrupt:
            self.console.print("\n[yellow]Interrupted by user.[/yellow]")
            return 130
        except Exception as e:
            self.console.print(f"\n[red]Fatal error: {e}[/red]")
            return 1

    def _get_buildable_components(self, config: BuildConfig) -> List[Component]:
        platform = self.platform_detector.get_platform_name()
        if self._is_cross_mingw(config):
            selection = self._get_component_selection(config)
            selected = {
                component.name: component
                for component in (
                    selection.target_inputs
                    + selection.target_components
                    + selection.mixed_components
                )
            }
            return [
                selected[component.name]
                for component in self.registry.get_all()
                if component.name in selected
            ]
        return self.registry.get_buildable(
            config.gpl_enabled,
            platform,
            self.tools,
            config.disable_lv2,
            config.enable_libvmaf,
            self.platform_info,
            full_static=config.full_static,
            ffmpeg_version=config.ffmpeg_version,
            toolchain_checker=self._component_toolchain_checker(config),
        )

    def _get_component_selection(self, config: BuildConfig) -> ComponentSelection:
        """Return the role-aware selection for the WSL-Mingw build graph."""
        if not self._is_cross_mingw(config):
            raise ValueError("Role-aware selection is only available for linux-cross-mingw")
        return self.registry.get_buildable_selection(
            config.gpl_enabled,
            self.platform_detector.get_platform_name(),
            self.tools,
            disable_lv2=config.disable_lv2,
            enable_libvmaf=config.enable_libvmaf,
            platform_info=self.platform_info,
            ffmpeg_version=config.ffmpeg_version,
            toolchain_checker=self._component_toolchain_checker(config),
            target_backend=TARGET_BACKEND_CROSS_MINGW,
            host_tool_checker=self._host_tool_available,
            target_platform_info=self._cross_target_capabilities(config),
        )

    def _cross_target_capabilities(self, config: BuildConfig) -> Optional[Any]:
        """Project Windows/runtime facts without consulting WSL GPU hardware."""
        state = self.windows_target_state
        if state is None:
            return None
        cuda_enabled = config.cuda_build_mode != "disabled" and (
            config.cuda_build_mode == "required"
            or (config.enable_libvmaf and config.enable_libvmaf_cuda)
        )
        nvidia_api = next((value for value in state.nvidia.api.values() if value), None)
        opencl_present = state.opencl.state == FeatureState.PRESENT
        return SimpleNamespace(
            cuda_available=cuda_enabled,
            nvenc_api_version=nvidia_api,
            vulkan_available=config.enable_libplacebo_vulkan,
            amf_available=False,
            opencl_available=opencl_present,
            opencl_runtime_available=opencl_present,
            qsv_available=False,
            is_msys2=False,
            is_ucrt64=False,
            build_backend=TARGET_BACKEND_CROSS_MINGW,
        )

    @staticmethod
    def _host_tool_available(tool: str) -> bool:
        """Check host executable availability without installing or mutating it."""
        aliases = {
            "pkg-config": ("pkg-config", "pkgconf"),
            "libtool": ("libtool", "libtoolize"),
            "automake": ("automake", "automake-1.18", "automake-1.17", "automake-1.16"),
            "autoconf": ("autoconf", "autoconf-2.72", "autoconf-2.71"),
        }
        return any(shutil.which(name) is not None for name in aliases.get(tool, (tool,)))

    def _component_toolchain_checker(self, config: BuildConfig) -> Callable[[Component], bool]:
        """Per-component toolchain gate for skipped components.

        A component is kept when its resolved toolchain is ``system`` (always
        available by policy), when the required profile is installed in the
        store, or when it is ``rav1e`` (always kept so a missing toolchain
        raises a build error instead of silently skipping). Everything else —
        unknown profile, non-deployable profile, or not installed — is
        excluded.
        """
        try:
            toolchain_registry = ToolchainRegistry()
        except (OSError, ValueError):
            return lambda component: True
        installed = self._installed_toolchains(config)
        versions = self._system_tool_versions()
        platform = self.platform_detector.get_platform_name()

        def _check(component: Component) -> bool:
            if component.name == "rav1e":
                return True
            assignment = toolchain_registry.resolve_component_toolchain(
                component.toolchain, config.ffmpeg_version, platform, versions
            )
            if assignment is None or assignment.mode == "system":
                return True
            profile_name = assignment.profile
            if profile_name is None:
                return False
            profile = toolchain_registry.get_profile(profile_name)
            if profile is None:
                return False
            if not toolchain_registry.is_profile_available(profile, platform):
                return False
            return installed.get(profile_name) is True

        return _check

    @staticmethod
    def _toolchain_backend(config: BuildConfig) -> Optional[str]:
        return (
            TARGET_BACKEND_CROSS_MINGW
            if config.target_backend == TARGET_BACKEND_CROSS_MINGW
            else None
        )

    def _installed_toolchains(self, config: BuildConfig) -> Dict[str, bool]:
        """Installed toolchain profiles mapped to their verification status."""
        store = ToolchainStore(
            resolve_store_root(PROJECT_ROOT),
            workspace=self.workspace,
            backend=self._toolchain_backend(config),
        )
        try:
            registry = ToolchainRegistry()
        except (OSError, ValueError):
            registry = None
        return {
            item.profile: item.verified
            and (
                registry is None
                or store.installed_status(registry, item.profile)[0] == "compatible"
            )
            for item in store.list_installed()
        }

    def _component_toolchain_reasons(self, config: BuildConfig) -> Dict[str, str]:
        """Explain why buildable-excluded components were gated by toolchain.

        Returns a mapping of component name to a human-readable reason for
        components that are otherwise eligible but excluded by the toolchain
        gate (unknown / non-deployable / not-installed required profile).
        """
        try:
            toolchain_registry = ToolchainRegistry()
        except (OSError, ValueError):
            return {}
        installed = self._installed_toolchains(config)
        versions = self._system_tool_versions()
        platform = self.platform_detector.get_platform_name()
        reasons: Dict[str, str] = {}
        for component in self.registry.get_all():
            if component.name == "rav1e":
                continue
            assignment = toolchain_registry.resolve_component_toolchain(
                component.toolchain, config.ffmpeg_version, platform, versions
            )
            if assignment is None or assignment.mode == "system":
                continue
            profile_name = assignment.profile
            if profile_name is None:
                continue
            profile = toolchain_registry.get_profile(profile_name)
            if profile is None:
                reasons[component.name] = f"unknown toolchain profile {profile_name!r}"
            elif not toolchain_registry.is_profile_available(profile, platform):
                reasons[component.name] = (
                    f"toolchain {profile_name!r} not available for {platform} "
                    "(missing source or platform)"
                )
            elif profile_name not in installed:
                reasons[component.name] = (
                    f"toolchain {profile_name!r} not installed (use the Toolchain store)"
                )
            elif installed.get(profile_name) is not True:
                reasons[component.name] = (
                    f"toolchain {profile_name!r} failed verification (re-acquire it)"
                )
        return reasons

    def _system_tool_versions(self) -> Dict[str, str]:
        """Map detected system tool names to version strings for range checks."""
        return detected_system_tool_versions(self.platform_info, self.tools)

    def _show_toolchain_plan(self, config: BuildConfig) -> None:
        """Show the read-only resolved-toolchain plan for the current config."""
        try:
            toolchain_registry = ToolchainRegistry()
        except (OSError, ValueError) as exc:
            self.console.print(f"[red]Toolchain registry unavailable: {exc}[/red]")
            return
        components = self._get_buildable_components(config)
        plan = toolchain_registry.plan_component_toolchains(
            components,
            config.ffmpeg_version,
            self.platform_detector.get_platform_name(),
            self._system_tool_versions(),
            cuda_version=self.platform_info.cuda_version,
        )
        self.toolchain_screen.show(plan)

    def _show_toolchain_store(self, config: BuildConfig) -> None:
        """Show the persistent toolchain store (acquire/remove/list)."""
        try:
            toolchain_registry = ToolchainRegistry()
        except (OSError, ValueError) as exc:
            self.console.print(f"[red]Toolchain registry unavailable: {exc}[/red]")
            return
        store = ToolchainStore(
            resolve_store_root(PROJECT_ROOT),
            workspace=self.workspace,
            source_archives_dir=PROJECT_ROOT / config.source_archives_dir,
            allow_network_downloads=config.allow_network_downloads,
            backend=self._toolchain_backend(config),
        )
        recommended = toolchain_registry.recommend_profiles(
            config.ffmpeg_version,
            self.platform_detector.get_platform_name(),
            self._system_tool_versions(),
            components=self.registry.get_all(),
            installed=[item.profile for item in store.list_installed()],
            cuda_version=self.platform_info.cuda_version,
            backend=(
                TARGET_BACKEND_CROSS_MINGW
                if config.target_backend == TARGET_BACKEND_CROSS_MINGW
                else None
            ),
        )
        self.store_screen.show(store, toolchain_registry, recommended=recommended)

    def _component_toolchain_env(
        self, config: BuildConfig, components: List[Component]
    ) -> Dict[str, Dict[str, str]]:
        """Per-component environment overrides from installed toolchains."""
        try:
            toolchain_registry = ToolchainRegistry()
        except (OSError, ValueError):
            return {}
        store = ToolchainStore(
            resolve_store_root(PROJECT_ROOT),
            workspace=self.workspace,
            source_archives_dir=PROJECT_ROOT / config.source_archives_dir,
            allow_network_downloads=config.allow_network_downloads,
            backend=self._toolchain_backend(config),
        )
        versions = self._system_tool_versions()
        platform = self.platform_detector.get_platform_name()
        result: Dict[str, Dict[str, str]] = {}
        for component in components:
            assignment = toolchain_registry.resolve_component_toolchain(
                component.toolchain, config.ffmpeg_version, platform, versions
            )
            if assignment is None or assignment.mode != "deploy" or not assignment.profile:
                continue
            env = store.component_env(toolchain_registry, assignment.profile)
            if env:
                result[component.name] = env
        return result

    def _run_build(self, config: BuildConfig, resume: bool = False) -> None:
        """Run the build process.

        Args:
            config: Build configuration.
            resume: Whether to resume from previous state.
        """
        if FFmpegBuilderApp._is_cross_mingw(config):
            from .g4_final import run_g4_final

            result = run_g4_final(self, config)
            if result == 2:
                self.console.print(
                    "[red]Build preparation blocked by identity guard or prerequisites.[/red]"
                )
            elif result:
                self.console.print("[red]G4 FFmpeg integration failed.[/red]")
            elif config.make_release:
                from .g6 import run_g6

                if run_g6(self, config):
                    self.console.print("[red]G6 Windows packaging acceptance failed.[/red]")
            return
        try:
            session = self.prepare_target(config, resume)
            all_components = self._get_buildable_components(config)
            validate_required_cuda_components(
                config, all_components, self.platform_info.cuda_build_host_inputs
            )
        except BuildError as e:
            self.console.print(
                f"[red]Build preparation blocked (identity guard/prerequisite validation):[/red] {e}"
            )
            return
        for note in session.notes:
            self.console.print(f"[yellow]{note}[/yellow]")

        components = list(all_components)
        nv_codec_identity = cuda_nv_codec_identity(
            all_components,
            vmaf_headers=(
                self.registry.get_nv_codec_component()
                if self.platform_info.cuda_build_host_inputs
                and config.enable_libvmaf
                and config.enable_libvmaf_cuda
                else None
            ),
        )
        if resume:
            from .builders.graphics.vulkan_headers import invalidate_vulkan_resume

            invalidate_vulkan_resume(self.state_manager, self.workspace, all_components)
            invalidate_cuda_resume(
                self.state_manager,
                self.platform_info.cuda_build_host_inputs,
                libvmaf_cuda=config.enable_libvmaf and config.enable_libvmaf_cuda,
                ffmpeg_cuda=config.gpl_enabled or config.allow_nonfree_cuda,
                nv_codec=nv_codec_identity,
            )
            resume_point = self.state_manager.get_resume_point()
            if resume_point:
                idx = next((i for i, c in enumerate(components) if c.name == resume_point), 0)
                components = components[idx:]
                self.console.print(f"[green]Resuming from {resume_point}[/green]")
        else:
            self.state_manager.reset()

        dashboard = BuildDashboard(
            self.console,
            config.ffmpeg_version,
            self.platform_detector.get_num_jobs(config.num_jobs),
            config.download_workers if config.async_downloads else 0,
        )
        dashboard.set_components(all_components)

        def on_status(
            name: str,
            status: ComponentStatus,
            version: Optional[str],
            error_message: Optional[str],
            detail: Optional[str] = None,
        ) -> None:
            dashboard.update_status(name, status, version, detail or "")
            dashboard.log(f"{name} {status.value}")

        def on_download_status(name: str, status: str) -> None:
            dashboard.update_download_status(name, ComponentStatus(status))

        def on_download_progress(name: str, downloaded: int, total: int) -> None:
            dashboard.update_download_progress(name, downloaded, total)

        builder = FFmpegBuilder(
            config,
            self.workspace,
            self.packages,
            self.state_manager,
            self.platform_detector,
            on_download_status,
            dashboard.log,
            on_download_progress,
            toolchain_env=self._component_toolchain_env(config, all_components),
        )

        state = self.state_manager.get()
        state.config = config.to_dict()
        state.total_steps = len(all_components)
        state.cuda_profile = cuda_profile_identity(
            self.platform_info.cuda_build_host_inputs,
            libvmaf_cuda=config.enable_libvmaf and config.enable_libvmaf_cuda,
            ffmpeg_cuda=config.gpl_enabled or config.allow_nonfree_cuda,
            nv_codec=nv_codec_identity,
        )
        self.state_manager.save()
        session.stamp(self.state_manager)

        for name, component_state in state.components.items():
            dashboard.update_status(name, component_state.status, component_state.version)
            dashboard.reveal(name)

        self.state_manager.status_listener = on_status
        live: Optional[Live] = None
        success = True
        error_message = None
        idx = 1

        try:
            builder.prefetch_downloads(components)
            if self.console.is_terminal:
                live = Live(dashboard, console=self.console, refresh_per_second=8, screen=True)
                live.start()
            else:
                dashboard.log("Live dashboard disabled because output is not a terminal")

            while idx <= len(components):
                component = components[idx - 1]
                state.current_step = all_components.index(component) + 1
                self.state_manager.save()

                try:
                    builder.build_component(component)
                    current_state = self.state_manager.get().components.get(component.name)
                    if current_state is None or current_state.status != ComponentStatus.SYSTEM:
                        self.state_manager.mark_component_status(
                            component.name,
                            ComponentStatus.COMPLETED,
                            component.version,
                        )
                    idx += 1

                except SkipComponent as e:
                    self.state_manager.mark_component_status(
                        component.name,
                        ComponentStatus.SKIPPED,
                        component.version,
                        str(e),
                    )
                    idx += 1
                    continue

                except BuildError as e:
                    self.state_manager.mark_component_status(
                        component.name,
                        ComponentStatus.FAILED,
                        component.version,
                        str(e),
                        str(e.log_file) if e.log_file else None,
                    )

                    if live is not None:
                        live.stop()
                    action = self.error_handler.handle_error(
                        component.name,
                        str(e),
                        e.log_file,
                    )
                    if live is not None and action != "abort":
                        live.start()

                    if action == "retry":
                        builder.retry_download(component)
                        self.state_manager.mark_component_status(
                            component.name,
                            ComponentStatus.PENDING,
                            component.version,
                        )
                        continue
                    if action == "skip":
                        self.state_manager.mark_component_status(
                            component.name,
                            ComponentStatus.SKIPPED,
                            component.version,
                        )
                        idx += 1
                        continue
                    if action == "abort":
                        success = False
                        error_message = str(e)
                        break
        finally:
            if live is not None:
                live.stop()
            self.state_manager.status_listener = None
            builder.shutdown_downloads(wait=False)

        self.final_screen.show(
            self.state_manager.get(),
            self.workspace,
            success,
            error_message,
        )

        if success:
            drift = self.recheck_target_runtime(config, session)
            if drift:
                self.console.print(f"[yellow]{drift}[/yellow]")

        if success and config.make_release:
            try:
                release_dir = builder.make_release_bundle()
                self.console.print(f"[green]Release bundle created: {release_dir}[/green]")
            except BuildError as e:
                self.console.print(f"[red]Release bundle failed: {e}[/red]")

    def _cleanup(self) -> None:
        """Cleanup workspace and packages."""
        import shutil
        import stat

        def _rmtree(path: Path) -> None:
            def _on_error(
                func: Callable[..., Any],
                fpath: str,
                exc_info: Tuple[Type[BaseException], BaseException, TracebackType],
            ) -> None:
                try:
                    os.chmod(fpath, stat.S_IWRITE)
                    func(fpath)
                except Exception:
                    pass

            shutil.rmtree(path, onerror=_on_error)

        self.state_manager.reset()
        self.console.print("[green]Reset build state[/green]")
        self.workspace = self.base_workspace
        self.packages = self.workspace / "packages"

        legacy_workspace = PROJECT_ROOT / "workspace"
        if legacy_workspace.exists():
            _rmtree(legacy_workspace)
            self.console.print(f"[green]Removed legacy {legacy_workspace}[/green]")

        if self.workspace.exists():
            _rmtree(self.workspace)
            self.console.print(f"[green]Removed {self.workspace}[/green]")

        if self.packages.exists():
            _rmtree(self.packages)
            self.console.print(f"[green]Removed {self.packages}[/green]")
