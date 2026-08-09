"""UI screens using rich library."""

from pathlib import Path
from typing import List, Optional, Set

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..components import Component
from ..config import BuildConfig
from ..state import BuildState, ComponentStatus
from ..system_report import SystemReport

_START_KEYS: List[tuple] = [
    ("b", "Start new build", "Build all components from scratch (resets previous state)"),
    ("r", "Resume previous build", "Continue from the last interrupted build"),
    ("c", "Edit configuration", "Change build flags, jobs, async downloads"),
    ("w", "Cleanup workspace", "Remove state, sources, and build artifacts"),
    ("i", "Component info", "List all components (buildable + skipped)"),
    ("h", "Help", "Show full key reference for all screens"),
    ("q", "Exit", "Quit the application"),
]

_INFO_KEYS: List[tuple] = [
    ("n", "Next page", "Show the next page of components"),
    ("p", "Previous page", "Show the previous page of components"),
    ("q", "Back", "Return to the system report"),
]

_ERROR_KEYS: List[tuple] = [
    ("r", "Retry", "Re-attempt the failed step for this component"),
    ("s", "Skip", "Mark the component as skipped and continue"),
    ("a", "Abort", "Stop the build and return to the start screen"),
    ("l", "Show full log", "Print the full log of the failed step"),
]

_DASHBOARD_KEYS: List[tuple] = [
    ("—", "Live updates", "The dashboard refreshes automatically; no input required"),
    ("Ctrl+C", "Interrupt", "Abort the build and return to the start screen"),
]


class UIScreen:
    """Base UI screen."""

    def __init__(self, console: Console):
        """Initialize screen.

        Args:
            console: Rich console instance.
        """
        self.console = console


