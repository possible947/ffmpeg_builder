"""Main application class for FFmpeg Builder."""

import os
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, List, Optional, Tuple, Type

from rich.console import Console
from rich.live import Live

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import Component, ComponentRegistry
from .config import BuildConfig, ConfigManager
from .platform_detect import PlatformDetector
from .state import ComponentStatus, StateManager
from .system_report import SystemReportGenerator
from .ui.dashboard import BuildDashboard
from .ui.error_handler import ErrorHandler
from .ui.screens import ConfigScreen, FinalReportScreen, HelpScreen, InfoScreen, SystemReportScreen

# The package is the repository root (flat layout); anchor the default
# workspace/config paths to it so the app works from any CWD.
PROJECT_ROOT = Path(__file__).resolve().parent


class FFmpegBuilderApp:
    """Main application class."""

    def __init__(self, workspace: Optional[Path] = None):
        """Initialize application.

        Args:
            workspace: Workspace directory. If None, uses ./workspace.
        """
        self.workspace = workspace or PROJECT_ROOT / "workspace"
        self.packages = self.workspace / "packages"
        self.console = Console()

        self.config_manager = ConfigManager(PROJECT_ROOT / "build_config.yaml")
        self.state_manager = StateManager(self.workspace / "build_state.json")

        self.platform_detector = PlatformDetector()
        self.system_info, self.platform_info, self.tools = self.platform_detector.detect_all()

        report_gen = SystemReportGenerator(
            self.system_info,
            self.platform_info,
            self.tools,
            config=self.config_manager.load(),
        )
        self.system_report = report_gen.generate()

        self.registry = ComponentRegistry()

        self.system_screen = SystemReportScreen(self.console)
        self.config_screen = ConfigScreen(self.console)
        self.info_screen = InfoScreen(self.console)
        self.final_screen = FinalReportScreen(self.console)
        self.help_screen = HelpScreen(self.console)
        self.error_handler = ErrorHandler(self.console)

    def run(self) -> int:
        """Run the application.

        Returns:
            Exit code.
        """
        try:
            while True:
                config = self.config_manager.get()
                state = self.state_manager.load()
                components = self._get_buildable_components(config)

                action = self.system_screen.show(
                    self.system_report,
                    config,
                    state,
                    len(components),
                )

                if action == "build":
                    self._run_build(config, resume=False)
                elif action == "resume":
                    if state:
                        self._run_build(config, resume=True)
                    else:
                        self.console.print("[red]No previous build to resume.[/red]")
                        continue
                elif action == "config":
                    config = self.config_screen.show(config)
                    self.config_manager.save(config)
                elif action == "cleanup":
                    self._cleanup()
                elif action == "info":
                    self.info_screen.show(components, self.registry.get_all())
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
        )

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

        if self.workspace.exists():
            _rmtree(self.workspace)
            self.console.print(f"[green]Removed {self.workspace}[/green]")

        if self.packages.exists():
            _rmtree(self.packages)
            self.console.print(f"[green]Removed {self.packages}[/green]")
