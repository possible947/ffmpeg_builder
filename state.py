"""State management for build process."""

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class ComponentStatus(str, Enum):
    """Component build status."""

    PENDING = "pending"
    SYSTEM = "system"
    DOWNLOADING = "downloading"
    CONFIGURING = "configuring"
    BUILDING = "building"
    INSTALLING = "installing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_STATUSES = {
    ComponentStatus.SYSTEM,
    ComponentStatus.COMPLETED,
    ComponentStatus.SKIPPED,
}


IN_PROGRESS_STATUSES = {
    ComponentStatus.DOWNLOADING,
    ComponentStatus.CONFIGURING,
    ComponentStatus.BUILDING,
    ComponentStatus.INSTALLING,
}


@dataclass
class ComponentState:
    """State of a single component."""

    status: ComponentStatus = ComponentStatus.PENDING
    version: Optional[str] = None
    built_at: Optional[str] = None
    error_message: Optional[str] = None
    log_file: Optional[str] = None


@dataclass
class BuildState:
    """Overall build state."""

    build_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    config: Dict[str, Any] = field(default_factory=dict)
    components: Dict[str, ComponentState] = field(default_factory=dict)
    current_step: int = 0
    total_steps: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "build_id": self.build_id,
            "started_at": self.started_at,
            "config": self.config,
            "components": {
                name: {
                    "status": state.status.value,
                    "version": state.version,
                    "built_at": state.built_at,
                    "error_message": state.error_message,
                    "log_file": state.log_file,
                }
                for name, state in self.components.items()
            },
            "current_step": self.current_step,
            "total_steps": self.total_steps,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BuildState":
        """Create from dictionary.

        Unknown top-level keys are ignored (forward compatibility with
        state files written by newer versions) and unknown or corrupt
        component status values are reset to PENDING so the component
        is rebuilt instead of crashing the app.
        """
        payload = dict(data)
        components_data = payload.pop("components", {})
        if not isinstance(components_data, dict):
            components_data = {}
        known_fields = {item.name for item in fields(cls)}
        state = cls(**{key: value for key, value in payload.items() if key in known_fields})

        for name, comp_data in components_data.items():
            if not isinstance(comp_data, dict):
                comp_data = {}
            try:
                status = ComponentStatus(comp_data.get("status", "pending"))
            except ValueError:
                status = ComponentStatus.PENDING
            if status in IN_PROGRESS_STATUSES:
                status = ComponentStatus.PENDING
            state.components[name] = ComponentState(
                status=status,
                version=comp_data.get("version"),
                built_at=comp_data.get("built_at"),
                error_message=comp_data.get("error_message"),
                log_file=comp_data.get("log_file"),
            )

        return state


class StateManager:
    """Manages build state."""

    def __init__(self, state_path: Optional[Path] = None):
        """Initialize state manager.

        Args:
            state_path: Path to state file. If None, uses default.
        """
        self.state_path = state_path or Path("workspace/build_state.json")
        self.state: Optional[BuildState] = None
        self._lock = threading.RLock()
        self.status_listener: Optional[
            Callable[
                [str, ComponentStatus, Optional[str], Optional[str], Optional[str]],
                None,
            ]
        ] = None

    def load(self) -> Optional[BuildState]:
        """Load state from file.

        Returns:
            BuildState instance, or None if no state file exists or the
            file is unreadable/corrupt (a corrupt file is logged and
            treated as "no previous build" instead of crashing the app,
            which would make the state file unrecoverable without manual
            deletion).
        """
        if not self.state_path.exists():
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.state = BuildState.from_dict(data)
            return self.state
        except (OSError, ValueError, TypeError) as e:
            # json.JSONDecodeError is a ValueError subclass.
            logging.getLogger(__name__).warning(
                "Ignoring unreadable build state file %s: %s", self.state_path, e
            )
            return None

    def save(self, state: Optional[BuildState] = None) -> None:
        """Save state to file.

        Args:
            state: State to save. If None, saves current state.
        """
        with self._lock:
            if state is not None:
                self.state = state

            if self.state is None:
                raise ValueError("No state to save")

            self.state_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: a crash mid-write must not leave a truncated
            # state file that the next launch cannot parse.
            tmp_path = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.state.to_dict(), f, indent=2)
                os.replace(tmp_path, self.state_path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise

    def reset(self) -> None:
        """Reset state in memory and remove state file if it exists."""
        with self._lock:
            self.state = None
            if self.state_path.exists():
                self.state_path.unlink()

    def get(self) -> BuildState:
        """Get current state.

        Returns:
            Current BuildState instance.
        """
        if self.state is None:
            loaded = self.load()
            if loaded is None:
                self.state = BuildState()
            else:
                self.state = loaded
        return self.state

    def mark_component_status(
        self,
        component_name: str,
        status: ComponentStatus,
        version: Optional[str] = None,
        error_message: Optional[str] = None,
        log_file: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        """Mark component with status.

        Args:
            component_name: Name of the component.
            status: New status.
            version: Component version.
            error_message: Error message if failed.
            log_file: Path to log file.
            detail: Optional transient detail (e.g. current command or
                download progress). Not persisted to the state file.
        """
        with self._lock:
            state = self.get()

            if component_name not in state.components:
                state.components[component_name] = ComponentState()

            comp_state = state.components[component_name]
            comp_state.status = status

            if version is not None:
                comp_state.version = version

            if status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM):
                comp_state.built_at = datetime.now().isoformat()
                comp_state.error_message = None
            elif status == ComponentStatus.FAILED:
                comp_state.error_message = error_message
            elif status != ComponentStatus.FAILED:
                comp_state.error_message = None

            if log_file is not None:
                comp_state.log_file = log_file

            self.save()

        # Fire listener outside the lock to avoid blocking other state access
        if self.status_listener is not None:
            self.status_listener(component_name, status, version, error_message, detail)

    def update_progress(self, current_step: int, total_steps: int) -> None:
        """Update build progress.

        Args:
            current_step: Current step number.
            total_steps: Total number of steps.
        """
        with self._lock:
            state = self.get()
            state.current_step = current_step
            state.total_steps = total_steps
            self.save()

    def get_resume_point(self) -> Optional[str]:
        """Get name of first incomplete component.

        Returns:
            Component name to resume from, or None if all completed.
        """
        state = self.get()

        for name, comp_state in state.components.items():
            if comp_state.status not in TERMINAL_STATUSES:
                return name

        return None

    def is_component_completed(self, component_name: str, version: str) -> bool:
        """Check if component is already completed with matching version.

        Args:
            component_name: Name of the component.
            version: Expected version.

        Returns:
            True if completed with matching version.
        """
        state = self.get()

        if component_name not in state.components:
            return False

        comp_state = state.components[component_name]
        return (
            comp_state.status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM)
            and comp_state.version == version
        )