class SystemReportScreen(UIScreen):
    """System report screen."""

    def show(
        self,
        report: SystemReport,
        config: BuildConfig,
        state: Optional[BuildState] = None,
        buildable_count: Optional[int] = None,
    ) -> str:
        """Show system report screen.

        Args:
            report: System report.
            config: Build configuration.
            state: Previous build state.
            buildable_count: Number of buildable components.

        Returns:
            User action: "build", "resume", "config", "cleanup", "info", or "exit".
        """
        self.console.clear()
        self.console.print(
            Panel.fit("[bold blue]FFmpeg Builder - System Report[/bold blue]", border_style="blue")
        )
        self.console.print()

        hw_table = Table(title="Hardware", show_header=False)
        hw_table.add_column("Property", style="cyan")
        hw_table.add_column("Value")
        hw_table.add_row("CPU", report.system_info.cpu_model)
        hw_table.add_row("Cores", str(report.system_info.cpu_cores))
        hw_table.add_row("RAM", f"{report.system_info.ram_gb:.1f} GB")
        if report.system_info.gpu_info:
            hw_table.add_row("GPU", ", ".join(report.system_info.gpu_info))
        self.console.print(hw_table)

        sw_table = Table(title="Software", show_header=False)
        sw_table.add_column("Property", style="cyan")
        sw_table.add_column("Value")
        os_display = report.system_info.os_name
        if report.system_info.os_version:
            os_display = f"{report.system_info.os_name} {report.system_info.os_version}"
        if report.platform_info.is_wsl2:
            os_display += " (WSL2)"
        compiler = report.get_compiler_info()
        runtime_mode = report.platform_info.platform
        backend_mode = report.platform_info.build_backend
        if report.platform_info.is_windows and report.platform_info.is_msys2:
            msystem = report.platform_info.msystem or "MSYS2"
            runtime_mode = f"windows ({msystem})"
        sw_table.add_row("OS", os_display)
        sw_table.add_row("Build Platform", runtime_mode)
        sw_table.add_row("Build Backend", backend_mode)
        sw_table.add_row("Architecture", report.system_info.architecture)
        sw_table.add_row("Compiler", f"{compiler['compiler']} {compiler['version']}")
        self.console.print(sw_table)

        available = sum(1 for t in report.tools.values() if t.available)
        missing = sum(1 for t in report.tools.values() if not t.available)
        tools_summary = f"{available}/{len(report.tools)} available"
        if missing:
            names = sorted(name for name, t in report.tools.items() if not t.available)
            tools_summary += f" · missing: {', '.join(names)}"
        tools_table = Table(title="Available Tools", show_header=False)
        tools_table.add_column("Property", style="cyan")
        tools_table.add_column("Value")
        tools_table.add_row("Summary", tools_summary)
        hwaccel = report.get_hardware_acceleration_status()
        if hwaccel:
            accel = ", ".join(name for name, ok in hwaccel.items() if ok)
            tools_table.add_row("HW accel", accel or "none")
        if report.platform_info.cuda_available:
            status = "yes" if report.platform_info.libvmaf_cuda_supported else "no"
            reason = report.platform_info.libvmaf_cuda_reason or "unknown"
            tools_table.add_row("libvmaf CUDA", f"{status} ({reason})")
        self.console.print(tools_table)

        config_table = Table(title="Build Configuration", show_header=False)
        config_table.add_column("Property", style="cyan")
        config_table.add_column("Value")
        config_table.add_row("FFmpeg Version", config.ffmpeg_version)
        config_table.add_row("GPL Enabled", "Yes" if config.gpl_enabled else "No")
        config_table.add_row("Make Release", "Yes" if config.make_release else "No")
        config_table.add_row("Native Build", "Yes" if config.native_build else "No")
        config_table.add_row("OpenMP", "Yes" if config.openmp else "No")
        config_table.add_row("Full Static", "Yes" if config.full_static else "No")
        config_table.add_row("libvmaf", "Yes" if config.enable_libvmaf else "No")
        config_table.add_row("libvmaf CUDA", "Yes" if config.enable_libvmaf_cuda else "No")
        config_table.add_row("libplacebo", "Yes")
        config_table.add_row(
            "libplacebo Vulkan", "Yes" if config.enable_libplacebo_vulkan else "No"
        )
        config_table.add_row("Parallel Jobs", str(config.num_jobs))
        config_table.add_row(
            "Async Downloads",
            f"Yes (workers: {config.download_workers})" if config.async_downloads else "No",
        )
        if report.platform_info.is_windows:
            backend = config.windows.backend
            if report.platform_info.is_msys2:
                backend += " / posix"
            config_table.add_row("Windows Backend", backend)
        if buildable_count is not None:
            config_table.add_row(
                "Components", f"{buildable_count} buildable · press [i] for full list"
            )
        self.console.print(config_table)

        if state:
            completed = sum(
                1
                for c in state.components.values()
                if c.status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM)
            )
            total = state.total_steps or len(state.components)
            state_table = Table(title="Previous Build", show_header=False)
            state_table.add_column("Property", style="cyan")
            state_table.add_column("Value")
            state_table.add_row("Build ID", state.build_id)
            state_table.add_row("Started", state.started_at)
            state_table.add_row("Progress", f"{completed}/{total} components")
            self.console.print(state_table)

        self.console.print()
        actions_table = Table(
            title="Actions (press key, then Enter)",
            show_header=True,
            header_style="bold cyan",
            title_justify="left",
        )
        actions_table.add_column("Key", style="bold", no_wrap=True, width=4)
        actions_table.add_column("Action", style="cyan", no_wrap=True, width=24)
        actions_table.add_column("Description")
        actions_table.add_row(
            "b", "Start new build", "Build all components from scratch (resets previous state)"
        )
        actions_table.add_row(
            "r", "Resume previous build", "Continue from the last interrupted build"
        )
        actions_table.add_row(
            "c", "Edit configuration", "Change build flags, jobs, async downloads"
        )
        actions_table.add_row(
            "w", "Cleanup workspace", "Remove state, sources, and build artifacts"
        )
        actions_table.add_row("i", "Component info", "List all components (buildable + skipped)")
        actions_table.add_row("h", "Help", "Show full key reference for all screens")
        actions_table.add_row("q", "Exit", "Quit the application")
        self.console.print(actions_table)

        choices = ["b", "c", "w", "i", "h", "q"]
        if state:
            choices.insert(1, "r")
        choice = Prompt.ask("Choice", choices=choices)

        if choice == "h":
            return "help"

        return {
            "b": "build",
            "r": "resume",
            "c": "config",
            "w": "cleanup",
            "i": "info",
            "q": "exit",
        }[choice]


