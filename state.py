"""State management for build process."""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum


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
                    "log_file": state.log_file
                }
                for name, state in self.components.items()
            },
            "current_step": self.current_step,
            "total_steps": self.total_steps
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BuildState":
        """Create from dictionary."""
        payload = dict(data)
        components_data = payload.pop("components", {})
        state = cls(**payload)
        
        for name, comp_data in components_data.items():
            status = ComponentStatus(comp_data["status"])
            if status in IN_PROGRESS_STATUSES:
                status = ComponentStatus.PENDING
            state.components[name] = ComponentState(
                status=status,
                version=comp_data.get("version"),
                built_at=comp_data.get("built_at"),
                error_message=comp_data.get("error_message"),
                log_file=comp_data.get("log_file")
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
        self.status_listener: Optional[
            Callable[
                [str, ComponentStatus, Optional[str], Optional[str], Optional[str]],
                None,
            ]
        ] = None
    
    def load(self) -> Optional[BuildState]:
        """Load state from file.
        
        Returns:
            BuildState instance or None if no state file exists.
        """
        if self.state_path.exists():
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.state = BuildState.from_dict(data)
            return self.state
        return None
    
    def save(self, state: Optional[BuildState] = None) -> None:
        """Save state to file.
        
        Args:
            state: State to save. If None, saves current state.
        """
        if state is not None:
            self.state = state
        
        if self.state is None:
            raise ValueError("No state to save")
        
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state.to_dict(), f, indent=2)
    
    def reset(self) -> None:
        """Reset state in memory and remove state file if it exists."""
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
        if self.status_listener is not None:
            self.status_listener(component_name, status, version, error_message, detail)
    
    def update_progress(self, current_step: int, total_steps: int) -> None:
        """Update build progress.
        
        Args:
            current_step: Current step number.
            total_steps: Total number of steps.
        """
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
            comp_state.status in (ComponentStatus.COMPLETED, ComponentStatus.SYSTEM) and
            comp_state.version == version
        )
