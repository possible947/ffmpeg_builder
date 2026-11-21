"""Normal, identity-guarded Windows dependency and FFmpeg production workflow."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import Component
from .config import PROJECT_ROOT, BuildConfig
from .g4_final import run_g4_final
from .g4_wave2 import Wave2TargetValidator
from .g4_wave6 import validate_prior_manifests
from .state import ComponentStatus


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path: Path, data: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def implementation_digest() -> str:
    """Identify production code used for clean-build acceptance, not just HEAD."""
    paths = [
        *PROJECT_ROOT.glob("*.py"),
        *PROJECT_ROOT.joinpath("builders").rglob("*.py"),
        *PROJECT_ROOT.joinpath("platforms").rglob("*.py"),
        *PROJECT_ROOT.joinpath("patches").rglob("*.py"),
        PROJECT_ROOT / "scripts/run_build.py",
        PROJECT_ROOT / "components.yaml",
        PROJECT_ROOT / "toolchains.yaml",
    ]
    return hashlib.sha256(
        json.dumps(
            {str(p.relative_to(PROJECT_ROOT)): _digest(p) for p in sorted(paths)},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def dependency_order(components: Sequence[Component]) -> list[Component]:
    """Order the effective selection, rejecting missing or cyclic dependencies."""
    selected = {component.name: component for component in components}
    if len(selected) != len(components):
        raise BuildError("Windows build", "Duplicate selected component.")
    ordered: list[Component] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise BuildError(name, "Cyclic target dependency.")
        if name not in selected:
            raise BuildError(name, "Required target dependency is not selected.")
        visiting.add(name)
        for dependency in selected[name].depends_on:
            visit(dependency)
        visiting.remove(name)
        done.add(name)
        ordered.append(selected[name])

    for component in components:
        if component.name != "ffmpeg":
            visit(component.name)
    if "ffmpeg" not in selected:
        raise BuildError("Windows build", "FFmpeg must be selected.")
    visit("ffmpeg")
    return ordered


def run_cross_build(
    app: Any, config: BuildConfig, *, resume: bool = False, no_release: bool = False
) -> int:
    """Build every selected dependency using its existing production contract."""
    from .cross_component_contracts import component_validator
    from .g6 import run_g7_package

    started = time.monotonic()
    builder = None
    session = None
    active = None
    built: list[str] = []
    reused: list[str] = []
    try:
        # Matching accepted artifacts are reused even without --resume. A fresh
        # build is an explicitly empty workspace, never a status-only reset.
        session = app.prepare_target(config, resume=True)
        initial_state = app.state_manager.get()
        clean_start = not initial_state.components and not any(
            path.is_file() or path.is_symlink()
            for directory in ("prefix", "packages", "bin", "release", "build", "generated")
            for path in (session.workspace / directory).rglob("*")
        )
        session.stamp(app.state_manager)
        environment = app.prepare_cross_environment(config, session)
        selection = app._get_component_selection(config)
        input_names = {component.name for component in selection.target_inputs}
        components = dependency_order(
            [
                *selection.target_inputs,
                *selection.target_components_to_build,
                *selection.mixed_components,
            ]
        )
        state = app.state_manager.get()
        provenance_path = session.workspace / "g7-build-start.json"
        implementation = implementation_digest()
        fresh_invocation = not provenance_path.exists()
        if not fresh_invocation:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        else:
            provenance = {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "identity_key": session.identity.build_key,
                "implementation_sha256": implementation,
                "clean_start": clean_start
                and not list(session.workspace.glob("*-acceptance.json")),
                "initial_components": sorted(state.components),
            }
            _write(provenance_path, provenance)
        _write(
            session.workspace / "g7-acceptance.json",
            {
                "status": "RUNNING",
                "identity_key": session.identity.build_key,
                "clean_build": provenance,
            },
        )
        state.config = config.to_dict()
        state.total_steps = len(components)
        app.state_manager.save()
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, components),
            cross_environment=environment,
        )
        setattr(builder, "windows_target_state", app.windows_target_state)
        builder.prefetch_downloads(components)
        sources = {}
        for position, component in enumerate(components, 1):
            active = component
            print(
                f"\n=== [{position}/{len(components)}] {component.name} {component.version} ===",
                flush=True,
            )
            state.current_step = position
            app.state_manager.save()
            archive = builder.downloader.download(
                component.get_url(),
                component.get_archive_filename(),
                expected_sha256=component.sha256,
                show_progress=False,
            )
            sources[component.name] = _digest(Path(archive))
            completed = app.state_manager.is_component_completed(component.name, component.version)
            if component.name == "ffmpeg":
                Wave2TargetValidator(environment, session.identity.build_key).validate_prefix()
                if run_g4_final(
                    app, config, production=True, session=session, environment=environment
                ):
                    raise BuildError("ffmpeg", "Windows FFmpeg integration failed.")
            else:
                validator = component_validator(
                    builder, component, session.identity.build_key, components
                )
                if completed:
                    if not validator.manifest_matches(component):
                        raise BuildError(
                            component.name, "Accepted output or source changed; refusing reuse."
                        )
                else:
                    app.state_manager.mark_component_status(
                        component.name,
                        ComponentStatus.PENDING,
                        component.version,
                        detail="Windows target dependency build",
                    )
                    if component.name not in input_names:
                        builder.build_component(component)
                outputs = validator.validate_component(component)
                validator.record_component(component, outputs)
                app.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.COMPLETED,
                    component.version,
                    detail="Windows target output contract accepted",
                )
            (reused if completed else built).append(component.name)
            print(f"Accepted {component.name}: {'reused' if completed else 'built'}", flush=True)
        active = None
        validate_prior_manifests(
            session.workspace, environment.paths.target_prefix, session.identity.build_key, ""
        )
        if run_g7_package(
            app, config, session, environment, package=config.make_release and not no_release
        ):
            raise BuildError("Windows acceptance", "Runtime/package acceptance failed.")
        effective = config.to_dict()
        final = json.loads(
            (session.workspace / "g4-final-acceptance.json").read_text(encoding="utf-8")
        )
        report = {
            "schema_version": 1,
            "status": "PASS",
            "profile": app.config_manager.config_path.stem,
            "effective_config": effective,
            "configuration_sha256": hashlib.sha256(
                json.dumps(effective, sort_keys=True).encode()
            ).hexdigest(),
            "identity_key": session.identity.build_key,
            "target_identity": session.identity.to_dict(),
            "implementation_sha256": implementation,
            "clean_build": provenance,
            "clean_full_build": provenance["clean_start"]
            and fresh_invocation
            and not reused
            and len(built) == len(components)
            and provenance["implementation_sha256"] == implementation
            and implementation_digest() == implementation,
            "components": [item.name for item in components],
            "features": final["configure_flags"],
            "sources": sources,
            "built": built,
            "reused": reused,
            "identical_run_reuse": not built and len(reused) == len(components),
            "ffmpeg": final,
            "package": (
                json.loads(
                    (session.workspace / "g7-package-acceptance.json").read_text(encoding="utf-8")
                )
                if config.make_release and not no_release
                else {"status": "not requested"}
            ),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": time.monotonic() - started,
        }
        runs = session.workspace / "g7-runs"
        runs.mkdir(exist_ok=True)
        _write(runs / f"{uuid.uuid4().hex}.json", report)
        _write(session.workspace / "g7-acceptance.json", report)
        print(
            f"PASS: Windows workflow built={built}, reused={reused}; "
            f"evidence={session.workspace / 'g7-acceptance.json'}",
            flush=True,
        )
        return 0
    except (
        BuildError,
        SkipComponent,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        if session is None:
            print(f"BLOCKED by identity guard: {exc}", flush=True)
            return 2
        if active is not None:
            app.state_manager.mark_component_status(
                active.name,
                ComponentStatus.FAILED,
                active.version,
                str(exc),
                detail="Windows production workflow failed",
            )
        print(f"FAIL: Windows production workflow: {exc}", flush=True)
        return 1
    finally:
        if builder is not None:
            builder.shutdown_downloads(wait=False)