class HelpScreen(UIScreen):
    """Full key reference screen."""

    def show(self) -> None:
        """Show the help screen with keys for every screen."""
        self.console.clear()
        self.console.print(
            Panel.fit(
                "[bold blue]FFmpeg Builder - Key Reference[/bold blue]",
                border_style="blue",
            )
        )
        self.console.print()

        sections = [
            ("Start screen", _START_KEYS),
            ("Component info screen", _INFO_KEYS),
            ("Error prompt", _ERROR_KEYS),
            ("Build dashboard", _DASHBOARD_KEYS),
        ]
        for title, keys in sections:
            table = Table(
                title=title, show_header=True, header_style="bold cyan", title_justify="left"
            )
            table.add_column("Key", style="bold", no_wrap=True, width=10)
            table.add_column("Action", style="cyan", no_wrap=True, width=22)
            table.add_column("Description")
            for key, action, desc in keys:
                table.add_row(key, action, desc)
            self.console.print(table)
            self.console.print()

        self.console.print("[dim]All key choices must be confirmed with Enter.[/dim]")
        Prompt.ask("Press Enter to return")


class InfoScreen(UIScreen):
    """Component information screen."""

    def show(
        self, buildable: List[Component], all_components: Optional[List[Component]] = None
    ) -> None:
        """Show paged component information."""
        selected: Set[str] = {component.name for component in buildable}
        components = all_components or buildable
        page = 0
        page_size = max(8, self.console.size.height - 10)
        while True:
            self.console.clear()
            table = Table(
                title=f"FFmpeg Builder - Component Info ({len(buildable)} buildable)", expand=True
            )
            table.add_column("#", justify="right", width=4)
            table.add_column("Name", style="cyan")
            table.add_column("Version")
            table.add_column("Category")
            table.add_column("Notes")
            start = page * page_size
            end = min(len(components), start + page_size)
            for index, component in enumerate(components[start:end], start + 1):
                notes = []
                if component.system_component:
                    notes.append("system")
                if component.name not in selected:
                    notes.append("not selected")
                table.add_row(
                    str(index),
                    component.name,
                    component.version,
                    component.category.value,
                    ", ".join(notes),
                    style=None if component.name in selected else "dim",
                )
            self.console.print(table)
            self.console.print()

            hint = "[n] Next  [p] Previous  [h] Help  [q] Back"
            self.console.print(hint)
            choices = ["q", "h"]
            if end < len(components):
                choices.append("n")
            if page > 0:
                choices.append("p")
            choice = Prompt.ask("Choice", choices=choices)
            if choice == "q":
                return
            if choice == "n":
                page += 1
            elif choice == "p":
                page -= 1
            elif choice == "h":
                HelpScreen(self.console).show()


class ConfigScreen(UIScreen):
    """Configuration edit screen."""

    def show(self, config: BuildConfig) -> BuildConfig:
        """Show configuration edit screen.

        Args:
            config: Current configuration.

        Returns:
            Updated configuration.
        """
        self.console.clear()
        self.console.print("[bold blue]Edit Build Configuration[/bold blue]")
        self.console.print()

        config.gpl_enabled = Confirm.ask(
            "Enable GPL and non-free codecs?", default=config.gpl_enabled
        )
        config.make_release = Confirm.ask(
            "Create release bundle after successful build?",
            default=config.make_release,
        )
        config.native_build = Confirm.ask(
            "Enable native CPU optimizations?", default=config.native_build
        )
        config.openmp = Confirm.ask("Enable OpenMP parallelism?", default=config.openmp)
        config.full_static = Confirm.ask(
            "Build full static binary (Linux only)?", default=config.full_static
        )
        config.enable_libvmaf = Confirm.ask("Enable libvmaf?", default=config.enable_libvmaf)
        config.enable_libvmaf_cuda = Confirm.ask(
            "Enable CUDA path for libvmaf when supported?",
            default=config.enable_libvmaf_cuda,
        )
        config.enable_libplacebo_vulkan = Confirm.ask(
            "Enable libplacebo Vulkan GPU processing (requires Vulkan; disabled on full_static Linux)?",
            default=config.enable_libplacebo_vulkan,
        )
        config.disable_lv2 = Confirm.ask("Disable LV2 libraries?", default=config.disable_lv2)
        jobs = Prompt.ask("Number of parallel jobs", default=str(config.num_jobs))
        config.num_jobs = jobs
        config.async_downloads = Confirm.ask(
            "Enable async source downloads?", default=config.async_downloads
        )
        workers = Prompt.ask("Number of download workers", default=str(config.download_workers))
        config.download_workers = int(workers)

        self.console.print()
        self.console.print("[green]Configuration updated.[/green]")
        Prompt.ask("Press Enter to continue")

        return config


