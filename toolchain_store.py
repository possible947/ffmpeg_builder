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
from typing import IO, Callable, Dict, Iterator, List, Optional

import yaml

from . import toolchain_builders
from .toolchain_registry import ToolchainProfile, ToolchainRegistry, ToolchainSource

STORE_SUBDIRS = ("archives", "installed", "logs", "locks", "profiles", "build")

Runner = toolchain_builders.Runner
# (recipe, prefix) -> list of failure descriptions; empty means usable.
Verifier = Callable[[Optional[str], Path], List[str]]


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
    verified: bool = True

    def to_dict(self) -> Dict[str, object]:
        return {
            "profile": self.profile,
            "version": self.version,
            "variant": self.variant,
            "path": self.path,
            "installed_at": self.installed_at,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "InstalledToolchain":
        return cls(
            profile=str(data["profile"]),
            version=str(data.get("version", "")),
            variant=str(data.get("variant", "")),
            path=str(data.get("path", "")),
            installed_at=str(data.get("installed_at", "")),
            # Entries written before verification existed default to trusted.
            verified=bool(data.get("verified", True)),
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
        entry = self.load_state().profiles.get(profile)
        if entry is None or entry.verified:
            return entry
        # A failed install was recorded as unverified: treat as absent and warn.
        return None

    def resolve_entry_path(self, entry: InstalledToolchain) -> Path:
        """Resolve an entry's install prefix against this store's root.

        The recorded absolute path wins when it exists. Otherwise the same
        directory basename is tried under this store's ``installed/`` — this is
        what makes a copied store work at a new location (relocation). When
        neither exists, the recorded path is returned so verification fails
        with an honest "missing" report.
        """
        recorded = Path(entry.path)
        if recorded.exists():
            return recorded
        relocated = self.installed_dir / recorded.name
        if relocated.exists():
            return relocated
        return recorded

    def mark_unverified(self, profile: str) -> None:
        """Record an installed profile as failed verification.

        Keeps the state entry (so the broken path is visible) but withholds it
        from :meth:`installed`, :meth:`profile_paths`, and :meth:`component_env`
        until the profile is re-acquired.
        """
        state = self.load_state()
        entry = state.profiles.get(profile)
        if entry is not None and entry.verified:
            entry.verified = False
            self.save_state(state)

    def reverify(self, registry: ToolchainRegistry, adopt_relocated: bool = False) -> List[str]:
        """Re-run verification for every installed profile.

        Prefixes are resolved with :meth:`resolve_entry_path`, so this is also
        the store-relocation acceptance check: copy the store, run this against
        the copy, and failures show exactly which profiles did not survive the
        move. Failed profiles are marked unverified. When ``adopt_relocated``
        is set, successfully re-anchored entries have their recorded path
        rewritten to the new root so the moved store becomes authoritative.
        Returns the names of the failed profiles.
        """
        failures: List[str] = []
        state = self.load_state()
        changed = False
        for name, entry in state.profiles.items():
            prefix = self.resolve_entry_path(entry)
            recipe: Optional[str] = None
            profile = registry.get_profile(name)
            if profile is not None and profile.default_source is not None:
                source = profile.sources.get(profile.default_source)
                recipe = source.recipe if source is not None else None
            if toolchain_builders.verify_profile(recipe, prefix):
                entry.verified = False
                changed = True
                failures.append(name)
            elif adopt_relocated and str(prefix) != entry.path and prefix.exists():
                entry.path = str(prefix)
                changed = True
        if changed:
            self.save_state(state)
        return failures

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
        paths = self.profile_paths(registry, profile_name)
        if not paths:
            return None
        env: Dict[str, str] = {}
        bin_dir = paths.get("BIN_DIR")
        if bin_dir:
            env["PATH_PREPEND"] = bin_dir
        if "CC" in paths:
            env["CC"] = paths["CC"]
        if "CXX" in paths:
            env["CXX"] = paths["CXX"]
        return env or None

    def profile_paths(self, registry: ToolchainRegistry, profile_name: str) -> Dict[str, str]:
        """Resolve absolute tool paths (CC/CXX/bin dir) for an installed profile.

        Returns a mapping with any of ``CC``, ``CXX``, ``BIN_DIR`` keys, or an
        empty dict when the profile is not installed, failed verification, or
        declares no materialization. Used by the CUDA host-compiler selection
        (``-ccbin``) as well as :meth:`component_env`.
        """
        installed = self.installed(profile_name)
        if installed is None or not installed.verified:
            return {}
        profile = registry.get_profile(profile_name)
        if profile is None:
            return {}
        prefix = Path(installed.path)
        paths: Dict[str, str] = {}
        bin_subdir = profile.materialize.get("bin_subdir")
        if bin_subdir:
            paths["BIN_DIR"] = str(prefix / bin_subdir)
        cc = profile.materialize.get("cc")
        if cc:
            paths["CC"] = str(prefix / cc)
        cxx = profile.materialize.get("cxx")
        if cxx:
            paths["CXX"] = str(prefix / cxx)
        return paths

    def cuda_host_compiler(
        self, registry: ToolchainRegistry, cuda_version: Optional[str]
    ) -> Optional[str]:
        """Resolve the CUDA host compiler from the installed toolchain store.

        The registry's ``cuda`` table maps the detected CUDA Toolkit version
        (``"major.minor"``) to the required host-compiler profile; when that
        profile is installed, its ``CC`` path is returned for ``nvcc -ccbin``.
        Returns ``None`` when the version is unknown, unmapped, or the mapped
        profile is not installed. There is no automatic substitution: an
        absent mapping is a skip/error, never a guess.
        """
        if not cuda_version:
            return None
        profile_name = registry.toolchain_for_cuda(cuda_version)
        if profile_name is None:
            return None
        paths = self.profile_paths(registry, profile_name)
        cc = paths.get("CC")
        if cc and Path(cc).exists():
            return cc
        return None

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
        jobs: Optional[int] = None,
        verifier: Optional[Verifier] = None,
    ) -> InstalledToolchain:
        """Acquire (install) a toolchain profile into the store.

        Source builds run with ``jobs`` parallel make jobs (defaulting to the
        detected CPU count) and are verified after installation with ``verifier``
        (defaults to :func:`toolchain_builders.verify_profile`). A failed
        verification raises instead of recording a broken profile as installed.
        """
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
        build_jobs = jobs if jobs is not None else max(1, os.cpu_count() or 1)
        check = verifier or toolchain_builders.verify_profile

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
                self._build(registry, profile, source, dest, build_runner, build_jobs, check)
                failures = check(source.recipe, dest)
                if failures:
                    raise RuntimeError(
                        f"Toolchain {profile_name!r} verification failed: " + "; ".join(failures)
                    )
            else:  # pragma: no cover - guarded by registry validation
                raise ValueError(f"Unsupported source type {source.type!r}")

            installed = InstalledToolchain(
                profile=profile_name,
                version=profile.version or "",
                variant=chosen,
                path=str(dest),
                installed_at=_now(),
                verified=True,
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
        jobs: int,
        verifier: Verifier,
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
                installed = self.acquire(
                    registry, requirement, runner=runner, jobs=jobs, verifier=verifier
                )
                prerequisites[requirement] = Path(installed.path)

        log_dir = self.logs_dir / _safe_name(profile.name)
        source_dir = _single_child_dir(work)
        toolchain_builders.run_build(
            source.recipe,
            source_dir,
            dest,
            log_dir,
            runner,
            toolchain_builders.resolve_args(source.configure_args, dest, prerequisites, jobs),
            toolchain_builders.resolve_args(source.build_args, dest, prerequisites, jobs),
            toolchain_builders.resolve_args(source.install_args, dest, prerequisites, jobs),
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
