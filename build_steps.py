"""Shared build-step execution helpers."""

from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple

from .build_types import BuildError
from .components import Component
from .executor import CommandExecutor, ExecutionResult
from .state import ComponentStatus, StateManager


class BuildStepContext(Protocol):
    """Minimal builder surface required by shared build-step helpers."""

    executor: CommandExecutor
    state_manager: StateManager


def run_step(
    context: BuildStepContext,
    component: Component,
    status: ComponentStatus,
    detail: str,
    error_msg: str,
    command: list[str],
    step_name: str,
    work_dir: Path,
    env: Dict[str, str],
    stdin: Optional[str] = None,
) -> Tuple[ExecutionResult, Path]:
    """Mark status, execute a shell command, and raise on failure."""
    context.state_manager.mark_component_status(
        component.name, status, component.version, detail=detail
    )
    result, log_file = context.executor.execute_with_log(
        command, component.name, step_name, work_dir, env, stdin=stdin
    )
    if not result.success:
        raise BuildError(component.name, error_msg, log_file)
    return result, log_file


def run_make(
    context: BuildStepContext,
    component: Component,
    status: ComponentStatus,
    detail: str,
    error_msg: str,
    work_dir: Path,
    jobs: int,
    env: Dict[str, str],
    timeout: Optional[int] = None,
) -> Tuple[ExecutionResult, Path]:
    """Mark status, run make, and raise on failure."""
    context.state_manager.mark_component_status(
        component.name, status, component.version, detail=detail
    )
    result, log_file = context.executor.execute_make(
        work_dir, jobs, env, component.name, timeout=timeout
    )
    if not result.success:
        raise BuildError(component.name, error_msg, log_file)
    return result, log_file


def run_install(
    context: BuildStepContext,
    component: Component,
    status: ComponentStatus,
    detail: str,
    error_msg: str,
    work_dir: Path,
    env: Dict[str, str],
    timeout: Optional[int] = None,
) -> Tuple[ExecutionResult, Path]:
    """Mark status, run make install, and raise on failure."""
    context.state_manager.mark_component_status(
        component.name, status, component.version, detail=detail
    )
    result, log_file = context.executor.execute_install(
        work_dir, env, component.name, timeout=timeout
    )
    if not result.success:
        raise BuildError(component.name, error_msg, log_file)
    return result, log_file