class FinalReportScreen(UIScreen):
    """Final build report screen."""

    def show(
        self,
        state: BuildState,
        workspace: Path,
        success: bool,
        error_message: Optional[str] = None,
    ) -> None:
        """Show final build report.

        Args:
            state: Final build state.
            workspace: Workspace directory.
            success: Whether build succeeded.
            error_message: Error message if failed.
        """
        self.console.clear()

        if success:
            self.console.print("[bold green]Build Completed Successfully![/bold green]")
        else:
            self.console.print("[bold red]Build Failed[/bold red]")

        self.console.print()

        completed = [
            name for name, c in state.components.items() if c.status == ComponentStatus.COMPLETED
        ]
        system = [
            name for name, c in state.components.items() if c.status == ComponentStatus.SYSTEM
        ]
        failed = [
            name for name, c in state.components.items() if c.status == ComponentStatus.FAILED
        ]
        skipped = [
            name for name, c in state.components.items() if c.status == ComponentStatus.SKIPPED
        ]

        summary_table = Table(title="Build Summary", show_header=True)
        summary_table.add_column("Status", style="cyan")
        summary_table.add_column("Count")
        summary_table.add_row("[green]Completed[/green]", str(len(completed)))
        summary_table.add_row("[cyan]System[/cyan]", str(len(system)))
        summary_table.add_row("[red]Failed[/red]", str(len(failed)))
        summary_table.add_row("[yellow]Skipped[/yellow]", str(len(skipped)))
        self.console.print(summary_table)
        self.console.print()

        if success:
            bin_dir = workspace / "bin"
            binaries = ["ffmpeg", "ffprobe", "ffplay"]

            bin_table = Table(title="Built Binaries", show_header=True)
            bin_table.add_column("Binary", style="cyan")
            bin_table.add_column("Path")

            for binary in binaries:
                bin_path = bin_dir / binary
                if bin_path.exists():
                    bin_table.add_row(binary, str(bin_path))
                else:
                    bin_table.add_row(binary, "[dim]Not built[/dim]")

            self.console.print(bin_table)
            self.console.print()

            if Confirm.ask("Install binaries to system?", default=False):
                self._install_binaries(bin_dir)

        else:
            if error_message:
                self.console.print(
                    Panel(
                        error_message,
                        title="Error Details",
                        border_style="red",
                    )
                )
                self.console.print()

            if failed:
                failed_table = Table(title="Failed Components", show_header=True)
                failed_table.add_column("Component", style="cyan")
                failed_table.add_column("Error")

                for name in failed:
                    comp_state = state.components[name]
                    error = comp_state.error_message or "Unknown error"
                    failed_table.add_row(name, error)

                self.console.print(failed_table)
                self.console.print()

    def _install_binaries(self, bin_dir: Path) -> None:
        """Install binaries to system.

        Args:
            bin_dir: Directory containing binaries.
        """
        import platform
        import shutil

        if platform.system() == "Darwin":
            install_dir = Path("/usr/local/bin")
        else:
            install_dir = Path.home() / ".local" / "bin"

        install_dir.mkdir(parents=True, exist_ok=True)

        binaries = ["ffmpeg", "ffprobe", "ffplay"]

        for binary in binaries:
            src = bin_dir / binary
            dst = install_dir / binary

            if src.exists():
                try:
                    shutil.copy2(src, dst)
                    self.console.print(f"[green]Installed {binary} to {install_dir}[/green]")
                except Exception as e:
                    self.console.print(f"[red]Failed to install {binary}: {e}[/red]")

        self.console.print()
        self.console.print("[green]Installation complete.[/green]")
