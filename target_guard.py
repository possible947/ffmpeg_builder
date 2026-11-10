"""Identity guard: decide whether saved state and artifacts may be reused.

The guard runs *before* anything trusts a saved ``completed``/``system``
status, selects a resume point, reuses a mutable build tree or target prefix,
or overwrites the saved configuration. It is shared by the TUI
(``app.FFmpegBuilderApp``) and the headless driver (``scripts/run_build.py``).

Isolation model for the WSL-Mingw route: every distinct *build* identity owns
its own workspace directory ``<base workspace>/targets/<backend>-<key>`` that
contains the prefix, ``packages`` build trees, logs and ``build_state.json``.
A material change therefore resolves to a different directory and cannot touch
the old artifacts, which are preserved until an explicit cleanup. A marker
file (``workspace_identity.json``) written when the directory is created lets
us recognise our own directory even after a state reset, and refuse a
directory whose content has no trusted identity.

Native backends keep their historical workspace layout and semantics; they get
a minimal identity (backend/architecture/FFmpeg version) and a lenient guard.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .build_identity import (
    BACKEND_CROSS_MINGW,
    Compatibility,
    IdentityComparison,
    IdentityLoadStatus,
    TargetIdentity,
    build_windows_identity,
    compare_identities,
    compare_saved,
    parse_identity,
    runtime_identity,
    diagnostic_snapshot,
)
from .build_types import BuildError
from .state import BuildState, StateManager
from .windows_probe import WindowsInterop, WindowsTargetState, probe_windows_target

TARGETS_DIRNAME = "targets"
MARKER_NAME = "workspace_identity.json"
# Files the guard itself may leave in an otherwise empty identity directory.
_GUARD_FILES = {MARKER_NAME}


class IdentityError(BuildError):
    """Reuse of saved state/artifacts is unsafe (or identity cannot be established)."""

    def __init__(
        self,
        kind: str,
        message: str,
        comparison: Optional[IdentityComparison] = None,
        path: Optional[Path] = None,
    ):
        text = f"[{kind}] {message}"
        if comparison is not None and comparison.differences:
            text += "\n" + comparison.format()
        super().__init__("build-identity", text)
        self.kind = kind
        self.detail = message
        self.comparison = comparison
        self.path = path


@dataclass
class TargetSession:
    """Outcome of a successful guard run."""

    backend: str
    workspace: Path
    identity: TargetIdentity
    comparison: Optional[IdentityComparison] = None
    resumed: bool = False
    acceptance_invalidated: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def uses_isolated_workspace(self) -> bool:
        return self.backend == BACKEND_CROSS_MINGW

    def stamp(self, state_manager: StateManager) -> None:
        """Persist the verified identity into the (possibly freshly reset) state."""
        state = state_manager.get()
        state.identity = self.identity.to_dict()
        if self.acceptance_invalidated and self.comparison is not None:
            state.acceptance = {
                "status": "invalidated",
                "reason": "runtime identity changed since the build started",
                "changed": [d.path for d in self.comparison.affecting(*_runtime_category())],
            }
        elif state.acceptance is None or not self.resumed:
            state.acceptance = {"status": "none"}
        state_manager.save()


def _runtime_category() -> tuple:
    from .build_identity import Category

    return (Category.RUNTIME,)


# --------------------------------------------------------------------------
# Windows target (linux-cross-mingw)
# --------------------------------------------------------------------------


def identity_workspace(base_workspace: Path, identity: TargetIdentity) -> Path:
    """Directory owned by one build identity (same identity -> same path)."""
    return base_workspace / TARGETS_DIRNAME / f"{identity.backend}-{identity.build_key}"


def probe_current_identity(
    config_dict: Mapping[str, Any],
    override: Optional[Mapping[str, Any]],
    *,
    components: Optional[Mapping[str, str]] = None,
    toolchains: Optional[Mapping[str, str]] = None,
    host: Optional[Mapping[str, Any]] = None,
    interop: Optional[WindowsInterop] = None,
    probe: Callable[..., WindowsTargetState] = probe_windows_target,
) -> TargetIdentity:
    """Freshly probe (or take the explicit override) and build the current identity."""
    if interop is not None:
        state = probe(override, interop=interop)
    else:
        state = probe(override)
    return build_windows_identity(config_dict, state, components, toolchains, host)


def _write_marker(path: Path, identity: TargetIdentity) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(identity.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_marker(path: Path) -> Any:
    """Return parsed JSON, raising ValueError/OSError on unreadable content."""
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_notes(base_workspace: Path) -> List[str]:
    notes: List[str] = []
    legacy_state = base_workspace / "build_state.json"
    if legacy_state.exists():
        inspection = StateManager(legacy_state).inspect()
        what = "unreadable" if inspection.kind == "corrupt" else "legacy (no identity)"
        notes.append(
            f"Existing {what} state at {legacy_state} is preserved and NOT used for the "
            f"WSL-Mingw route; this build runs in an isolated identity workspace. "
            "Use Cleanup to remove old workspaces explicitly."
        )
    return notes


def prepare_windows_target(
    base_workspace: Path, current: TargetIdentity, resume: bool
) -> TargetSession:
    """Guard the WSL-Mingw route and return the identity workspace to use.

    Raises :class:`IdentityError` (before touching any saved state) when reuse
    would be unsafe.
    """
    workspace = identity_workspace(base_workspace, current)
    marker = workspace / MARKER_NAME
    notes = _legacy_notes(base_workspace)

    if workspace.exists():
        stale = [p for p in workspace.iterdir() if p.name not in _GUARD_FILES]
        if not marker.exists():
            if stale:
                raise IdentityError(
                    "unsafe_stale_artifacts",
                    f"{workspace} contains files but no trusted identity marker "
                    f"({MARKER_NAME}); they cannot be shown to match this build and are "
                    "left untouched. Remove the directory (Cleanup) or choose another workspace.",
                    path=workspace,
                )
            _write_marker(marker, current)
        else:
            _verify_marker(marker, current, workspace)
    else:
        _write_marker(marker, current)

    manager = StateManager(workspace / "build_state.json")
    inspection = manager.inspect()
    comparison: Optional[IdentityComparison] = None
    invalidated = False
    resumed = False

    if inspection.kind == "corrupt":
        raise IdentityError(
            "corrupt_state",
            f"{manager.state_path} is unreadable ({inspection.error}); it is not treated as a "
            "fresh state and has been left untouched. Remove it explicitly (Cleanup) to start over.",
            path=manager.state_path,
        )
    if inspection.kind == "ok":
        assert inspection.state is not None
        comparison = compare_saved(inspection.state.identity, current)
        loaded = parse_identity(inspection.state.identity)
        if comparison.result == Compatibility.UNKNOWN:
            raise IdentityError(
                _unknown_kind(loaded.status),
                f"saved state in {workspace} has no trustworthy identity ({loaded.reason}); "
                "statuses (completed/system) and artifacts are not reused and nothing was changed. "
                "Start over with Cleanup or a different workspace.",
                comparison,
                workspace,
            )
        if comparison.result == Compatibility.BUILD_INCOMPATIBLE:
            raise IdentityError(
                "build_mismatch",
                f"saved state in {workspace} was produced for a different build identity; "
                "its statuses, prefix and build trees are not reused. Nothing was modified.",
                comparison,
                workspace,
            )
        if resume:
            resumed = True
            invalidated = comparison.result == Compatibility.RUNTIME_CHANGED
            if invalidated:
                notes.append(
                    "Windows runtime state changed since this build started; compiled artifacts "
                    "are reused but earlier runtime acceptance is invalidated and must be re-run."
                )
    elif resume:
        notes.append("No saved state for this identity; starting a fresh build.")

    return TargetSession(
        backend=BACKEND_CROSS_MINGW,
        workspace=workspace,
        identity=current,
        comparison=comparison,
        resumed=resumed,
        acceptance_invalidated=invalidated,
        notes=notes,
    )


def _unknown_kind(status: IdentityLoadStatus) -> str:
    return {
        IdentityLoadStatus.LEGACY: "legacy_state",
        IdentityLoadStatus.CORRUPT: "corrupt_identity",
        IdentityLoadStatus.UNSUPPORTED_SCHEMA: "unsupported_identity_schema",
    }.get(status, "unknown_identity")


def _verify_marker(marker: Path, current: TargetIdentity, workspace: Path) -> None:
    try:
        raw = _read_marker(marker)
    except (OSError, ValueError) as exc:
        raise IdentityError(
            "corrupt_identity",
            f"{marker} is unreadable ({exc}); the directory is not trusted.",
            path=workspace,
        ) from exc
    comparison = compare_saved(raw, current, build_only=True)
    if comparison.result == Compatibility.UNKNOWN:
        raise IdentityError(
            "corrupt_identity",
            f"{marker} holds no valid identity; the directory is not trusted.",
            comparison,
            workspace,
        )
    if comparison.result == Compatibility.BUILD_INCOMPATIBLE:
        raise IdentityError(
            "build_mismatch",
            f"{workspace} belongs to a different build identity than its name implies.",
            comparison,
            workspace,
        )


def mark_state_verified(state_manager: StateManager) -> None:
    """Allow the state manager to trust saved statuses after the guard passed."""
    state_manager.identity_verified = True


# --------------------------------------------------------------------------
# Native backends (lenient)
# --------------------------------------------------------------------------


def prepare_native_target(
    base_workspace: Path,
    state_manager: StateManager,
    current: TargetIdentity,
    resume: bool,
) -> TargetSession:
    """Guard native builds without changing their historical behaviour.

    * New build: unchanged (state is reset by the caller).
    * Resume, legacy state (no identity): adopted - the identity is stamped on
      the next save. Native flows never required one.
    * Resume, identity recorded: must be intact and match backend /
      architecture / FFmpeg version, otherwise resume is refused.
    """
    notes: List[str] = []
    comparison: Optional[IdentityComparison] = None
    resumed = False
    if resume:
        inspection = state_manager.inspect()
        if inspection.kind == "ok":
            assert inspection.state is not None
            raw = inspection.state.identity
            if raw is not None:
                comparison = compare_saved(raw, current)
                if comparison.result == Compatibility.UNKNOWN:
                    loaded = parse_identity(raw)
                    raise IdentityError(
                        _unknown_kind(loaded.status),
                        f"saved identity in {state_manager.state_path} is not trustworthy "
                        f"({loaded.reason}); refusing to resume.",
                        comparison,
                    )
                if comparison.result == Compatibility.BUILD_INCOMPATIBLE:
                    raise IdentityError(
                        "build_mismatch",
                        f"saved state in {state_manager.state_path} was produced for a different "
                        "backend/architecture/FFmpeg version; refusing to resume.",
                        comparison,
                    )
                resumed = True
            else:
                notes.append("Adopting legacy native state without identity.")
                resumed = True
        elif inspection.kind == "corrupt":
            notes.append(f"Saved state is unreadable ({inspection.error}); starting fresh.")
    return TargetSession(
        backend=current.backend,
        workspace=base_workspace,
        identity=current,
        comparison=comparison,
        resumed=resumed,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Runtime drift re-check (before final runtime acceptance)
# --------------------------------------------------------------------------


@dataclass
class DriftResult:
    comparison: IdentityComparison
    acceptance_valid: bool
    build_unsafe: bool

    @property
    def message(self) -> str:
        if self.build_unsafe:
            return (
                "Windows target state now differs from the state these binaries were built for; "
                "a new build is required.\n" + self.comparison.format()
            )
        if not self.acceptance_valid:
            return (
                "Windows runtime state changed since the build started; earlier runtime "
                "acceptance is invalid and must be re-run.\n" + self.comparison.format()
            )
        return "Windows runtime state matches the build-start identity."


def recheck_runtime_identity(
    baseline: TargetIdentity,
    current: TargetIdentity,
) -> DriftResult:
    """Compare a *fresh* identity with the build-start baseline.

    Callers must obtain ``current`` from a new probe (``probe_current_identity``);
    a saved snapshot is never a valid substitute. Only compatibility is
    evaluated here; invalidation is applied via :func:`invalidate_acceptance`.
    """
    comparison = compare_identities(baseline, current)
    return DriftResult(
        comparison=comparison,
        acceptance_valid=comparison.acceptance_valid,
        build_unsafe=not comparison.build_compatible,
    )


def invalidate_acceptance(state_manager: StateManager, reason: str, changed: List[str]) -> None:
    """Record that prior runtime acceptance can no longer be relied upon."""
    state = state_manager.get()
    state.acceptance = {"status": "invalidated", "reason": reason, "changed": list(changed)}
    state_manager.save()


def baseline_from_state(state: BuildState) -> Optional[TargetIdentity]:
    """Build-start identity recorded in ``state`` (None when absent/untrusted)."""
    return parse_identity(state.identity).identity
