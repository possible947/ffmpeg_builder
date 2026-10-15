"""Main application class for FFmpeg Builder."""

import os
import re
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from rich.console import Console
from rich.live import Live

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import Component, ComponentRegistry
from .config import BuildConfig, ConfigManager
from .ffmpeg_policy import FfmpegPolicy, evaluate_ffmpeg_policy
from .platform_detect import PlatformDetector
from .state import ComponentStatus, StateManager
from .system_report import SystemReportGenerator
from .toolchain_registry import ToolchainRegistry
from .toolchain_store import ToolchainStore
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


def _leading_version(value: object) -> Optional[str]:
    """Extract the first dotted version (or bare number) from a raw string."""
    if not value:
        return None
    text = str(value)
    match = re.search(r"\d+(?:\.\d+)+", text) or re.search(r"\d+", text)
    return match.group(0) if match else None


class FFmpegBuilderApp:
    """Main application class."""

    def __init__(self, workspace: Optional[Path] = None):
        """Initialize application.

        Args:
            workspace: Workspace directory. If None, uses versioned workspace (e.g. workspace_81).
        """
        self._custom_workspace = workspace
        self.console = Console()

        self.config_manager = ConfigManager(PROJECT_ROOT / "build_config.yaml")
        config = self.config_manager.load()
        self._sync_workspace(config)

        self.platform_detector = PlatformDetector()
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
        self.packages = self.workspace / "packages"
        self.state_manager = StateManager(self.workspace / "build_state.json")

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
                    if state:
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
                    self.info_screen.show(components, self.registry.get_all())
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
        installed = {
            item.profile
            for item in ToolchainStore(
                PROJECT_ROOT / "toolchains",
                workspace=self.workspace,
            ).list_installed()
        }
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
            return profile_name in installed

        return _check

    def _system_tool_versions(self) -> Dict[str, str]:
        """Map detected system tool names to version strings for range checks."""
        versions: Dict[str, str] = {}
        if self.platform_info.gcc_major_version is not None:
            versions["gcc"] = str(self.platform_info.gcc_major_version)
        for name in ("gcc", "clang"):
            info = self.tools.get(name)
            parsed = _leading_version(getattr(info, "version", None))
            if parsed:
                versions[name] = parsed
        return versions

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
            PROJECT_ROOT / "toolchains",
            workspace=self.workspace,
            source_archives_dir=PROJECT_ROOT / config.source_archives_dir,
            allow_network_downloads=config.allow_network_downloads,
        )
        self.store_screen.show(store, toolchain_registry)

    def _component_toolchain_env(
        self, config: BuildConfig, components: List[Component]
    ) -> Dict[str, Dict[str, str]]:
        """Per-component environment overrides from installed toolchains."""
        try:
            toolchain_registry = ToolchainRegistry()
        except (OSError, ValueError):
            return {}
        store = ToolchainStore(
            PROJECT_ROOT / "toolchains",
            workspace=self.workspace,
            source_archives_dir=PROJECT_ROOT / config.source_archives_dir,
            allow_network_downloads=config.allow_network_downloads,
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
        all_components = self._get_buildable_components(config)
        components = list(all_components)

        if resume:
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
        self.state_manager.save()

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
