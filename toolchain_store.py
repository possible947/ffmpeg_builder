"""Persistent toolchain store: layout, state, locking, and acquisition.

The store is independent from the build workspace (its root must not overlap the
workspace) and tracks installed profiles in its own state file. Acquisition
supports the four declared source variants: ``local``, ``download``, ``binary``,
and ``build`` (delegated to :mod:`ffmpeg_builder.toolchain_builders`).
"""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import tarfile
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Dict, Iterator, List, Optional

import yaml

from . import toolchain_builders
from .toolchain_registry import ToolchainProfile, ToolchainRegistry, ToolchainSource

STORE_SUBDIRS = ("archives", "installed", "logs", "locks", "profiles", "build")

Runner = toolchain_builders.Runner


def _now() -> str:
    """Return an ISO-8601 timestamp (seconds precision)."""
    return datetime.now().isoformat(timespec="seconds")


def _is_within(child: Path, parent: Path) -> bool:
    """Whether ``child`` is inside ``parent`` (both resolved)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_name(name: str) -> str:
    """Sanitize a profile name for use as a filesystem path component."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


def _sha256(path: Path) -> str:
    """Compute the lowercase SHA-256 of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class InstalledToolchain:
    """A toolchain profile installed in the store."""

    profile: str
    version: str
    variant: str
    path: str
    installed_at: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "profile": self.profile,
            "version": self.version,
            "variant": self.variant,
            "path": self.path,
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "InstalledToolchain":
        return cls(
            profile=str(data["profile"]),
            version=str(data.get("version", "")),
            variant=str(data.get("variant", "")),
            path=str(data.get("path", "")),
            installed_at=str(data.get("installed_at", "")),
        )


@dataclass
class ToolchainState:
    """Independent state for the toolchain store."""

    profiles: Dict[str, InstalledToolchain] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {"profiles": {name: item.to_dict() for name, item in self.profiles.items()}}

    @classmethod
    def from_dict(cls, data: object) -> "ToolchainState":
        if not isinstance(data, dict):
            return cls()
        raw = data.get("profiles") or {}
        profiles: Dict[str, InstalledToolchain] = {}
        if isinstance(raw, dict):
            for name, item in raw.items():
                if isinstance(item, dict):
                    profiles[str(name)] = InstalledToolchain.from_dict(item)
        return cls(profiles=profiles)


class ToolchainStore:
    """Persistent store for installed toolchain profiles."""

    def __init__(
        self,
        root: Path,
        *,
        workspace: Optional[Path] = None,
        source_archives_dir: Optional[Path] = None,
        allow_network_downloads: bool = False,
    ) -> None:
        self.root = Path(root)
        self.workspace = Path(workspace) if workspace is not None else None
        self.source_archives_dir = (
            Path(source_archives_dir) if source_archives_dir is not None else None
        )
        self.allow_network_downloads = allow_network_downloads
        self._validate_root()

    def _validate_root(self) -> None:
        if self.workspace is None:
            return
        if _is_within(self.root, self.workspace) or _is_within(self.workspace, self.root):
            raise ValueError(
                f"Toolchain store {self.root} must not overlap the build workspace "
                f"{self.workspace}"
            )

    # ------------------------------------------------------------------
    # Layout + state
    # ------------------------------------------------------------------

    @property
    def archives_dir(self) -> Path:
        return self.root / "archives"

    @property
    def installed_dir(self) -> Path:
        return self.root / "installed"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def ensure_layout(self) -> None:
        for sub in STORE_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def load_state(self) -> ToolchainState:
        if not self.state_path.exists():
            return ToolchainState()
        with open(self.state_path, "r", encoding="utf-8") as handle:
            return ToolchainState.from_dict(yaml.safe_load(handle))

    def save_state(self, state: ToolchainState) -> None:
        self.ensure_layout()
        with open(self.state_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(state.to_dict(), handle, default_flow_style=False, sort_keys=True)

    def installed(self, profile: str) -> Optional[InstalledToolchain]:
        return self.load_state().profiles.get(profile)

    def list_installed(self) -> List[InstalledToolchain]:
        return list(self.load_state().profiles.values())

    def component_env(
        self, registry: ToolchainRegistry, profile_name: str
    ) -> Optional[Dict[str, str]]:
        """Build per-component env overrides from an installed profile.

        Returns a subset of ``{"PATH_PREPEND", "CC", "CXX"}`` derived from the
        profile's ``materialize`` map, or ``None`` when the profile is not
        installed or declares no materialization.
        """
        installed = self.installed(profile_name)
        if installed is None:
            return None
        profile = registry.get_profile(profile_name)
        if profile is None:
            return None
        prefix = Path(installed.path)
        env: Dict[str, str] = {}
        bin_subdir = profile.materialize.get("bin_subdir")
        if bin_subdir:
            env["PATH_PREPEND"] = str(prefix / bin_subdir)
        cc = profile.materialize.get("cc")
        if cc:
            env["CC"] = str(prefix / cc)
        cxx = profile.materialize.get("cxx")
        if cxx:
            env["CXX"] = str(prefix / cxx)
        return env or None

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        """Exclusive, cross-process lock for a store operation."""
        self.ensure_layout()
        lock_path = self.locks_dir / f"{_safe_name(name)}.lock"
        handle = open(lock_path, "a+")
        try:
            _lock_handle(handle)
            yield
        finally:
            _unlock_handle(handle)
            handle.close()

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def acquire(
        self,
        registry: ToolchainRegistry,
        profile_name: str,
        variant: Optional[str] = None,
        runner: Optional[Runner] = None,
    ) -> InstalledToolchain:
        """Acquire (install) a toolchain profile into the store."""
        profile = registry.get_profile(profile_name)
        if profile is None:
            raise ValueError(f"Unknown toolchain profile {profile_name!r}")
        if not profile.deployable:
            raise ValueError(
                f"Toolchain profile {profile_name!r} has no acquisition source (not deployable)"
            )
        chosen = variant or profile.default_source
        if chosen is None or chosen not in profile.sources:
            raise ValueError(
                f"Toolchain profile {profile_name!r} has no variant {chosen!r} "
                f"(available: {', '.join(sorted(profile.sources)) or '<none>'})"
            )
        source = profile.sources[chosen]
        build_runner = runner or toolchain_builders.default_runner

        with self.lock(profile_name):
            version_suffix = profile.version or "unversioned"
            dest = self.installed_dir / f"{_safe_name(profile_name)}-{version_suffix}"
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)

            if source.type == "local":
                self._install_local(source, dest)
            elif source.type in ("binary", "download"):
                archive = self._fetch_or_resolve(source, profile)
                self._extract(archive, dest)
            elif source.type == "build":
                self._build(registry, profile, source, dest, build_runner)
            else:  # pragma: no cover - guarded by registry validation
                raise ValueError(f"Unsupported source type {source.type!r}")

            installed = InstalledToolchain(
                profile=profile_name,
                version=profile.version or "",
                variant=chosen,
                path=str(dest),
                installed_at=_now(),
            )
            state = self.load_state()
            state.profiles[profile_name] = installed
            self.save_state(state)
            return installed

    def remove(self, profile_name: str) -> bool:
        """Remove an installed profile from the store; returns whether it existed."""
        with self.lock(profile_name):
            state = self.load_state()
            entry = state.profiles.get(profile_name)
            if entry is None:
                return False
            path = Path(entry.path)
            if path.exists() and _is_within(path, self.installed_dir):
                shutil.rmtree(path)
            del state.profiles[profile_name]
            self.save_state(state)
            return True

    def clean_logs(self) -> None:
        """Remove all acquisition logs."""
        if self.logs_dir.exists():
            for item in self.logs_dir.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)

    # ------------------------------------------------------------------
    # Acquisition helpers
    # ------------------------------------------------------------------

    def _install_local(self, source: ToolchainSource, dest: Path) -> None:
        if not source.path:
            raise ValueError("Local source has no 'path'")
        origin = Path(source.path)
        if not origin.exists():
            raise FileNotFoundError(f"Local toolchain path not found: {origin}")
        if origin.is_dir():
            shutil.copytree(origin, dest, dirs_exist_ok=True)
        else:
            self._extract(origin, dest)

    def _fetch_or_resolve(self, source: ToolchainSource, profile: ToolchainProfile) -> Path:
        version = profile.version or ""
        filename = source.archive_filename or Path(source.url or "").name
        filename = filename.replace("{version}", version)
        if not filename:
            raise ValueError(f"Toolchain {profile.name!r} source has no archive filename or url")

        if self.source_archives_dir is not None:
            candidate = self.source_archives_dir / filename
            if candidate.exists():
                return candidate

        if not self.allow_network_downloads or not source.url:
            raise FileNotFoundError(
                f"Archive {filename!r} not found in the local sources "
                f"({self.source_archives_dir}); network downloads are disabled"
            )
        dest = self.archives_dir / filename
        self._download(source.url.replace("{version}", version), dest, source.sha256)
        return dest

    def _download(self, url: str, dest: Path, sha256: Optional[str]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url) as response, open(partial, "wb") as out:
            shutil.copyfileobj(response, out)
        if sha256 is not None and _sha256(partial) != sha256.lower():
            partial.unlink(missing_ok=True)
            raise ValueError(f"Checksum mismatch for {url}")
        os.replace(partial, dest)

    def _extract(self, archive: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(dest, filter="data")
        _flatten_single_top_dir(dest)

    def _build(
        self,
        registry: ToolchainRegistry,
        profile: ToolchainProfile,
        source: ToolchainSource,
        dest: Path,
        runner: Runner,
    ) -> None:
        archive = self._fetch_or_resolve(source, profile)
        work = self.build_dir / f"{_safe_name(profile.name)}-{profile.version or 'unversioned'}"
        if work.exists():
            shutil.rmtree(work)
        self._extract(archive, work)

        prerequisites: Dict[str, Path] = {}
        bootstrap = source.bootstrap
        if bootstrap is not None:
            for requirement in bootstrap.requires:
                installed = self.acquire(registry, requirement, runner=runner)
                prerequisites[requirement] = Path(installed.path)

        log_dir = self.logs_dir / _safe_name(profile.name)
        source_dir = _single_child_dir(work)
        toolchain_builders.run_build(
            source.recipe,
            source_dir,
            dest,
            log_dir,
            runner,
            source.configure_args,
            source.build_args,
            source.install_args,
            prerequisites,
        )


def _single_child_dir(path: Path) -> Path:
    """Return the sole child directory of ``path`` (typical extracted tarball)."""
    children = [item for item in path.iterdir() if item.is_dir()]
    if len(children) == 1 and not any(item.is_file() for item in path.iterdir()):
        return children[0]
    return path


def _flatten_single_top_dir(path: Path) -> None:
    """Move contents of a sole top-level directory up one level.

    Archives of deployable toolchains conventionally carry a single top-level
    directory (``<name>-<version>/...``). Binary acquisition installs the
    profile *contents* directly into ``installed/<profile>-<version>`` (so
    ``component_env()`` finds ``bin/`` there); without flattening the prefix
    would end up doubled (``installed/<profile>-<version>/<name>-<version>``).

    Multi-root archives (files or several dirs at the top level) are left
    untouched.
    """
    if not path.is_dir():
        return
    top = _single_child_dir(path)
    if top == path:
        return
    for child in list(top.iterdir()):
        target = path / child.name
        if target.exists():
            if target.is_dir() and child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)
        elif child.is_dir():
            shutil.move(str(child), str(target))
        else:
            shutil.move(str(child), str(target))
    shutil.rmtree(top, ignore_errors=True)


def _lock_handle(handle: IO[str]) -> None:
    """Acquire an exclusive OS lock on an open file handle (best effort)."""
    try:
        fcntl = importlib.import_module("fcntl")
    except ImportError:
        fcntl = None
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
        return
    msvcrt = importlib.import_module("msvcrt")  # pragma: no cover - Windows
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]


def _unlock_handle(handle: IO[str]) -> None:
    """Release a lock previously taken by :func:`_lock_handle` (best effort)."""
    try:
        fcntl = importlib.import_module("fcntl")
    except ImportError:
        fcntl = None
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        return
    msvcrt = importlib.import_module("msvcrt")  # pragma: no cover - Windows
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    except OSError:
        pass
