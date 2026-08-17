"""Command execution with logging and error handling."""

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Tools that are MSYS2-linked on UCRT64 and must run through sh.exe when
# invoked as a bare name (not a full native Windows path).
_UCRT64_SH_TOOLS = frozenset({"cmake", "ninja"})


@dataclass
class ExecutionResult:
    """Result of command execution."""

    success: bool
    returncode: int
    stdout: str
    stderr: str
    command: str

    @property
    def output(self) -> str:
        """Get combined output."""
        return self.stdout + self.stderr


class CommandExecutor:
    """Executes commands with logging and error handling."""

    def __init__(self, workspace: Path, log_dir: Optional[Path] = None):
        """Initialize executor.

        Args:
            workspace: Workspace directory.
            log_dir: Directory for log files.
        """
        self.workspace = workspace
        self.log_dir = log_dir or workspace / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        capture_output: bool = True,
        stdin: Optional[str] = None,
    ) -> ExecutionResult:
        """Execute a command.

        Args:
            command: Command and arguments.
            cwd: Working directory.
            env: Environment variables.
            timeout: Timeout in seconds.
            capture_output: Whether to capture output.

        Returns:
            ExecutionResult instance.
        """
        cmd_str = " ".join(str(c) for c in command)

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        try:
            run_command = list(command)
            if (
                os.name == "nt"
                and merged_env.get("MSYSTEM", "").upper() == "UCRT64"
                and run_command
            ):
                first = str(run_command[0])
                first_stem = Path(first).stem.lower()
                sh_path = shutil.which("sh", path=merged_env.get("PATH"))

                if first.startswith("./") and first.endswith(".py"):
                    # Python scripts must be run with the Python interpreter
                    # directly; sh.exe cannot execute them via shebang on Windows.
                    run_command = [sys.executable, first[2:]] + [str(c) for c in run_command[1:]]
                elif sh_path and (
                    first.startswith("./")
                    or (
                        first_stem in _UCRT64_SH_TOOLS
                        # A full native Windows path (C:\...) → run directly.
                        and not re.match(r"^[A-Za-z]:[/\\]", first)
                    )
                ):
                    # MSYS2-linked tools (cmake, ninja) and shell scripts need
                    # sh.exe to initialize the MSYS2 runtime.  Normalize
                    # backslashes in every argument so sh does not mis-interpret
                    # them as escape sequences (e.g. E:\Projects → E:/Projects).
                    run_command = [sh_path] + [
                        str(c).replace("\\", "/") for c in run_command
                    ]

            result = subprocess.run(
                run_command,
                cwd=cwd,
                env=merged_env,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
                input=stdin if stdin else None,
            )

            return ExecutionResult(
                success=result.returncode == 0,
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                command=cmd_str,
            )

        except subprocess.TimeoutExpired:
            return ExecutionResult(
                success=False,
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                command=cmd_str,
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                returncode=-1,
                stdout="",
                stderr=str(e),
                command=cmd_str,
            )

    def execute_with_log(
        self,
        command: List[str],
        component_name: str,
        step: str,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        stdin: Optional[str] = None,
    ) -> Tuple[ExecutionResult, Path]:
        """Execute a command and save output to log file.

        Args:
            command: Command and arguments.
            component_name: Name of the component.
            step: Build step name.
            cwd: Working directory.
            env: Environment variables.
            timeout: Timeout in seconds.

        Returns:
            Tuple of (ExecutionResult, log_file_path).
        """
        result = self.execute(command, cwd, env, timeout, capture_output=True, stdin=stdin)

        log_file = self.log_dir / f"{component_name}_{step}.log"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"Command: {result.command}\n")
            f.write(f"Return code: {result.returncode}\n")
            f.write(f"Working directory: {cwd}\n")
            f.write("=" * 80 + "\n")
            f.write("STDOUT:\n")
            f.write(result.stdout)
            f.write("\n" + "=" * 80 + "\n")
            f.write("STDERR:\n")
            f.write(result.stderr)

        return result, log_file

    def execute_make(
        self,
        cwd: Path,
        num_jobs: int,
        env: Optional[Dict[str, str]] = None,
        component_name: str = "",
        step: str = "build",
    ) -> Tuple[ExecutionResult, Path]:
        """Execute make command.

        Args:
            cwd: Working directory.
            num_jobs: Number of parallel jobs.
            env: Environment variables.
            component_name: Name of the component.
            step: Build step name.

        Returns:
            Tuple of (ExecutionResult, log_file_path).
        """
        command = ["make", f"-j{num_jobs}"]
        return self.execute_with_log(command, component_name, step, cwd, env)

    def execute_install(
        self,
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        component_name: str = "",
    ) -> Tuple[ExecutionResult, Path]:
        """Execute make install command.

        Args:
            cwd: Working directory.
            env: Environment variables.
            component_name: Name of the component.

        Returns:
            Tuple of (ExecutionResult, log_file_path).
        """
        command = ["make", "install"]
        return self.execute_with_log(command, component_name, "install", cwd, env)
