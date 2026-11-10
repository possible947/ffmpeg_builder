"""Persistent toolchain store: layout, state, locking, and acquisition.

The store is independent from the build workspace (its root must not overlap the
workspace) and tracks installed profiles in its own state file. Acquisition
supports the four declared source variants: ``local``, ``download``, ``binary``,
and ``build`` (delegated to :mod:`ffmpeg_builder.toolchain_builders`).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import tarfile
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import yaml

from . import toolchain_builders
from .build_types import CATEGORY_PROJECT_SOURCE, CATEGORY_TOOLCHAIN_INSTALL, BuildError
from .toolchain_registry import (
    ToolchainExtra,
    ToolchainProfile,
    ToolchainRegistry,
    ToolchainSource,
)

STORE_ROOT_ENV = "FFMPEG_BUILDER_TOOLCHAINS"
# Installed-state schema: 2 adds registry/source provenance to each entry.
STATE_SCHEMA_VERSION = 2
PROVENANCE_FILE = ".toolchain-install.json"
# Build recipes whose result is relocatable, so they are built in the staging
# directory and activated atomically (others bake the prefix and build in place).
STAGED_BUILD_RECIPES = frozenset({"rust_standalone"})

# Installation compatibility statuses (see ToolchainStore.installed_status).
STATUS_ABSENT = "absent"
STATUS_COMPATIBLE = "compatible"
STATUS_UNVERIFIED = "unverified"
STATUS_STALE = "stale"
STATUS_CORRUPT = "corrupt"


def resolve_store_root(project_root: Path, explicit: Optional[Path] = None) -> Path:
    """Single precedence rule for the toolchain store root.

    1. an explicit path (CLI ``--store``);
    2. the ``FFMPEG_BUILDER_TOOLCHAINS`` environment variable;
    3. ``<project_root>/toolchains``.
    """
    if explicit is not None:
        return Path(explicit)
    override = os.environ.get(STORE_ROOT_ENV)
    if override:
        return Path(override)
    return project_root / "toolchains"


def _source_error(
    profile: str, message: str, code: str, cause: Optional[BaseException] = None
) -> BuildError:
    error = BuildError(profile, message, category=CATEGORY_PROJECT_SOURCE, code=code)
    error.__cause__ = cause
    return error


def _install_error(
    profile: str, message: str, code: str, cause: Optional[BaseException] = None
) -> BuildError:
    error = BuildError(profile, message, category=CATEGORY_TOOLCHAIN_INSTALL, code=code)
    error.__cause__ = cause
    return error


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


def _archive_filename(source: ToolchainSource, profile: ToolchainProfile) -> str:
    """Resolved archive filename of a source (``{version}`` expanded)."""
    filename = source.archive_filename or Path(source.url or "").name
    return filename.replace("{version}", profile.version or "")


@dataclass
class InstalledToolchain:
    """A toolchain profile installed in the store."""

    profile: str
    version: str
    variant: str
    path: str
    installed_at: str
    verified: bool = True
    # Provenance (schema 2): what the registry declared when this was installed.
    schema_version: int = 1
    source_sha256: str = ""
    archive_filename: str = ""
    runtime: str = ""
    # Additive artifacts (e.g. rust-std targets) installed into this prefix.
    extras: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "profile": self.profile,
            "version": self.version,
            "variant": self.variant,
            "path": self.path,
            "installed_at": self.installed_at,
            "verified": self.verified,
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "archive_filename": self.archive_filename,
            "runtime": self.runtime,
            "extras": [dict(item) for item in self.extras],
        }

    def provenance(self) -> Dict[str, object]:
        """Project-managed inputs identifying this installation (no timestamps)."""
        return {
            "profile": self.profile,
            "version": self.version,
            "variant": self.variant,
            "archive_filename": self.archive_filename,
            "source_sha256": self.source_sha256,
            "runtime": self.runtime,
            "schema_version": self.schema_version,
            "extras": sorted(self.extras, key=lambda item: (item["kind"], item["target"])),
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
            schema_version=int(data.get("schema_version", 1) or 1),  # type: ignore[call-overload]
            source_sha256=str(data.get("source_sha256", "")),
            archive_filename=str(data.get("archive_filename", "")),
            runtime=str(data.get("runtime", "")),
            extras=_load_extras(data.get("extras")),
        )


def _extra_record(extra: ToolchainExtra) -> Dict[str, str]:
    """Provenance of one additive artifact (what the registry declared)."""
    return {
        "kind": extra.kind,
        "target": extra.target,
        "component": extra.component,
        "release": extra.release,
        "archive_filename": extra.archive_filename,
        "sha256": extra.sha256.lower(),
    }


def _load_extras(raw: object) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    return [
        {str(key): str(value) for key, value in item.items()}
        for item in raw
        if isinstance(item, dict) and "kind" in item and "target" in item
    ]


def _extra_subject(profile: ToolchainProfile, extra: Optional[ToolchainExtra]) -> str:
    """Human-readable subject for diagnostics about a base or additive archive."""
    base = f"{profile.name} {profile.version or ''}".strip()
    if extra is None:
        return base
    return f"{base} {extra.kind} ({extra.component}, target {extra.target})"


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
        backend: Optional[str] = None,
    ) -> None:
        self.root = Path(root)
        self.workspace = Path(workspace) if workspace is not None else None
        self.source_archives_dir = (
            Path(source_archives_dir) if source_archives_dir is not None else None
        )
        self.allow_network_downloads = allow_network_downloads
        # Target build backend; selects which additive artifacts are required.
        self.backend = backend
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
        partial = self.state_path.with_name(self.state_path.name + ".tmp")
        with open(partial, "w", encoding="utf-8") as handle:
            yaml.safe_dump(state.to_dict(), handle, default_flow_style=False, sort_keys=True)
        os.replace(partial, self.state_path)

    def installed(
        self, profile: str, registry: Optional[ToolchainRegistry] = None
    ) -> Optional[InstalledToolchain]:
        """Return the verified entry for ``profile``, or ``None``.

        Without ``registry`` only the verified flag is considered (legacy
        behaviour). With a ``registry`` the entry must also be
        :data:`STATUS_COMPATIBLE`; stale or corrupt installs are not current.
        """
        entry = self.load_state().profiles.get(profile)
        if entry is None or not entry.verified:
            return None
        if (
            registry is not None
            and self.installed_status(registry, profile)[0] != STATUS_COMPATIBLE
        ):
            return None
        return entry

    def installed_status(self, registry: ToolchainRegistry, profile_name: str) -> Tuple[str, str]:
        """Classify an installation against the current registry.

        Returns ``(status, reason)`` where status is one of ``absent``,
        ``compatible``, ``unverified``, ``stale`` (registry version, source
        hash, variant, runtime or state schema no longer match; reacquire) and
        ``corrupt`` (state says installed but the directory is missing).
        Nothing is deleted or adopted automatically.
        """
        entry = self.load_state().profiles.get(profile_name)
        if entry is None:
            return STATUS_ABSENT, "not installed"
        if not entry.verified:
            return STATUS_UNVERIFIED, "recorded as failed verification"
        if not self.resolve_entry_path(entry).is_dir():
            return STATUS_CORRUPT, f"state lists {entry.path} but the directory is missing"
        profile = registry.get_profile(profile_name)
        if profile is None:
            return STATUS_STALE, "profile is no longer declared in the registry"
        if entry.schema_version < STATE_SCHEMA_VERSION:
            return STATUS_STALE, "installed state has no provenance (reacquire or --verify-only)"
        if entry.version != (profile.version or ""):
            return STATUS_STALE, (
                f"registry version {profile.version or ''!r} != installed {entry.version!r}"
            )
        source = profile.sources.get(entry.variant)
        if source is None:
            return STATUS_STALE, f"source variant {entry.variant!r} is no longer declared"
        if (source.sha256 or "").lower() != entry.source_sha256.lower():
            return STATUS_STALE, "declared source sha256 changed"
        runtime = profile.target.runtime if profile.target is not None else ""
        if runtime != entry.runtime:
            return STATUS_STALE, f"runtime variant {runtime!r} != installed {entry.runtime!r}"
        extras_problem = self._extras_problem(profile, entry)
        if extras_problem:
            return STATUS_STALE, extras_problem
        return STATUS_COMPATIBLE, "matches the registry"

    def _extras_problem(
        self, profile: ToolchainProfile, entry: InstalledToolchain
    ) -> Optional[str]:
        """Why the recorded additive artifacts do not satisfy the registry, if so.

        Every artifact the current backend requires must be recorded with the
        declared release/component/archive/hash; recorded artifacts must still
        be declared and unchanged. Order is irrelevant (keyed by kind+target).
        """
        declared = {item.key: item for item in profile.extras}
        recorded = {(item.get("kind", ""), item.get("target", "")): item for item in entry.extras}
        for extra in profile.required_extras(self.backend):
            if extra.key not in recorded:
                return (
                    f"required {extra.kind} for {extra.target} is not installed "
                    "(reacquire to stage a complete toolchain)"
                )
        for key, record in recorded.items():
            decl = declared.get(key)
            if decl is None:
                return f"installed {key[0]} for {key[1]} is no longer declared"
            expected = _extra_record(decl)
            if record != expected:
                changed = sorted(
                    name for name, value in expected.items() if record.get(name) != value
                )
                return f"{key[0]} for {key[1]} changed in the registry ({', '.join(changed)})"
        return None

    def describe(self, registry: ToolchainRegistry, profile_name: str) -> Optional[Dict[str, Any]]:
        """Resolved metadata of a current installation (for later consumers).

        Returns ``None`` unless the profile is installed and compatible. The
        mapping carries the profile/version, the installed prefix, the base
        source SHA-256 and every installed additive artifact (kind, target,
        archive SHA-256), plus the verification state.
        """
        entry = self.installed(profile_name, registry)
        if entry is None:
            return None
        return {
            "profile": entry.profile,
            "version": entry.version,
            "prefix": str(self.resolve_entry_path(entry)),
            "base_sha256": entry.source_sha256,
            "targets": sorted(item["target"] for item in entry.extras),
            "extras": [dict(item) for item in entry.extras],
            "verified": entry.verified,
        }

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
            profile = registry.get_profile(name)
            source = None
            if profile is not None:
                source = profile.sources.get(entry.variant)
                if source is None and profile.default_source is not None:
                    source = profile.sources.get(profile.default_source)
            recorded = self._recorded_extras(profile, entry)
            if toolchain_builders.verify_installation(profile, source, prefix, recorded):
                entry.verified = False
                changed = True
                failures.append(name)
                continue
            if profile is not None and self._missing_required_extras(profile, entry):
                # Healthy but incomplete for this backend: reported, not marked broken.
                failures.append(name)
            if adopt_relocated and str(prefix) != entry.path and prefix.exists():
                entry.path = str(prefix)
                changed = True
            if (
                profile is not None
                and source is not None
                and entry.schema_version < STATE_SCHEMA_VERSION
                and entry.version == (profile.version or "")
            ):
                # Backfill provenance for a legacy entry that matches the registry.
                # No additive artifact is claimed: it cannot be vouched for.
                self._stamp_provenance(entry, profile, source)
                changed = True
        if changed:
            self.save_state(state)
        return failures

    @staticmethod
    def _recorded_extras(
        profile: Optional[ToolchainProfile], entry: InstalledToolchain
    ) -> List[ToolchainExtra]:
        """Declared extras the installation claims to contain (verified functionally)."""
        if profile is None:
            return []
        keys = {(item.get("kind", ""), item.get("target", "")) for item in entry.extras}
        return [item for item in profile.extras if item.key in keys]

    def _missing_required_extras(
        self, profile: ToolchainProfile, entry: InstalledToolchain
    ) -> List[ToolchainExtra]:
        keys = {(item.get("kind", ""), item.get("target", "")) for item in entry.extras}
        return [item for item in profile.required_extras(self.backend) if item.key not in keys]

    @staticmethod
    def _stamp_provenance(
        entry: InstalledToolchain,
        profile: ToolchainProfile,
        source: ToolchainSource,
        extras: Sequence[ToolchainExtra] = (),
    ) -> None:
        entry.extras = [_extra_record(item) for item in extras]
        entry.schema_version = STATE_SCHEMA_VERSION
        entry.version = profile.version or ""
        entry.source_sha256 = (source.sha256 or "").lower()
        entry.archive_filename = _archive_filename(source, profile)
        entry.runtime = profile.target.runtime if profile.target is not None else ""

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
        installed = self.installed(profile_name, registry)
        if installed is None:
            return {}
        profile = registry.get_profile(profile_name)
        if profile is None:
            return {}
        prefix = self.resolve_entry_path(installed)
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
        with self.lock(profile_name):
            version_suffix = profile.version or "unversioned"
            safe = _safe_name(profile_name)
            dest = self.installed_dir / f"{safe}-{version_suffix}"
            stage = self.installed_dir / f".staging-{safe}-{version_suffix}"
            backup = self.installed_dir / f".backup-{safe}-{version_suffix}"
            self.installed_dir.mkdir(parents=True, exist_ok=True)
            self._recover(dest, stage, backup)
            status, _reason = self.installed_status(registry, profile_name)
            existing = self.load_state().profiles.get(profile_name)
            if status == STATUS_COMPATIBLE and existing is not None and existing.variant == chosen:
                return existing

            installed = InstalledToolchain(
                profile=profile_name,
                version=profile.version or "",
                variant=chosen,
                path=str(dest),
                installed_at=_now(),
                verified=True,
            )
            extras = profile.required_extras(self.backend)
            self._stamp_provenance(installed, profile, source, extras)

            def verify(prefix: Path) -> None:
                failures = (
                    verifier(source.recipe, prefix)
                    if verifier is not None
                    else toolchain_builders.verify_installation(profile, source, prefix, extras)
                )
                if failures:
                    raise _install_error(
                        profile_name,
                        f"Toolchain {profile_name!r} verification failed: " + "; ".join(failures),
                        "verification_failed",
                    )

            staged_build = source.type == "build" and source.recipe in STAGED_BUILD_RECIPES
            if source.type == "build" and not staged_build:
                # Source builds bake the prefix into the result, so they build in
                # place; the previous installation is parked and rolled back on failure.
                archive = self._fetch_or_resolve(source, profile)
                self._commit(
                    installed,
                    dest,
                    backup,
                    lambda: self._build_in_place(
                        registry, profile, source, archive, dest, build_runner, build_jobs, verifier
                    ),
                    verify_in_place=verify,
                )
            else:
                if source.type == "local":
                    materialize = lambda: self._install_local(source, stage, profile)  # noqa: E731
                elif staged_build:
                    # Resolve and integrity-check every input before touching staging.
                    archive = self._fetch_or_resolve(source, profile)
                    extra_archives = [
                        (extra, self._fetch_or_resolve(extra.as_source(), profile, extra))
                        for extra in extras
                    ]
                    for extra, extra_archive in extra_archives:
                        self._check_extra_archive(profile, extra, extra_archive)

                    def materialize() -> None:
                        stage.mkdir(parents=True, exist_ok=True)
                        self._build(
                            registry,
                            profile,
                            source,
                            stage,
                            build_runner,
                            build_jobs,
                            verifier,
                            archive,
                        )
                        for extra, extra_archive in extra_archives:
                            self._install_extra(profile, extra, extra_archive, stage, build_runner)

                else:
                    archive = self._fetch_or_resolve(source, profile)
                    materialize = lambda: self._extract(archive, stage, profile)  # noqa: E731
                try:
                    materialize()
                    verify(stage)
                    self._write_provenance(stage, installed)
                except BaseException:
                    shutil.rmtree(stage, ignore_errors=True)
                    raise
                self._commit(installed, dest, backup, stage=stage)
            return installed

    def _recover(self, dest: Path, stage: Path, backup: Path) -> None:
        """Deterministic cleanup of leftovers from an interrupted acquisition."""
        shutil.rmtree(stage, ignore_errors=True)
        if backup.exists():
            if not dest.exists():
                os.replace(backup, dest)  # interrupted between park and activate
            else:
                shutil.rmtree(backup, ignore_errors=True)

    def _commit(
        self,
        installed: InstalledToolchain,
        dest: Path,
        backup: Path,
        build: Optional[Callable[[], None]] = None,
        stage: Optional[Path] = None,
        verify_in_place: Optional[Callable[[Path], None]] = None,
    ) -> None:
        """Activate a candidate installation; any failure restores the previous one.

        State is saved only after activation (and, for in-place builds, after
        verification) succeeded; the superseded installation is deleted last.
        """
        parked = False
        try:
            if dest.exists():
                os.replace(dest, backup)
                parked = True
            if stage is not None:
                try:
                    os.replace(stage, dest)
                except OSError as exc:
                    raise _install_error(
                        installed.profile,
                        f"activating {dest} failed: {exc}",
                        "activation_failed",
                        exc,
                    ) from exc
            else:
                dest.mkdir(parents=True, exist_ok=True)
                assert build is not None and verify_in_place is not None
                build()
                verify_in_place(dest)
                self._write_provenance(dest, installed)
            state = self.load_state()
            state.profiles[installed.profile] = installed
            self.save_state(state)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            if parked:
                os.replace(backup, dest)
            raise
        shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def _write_provenance(prefix: Path, installed: InstalledToolchain) -> None:
        (prefix / PROVENANCE_FILE).write_text(
            json.dumps(installed.provenance(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

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

    def _install_local(
        self, source: ToolchainSource, dest: Path, profile: ToolchainProfile
    ) -> None:
        if not source.path:
            raise _install_error(profile.name, "Local source has no 'path'", "local_path_missing")
        origin = Path(source.path)
        if not origin.exists():
            raise _source_error(
                profile.name, f"Local toolchain path not found: {origin}", "source_missing"
            )
        if origin.is_dir():
            shutil.copytree(origin, dest, dirs_exist_ok=True)
        else:
            self._extract(origin, dest, profile)

    def _fetch_or_resolve(
        self,
        source: ToolchainSource,
        profile: ToolchainProfile,
        extra: Optional[ToolchainExtra] = None,
    ) -> Path:
        """Resolve a source archive and verify its declared SHA-256 before use.

        The mirror is not trusted merely because it is local: the same checksum
        path covers mirror and downloaded archives. Raises ``BuildError`` with
        category ``project_source`` when the archive is absent or corrupt.
        """
        filename = _archive_filename(source, profile)
        if not filename:
            raise _source_error(
                profile.name, "source declares no archive filename or url", "source_unresolvable"
            )

        subject = _extra_subject(profile, extra)

        if self.source_archives_dir is not None:
            candidate = self.source_archives_dir / filename
            if candidate.exists():
                self._verify_archive(candidate, source, profile, subject)
                return candidate

        version = profile.version or ""
        if not self.allow_network_downloads or not source.url:
            raise _source_error(
                profile.name,
                f"{subject}: archive {filename!r} not found in the local sources "
                f"({self.source_archives_dir}); network downloads are disabled",
                "source_missing",
            )
        dest = self.archives_dir / filename
        self._download(source.url.replace("{version}", version), dest, source, profile, subject)
        return dest

    def _verify_archive(
        self,
        path: Path,
        source: ToolchainSource,
        profile: ToolchainProfile,
        subject: str = "",
    ) -> None:
        """Stream-hash ``path`` against the declared checksum (mirror and download)."""
        prefix = f"{subject}: " if subject else ""
        expected = (source.sha256 or "").lower()
        if not expected:
            raise _source_error(
                profile.name,
                f"{prefix}source for {path.name} declares no sha256; refusing to use it",
                "checksum_missing",
            )
        try:
            actual = _sha256(path)
        except OSError as exc:
            raise _source_error(
                profile.name, f"{prefix}cannot read archive {path}: {exc}", "source_unreadable", exc
            ) from exc
        if actual != expected:
            raise _source_error(
                profile.name,
                f"{prefix}checksum mismatch for archive {path}: expected sha256 {expected}, "
                f"got {actual} (the archive was not modified or extracted)",
                "checksum_mismatch",
            )

    def _download(
        self,
        url: str,
        dest: Path,
        source: ToolchainSource,
        profile: ToolchainProfile,
        subject: str = "",
    ) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(url) as response, open(partial, "wb") as out:
                shutil.copyfileobj(response, out)
        except (OSError, ValueError) as exc:
            partial.unlink(missing_ok=True)
            raise _source_error(
                profile.name, f"download of {url} failed: {exc}", "download_failed", exc
            ) from exc
        try:
            self._verify_archive(partial, source, profile, subject)
        except BuildError:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, dest)

    def _check_extra_archive(
        self, profile: ToolchainProfile, extra: ToolchainExtra, archive: Path
    ) -> None:
        """Check the installer metadata inside an (already hashed) extra archive.

        The distribution's top directory must be ``<kind>-<release>-<target>`` and
        its ``components`` manifest must list the declared component; a different
        release or target is rejected before anything is staged.
        """
        subject = _extra_subject(profile, extra)
        top = f"{extra.kind}-{extra.release}-{extra.target}"
        try:
            with tarfile.open(archive, "r:*") as tar:
                names = tar.getnames()
                roots = {name.split("/", 1)[0] for name in names}
                if roots != {top}:
                    raise _install_error(
                        profile.name,
                        f"{subject}: archive {archive.name} contains {sorted(roots)}, "
                        f"expected the single distribution {top!r} "
                        "(release/target mismatch)",
                        "extra_release_mismatch",
                    )
                member = tar.extractfile(f"{top}/components")
                components = (member.read().decode("utf-8") if member else "").split()
        except (tarfile.TarError, OSError, EOFError, KeyError) as exc:
            raise _install_error(
                profile.name,
                f"{subject}: cannot read installer metadata of {archive}: {exc}",
                "extract_failed",
                exc,
            ) from exc
        if extra.component not in components:
            raise _install_error(
                profile.name,
                f"{subject}: archive {archive.name} does not provide component "
                f"{extra.component!r} (has {components})",
                "extra_component_missing",
            )

    def _install_extra(
        self,
        profile: ToolchainProfile,
        extra: ToolchainExtra,
        archive: Path,
        stage: Path,
        runner: Runner,
    ) -> None:
        """Install an additive artifact into the staged prefix with its own installer."""
        subject = _extra_subject(profile, extra)
        safe = f"{_safe_name(profile.name)}-{profile.version or 'unversioned'}"
        work = self.build_dir / f"{safe}-extra-{_safe_name(extra.kind)}-{_safe_name(extra.target)}"
        shutil.rmtree(work, ignore_errors=True)
        try:
            self._extract(archive, work, profile)
            toolchain_builders.install_rust_component(
                _single_child_dir(work),
                stage,
                extra.component,
                self.logs_dir / _safe_name(profile.name),
                runner,
            )
        except BuildError:
            raise
        except Exception as exc:
            raise _install_error(
                profile.name,
                f"{subject}: installing into the staged prefix failed: {exc}",
                "extra_install_failed",
                exc,
            ) from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _extract(self, archive: Path, dest: Path, profile: ToolchainProfile) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(archive, "r:*") as tar:
                tar.extractall(dest, filter="data")
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise _install_error(
                profile.name, f"extracting {archive} failed: {exc}", "extract_failed", exc
            ) from exc
        _flatten_single_top_dir(dest)

    def _build_in_place(
        self,
        registry: ToolchainRegistry,
        profile: ToolchainProfile,
        source: ToolchainSource,
        archive: Path,
        dest: Path,
        runner: Runner,
        jobs: int,
        verifier: Optional[Verifier],
    ) -> None:
        self._build(registry, profile, source, dest, runner, jobs, verifier, archive)

    def _build(
        self,
        registry: ToolchainRegistry,
        profile: ToolchainProfile,
        source: ToolchainSource,
        dest: Path,
        runner: Runner,
        jobs: int,
        verifier: Optional[Verifier],
        archive: Path,
    ) -> None:
        work = self.build_dir / f"{_safe_name(profile.name)}-{profile.version or 'unversioned'}"
        if work.exists():
            shutil.rmtree(work)
        self._extract(archive, work, profile)

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
