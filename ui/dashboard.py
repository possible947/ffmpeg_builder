"""Live build dashboard for terminal progress."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Dict, List, Optional, Set

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ..components import Component
from ..state import ComponentStatus


@dataclass
class ComponentRow:
    """UI row for a build component."""
    index: int
    total: int
    name: str
    version: str
    status: ComponentStatus = ComponentStatus.PENDING
    detail: str = ""
    progress: float = 0.0
    first_seen: bool = False


_STATUS_LABELS = {
    ComponentStatus.PENDING: "pending",
    ComponentStatus.SYSTEM: "system",
    ComponentStatus.DOWNLOADING: "downloading",
    ComponentStatus.CONFIGURING: "config",
    ComponentStatus.BUILDING: "build",
    ComponentStatus.INSTALLING: "install",
    ComponentStatus.COMPLETED: "complete",
    ComponentStatus.FAILED: "fail",
    ComponentStatus.SKIPPED: "skip",
}

_STATUS_STYLES = {
    ComponentStatus.PENDING: "dim",
    ComponentStatus.SYSTEM: "cyan",
    ComponentStatus.DOWNLOADING: "cyan",
    ComponentStatus.CONFIGURING: "yellow",
    ComponentStatus.BUILDING: "magenta",
    ComponentStatus.INSTALLING: "blue",
    ComponentStatus.COMPLETED: "green",
    ComponentStatus.FAILED: "red",
    ComponentStatus.SKIPPED: "yellow dim",
}

_STEP_VALUES = {
    ComponentStatus.PENDING: 0,
    ComponentStatus.SYSTEM: 100,
    ComponentStatus.DOWNLOADING: 5,
    ComponentStatus.CONFIGURING: 35,
    ComponentStatus.BUILDING: 65,
    ComponentStatus.INSTALLING: 90,
    ComponentStatus.COMPLETED: 100,
    ComponentStatus.FAILED: 100,
    ComponentStatus.SKIPPED: 100,
}

_IN_PROGRESS = {
    ComponentStatus.DOWNLOADING,
    ComponentStatus.CONFIGURING,
    ComponentStatus.BUILDING,
    ComponentStatus.INSTALLING,
}


class BuildDashboard:
    """Rich renderable build dashboard."""

    def __init__(self, console: Console, ffmpeg_version: str, jobs: int, download_workers: int):
        self.console = console
        self.ffmpeg_version = ffmpeg_version
        self.jobs = jobs
        self.download_workers = download_workers
        self._rows_by_name: Dict[str, ComponentRow] = {}
        self.rows: List[ComponentRow] = []
        self.messages: Deque[str] = deque(maxlen=10)
        self.active_index = 0
        self.started_at = datetime.now()
        self._lock = threading.RLock()
        self._total = 0
        self._revealed: Set[str] = set()
        self._show_all_immediately = False

    def set_components(self, components: List[Component], reveal_all: bool = False) -> None:
        """Set planned components and create rows.

        Args:
            components: All buildable components in build order.
            reveal_all: When True, render all rows immediately. When False
                (default), rows only appear in the table after they have
                received at least one status update — so the table grows as
                downloads queue and builds start rather than starting fully
                populated.
        """
        with self._lock:
            self._show_all_immediately = reveal_all
            self._total = len(components)
            self._rows_by_name = {}
            self.rows = []
            self._revealed = set()
            for index, component in enumerate(components, 1):
                row = ComponentRow(index, self._total, component.name, component.version)
                self._rows_by_name[component.name] = row
                self.rows.append(row)
                if reveal_all:
                    self._revealed.add(component.name)
                    row.first_seen = True

    def reveal(self, name: str) -> None:
        """Force a row to be visible regardless of status updates."""
        with self._lock:
            if name in self._revealed:
                return
            self._revealed.add(name)
            row = self._rows_by_name.get(name)
            if row is not None:
                row.first_seen = True

    def _reveal_internal(self, name: str) -> None:
        with self._lock:
            if name in self._revealed:
                return
            self._revealed.add(name)
            row = self._rows_by_name.get(name)
            if row is not None:
                row.first_seen = True

    def update_status(
        self,
        name: str,
        status: ComponentStatus,
        version: Optional[str] = None,
        detail: str = "",
    ) -> None:
        """Update component status."""
        with self._lock:
            row = self._rows_by_name.get(name)
            if row is None:
                return
            if status == ComponentStatus.PENDING and row.status in (
                ComponentStatus.DOWNLOADING,
                ComponentStatus.CONFIGURING,
                ComponentStatus.BUILDING,
                ComponentStatus.INSTALLING,
            ):
                return
            row.status = status
            if version:
                row.version = version
            if status in (
                ComponentStatus.COMPLETED,
                ComponentStatus.FAILED,
                ComponentStatus.SKIPPED,
                ComponentStatus.SYSTEM,
            ):
                row.progress = 100.0
                row.detail = ""
            elif status == ComponentStatus.DOWNLOADING:
                row.progress = max(row.progress, _STEP_VALUES[ComponentStatus.DOWNLOADING])
            elif status == ComponentStatus.CONFIGURING:
                row.progress = max(row.progress, _STEP_VALUES[ComponentStatus.CONFIGURING])
            elif status == ComponentStatus.BUILDING:
                row.progress = max(row.progress, _STEP_VALUES[ComponentStatus.BUILDING])
            elif status == ComponentStatus.INSTALLING:
                row.progress = max(row.progress, _STEP_VALUES[ComponentStatus.INSTALLING])
            if detail:
                row.detail = detail
            try:
                self.active_index = self.rows.index(row)
            except ValueError:
                pass
            self._reveal_internal(name)

    def update_download_status(self, name: str, status: ComponentStatus) -> None:
        """Update a download status without overwriting later build phases."""
        with self._lock:
            row = self._rows_by_name.get(name)
            if row is None:
                return
            if status == ComponentStatus.PENDING and row.status in (
                ComponentStatus.DOWNLOADING,
                ComponentStatus.CONFIGURING,
                ComponentStatus.BUILDING,
                ComponentStatus.INSTALLING,
            ):
                return
            row.status = status
            if status != ComponentStatus.DOWNLOADING:
                row.progress = 0.0
            try:
                self.active_index = self.rows.index(row)
            except ValueError:
                pass
            self._reveal_internal(name)

    def update_download_progress(self, name: str, downloaded: int, total: int) -> None:
        """Update download progress for a component (bytes in/out of total)."""
        if total <= 0:
            return
        with self._lock:
            row = self._rows_by_name.get(name)
            if row is None:
                return
            row.status = ComponentStatus.DOWNLOADING
            pct = max(0.0, min(100.0, downloaded * 100.0 / total))
            row.progress = pct
            d_mb = downloaded / (1024 * 1024)
            t_mb = total / (1024 * 1024)
            if t_mb >= 1.0:
                row.detail = f"{d_mb:.1f}/{t_mb:.1f} MB ({pct:.0f}%)"
            else:
                row.detail = f"{downloaded}/{total} B ({pct:.0f}%)"
            self._reveal_internal(name)

    def log(self, message: str) -> None:
        """Append service message."""
        with self._lock:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.messages.append(f"[{timestamp}] {message}")

    def render(self) -> RenderableType:
        """Render current dashboard."""
        with self._lock:
            return Group(self._header(), self._table(), self._messages())

    def __rich__(self) -> RenderableType:
        return self.render()

    def _component_height(self) -> int:
        """Fixed number of component rows above the anchored messages panel.

        Layout overhead: header panel = 3 lines, messages panel = 10 lines
        (8 content + 2 borders).
        """
        return max(3, self.console.size.height - 13)

    def _header(self) -> Panel:
        """Compact header with title and elapsed time."""
        text = Text()
        text.append(f"FFmpeg Builder {self.ffmpeg_version} - Building", style="bold blue")
        elapsed_text = str(datetime.now() - self.started_at).split(".")[0]
        text.append(f" | Elapsed: {elapsed_text}", style="italic")
        return Panel(text, border_style="blue")

    def _table(self) -> Group:
        """Simplified table: one line per component with progress bar and status."""
        visible = self._visible_rows()
        height = self._component_height()
        rendered = []
        for row in visible:
            pct = int(row.progress)
            bar = '█' * (pct // 10) + '-' * ((100 - pct)//10)
            status_label = _STATUS_LABELS[row.status]
            txt = (f"{row.index}/{row.total} {row.name:20.20} "
                   f"{bar} {pct}% [{status_label}] | {row.detail}")
            rendered.append(Text(txt, style=_STATUS_STYLES[row.status]))
        # Pad so the component area always has the same height; messages panel stays anchored.
        for _ in range(height - len(rendered)):
            rendered.append(Text(""))
        return Group(*rendered)

    def _messages(self) -> Panel:
        """Fixed-height messages panel (8 content lines) anchored at the bottom."""
        lines = list(self.messages)[-8:]
        if not lines:
            lines = ["No messages yet."]
        while len(lines) < 8:
            lines.append("")
        content = "\n".join(lines)
        return Panel(Text(content), title="Messages", border_style="dim")

    def _visible_rows(self) -> List[ComponentRow]:
        with self._lock:
            if not self.rows:
                return []
            if self._show_all_immediately:
                visible = list(self.rows)
            else:
                visible = [r for r in self.rows if r.first_seen]
            if not visible:
                return []
            # Fixed layout: header = 3 lines, messages = 10 lines (8 content + 2 borders).
            # Component list fills the remaining space; messages stay anchored at the bottom.
            height = self._component_height()
            if len(visible) <= height:
                return visible

            anchor_idx = 0
            for i, row in enumerate(visible):
                if row.index - 1 == self.active_index:
                    anchor_idx = i
                    break
            else:
                if 0 <= self.active_index < len(self.rows):
                    anchor_idx = min(self.active_index, len(visible) - 1)
                else:
                    anchor_idx = len(visible) - 1

            half = height // 2
            start = max(0, anchor_idx - half)
            end = min(len(visible), start + height)
            start = max(0, end - height)
            return visible[start:end]
