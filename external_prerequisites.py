"""Resolution for externally managed build-host prerequisites.

External prerequisites are located and validated, but are never installed or
added to the project-managed ToolchainStore.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .build_types import CATEGORY_EXTERNAL_PREREQUISITE, BuildError

RESOLUTION_CONFIG = "config"
RESOLUTION_ENVIRONMENT = "environment"

CODE_PREREQUISITE_MISSING = "prerequisite_missing"
CODE_PREREQUISITE_INVALID_PATH = "prerequisite_invalid_path"
CODE_PREREQUISITE_VALIDATION_FAILED = "prerequisite_validation_failed"


@dataclass(frozen=True)
class ExternalPrerequisiteValidation:
    """Technology-specific validation results for an external prerequisite."""

    usable: bool = True
    host_tools: Mapping[str, Path] = field(default_factory=dict)
    diagnostics: Tuple[str, ...] = ()
    diagnostic_metadata: Mapping[str, Any] = field(default_factory=dict)
    effective_values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedExternalPrerequisite:
    """Resolved location and validation provenance for a host prerequisite."""

    name: str
    root: Path
    resolution_source: str
    host_tools: Mapping[str, Path]
    usable: bool
    diagnostics: Tuple[str, ...]
    diagnostic_metadata: Mapping[str, Any]
    effective_values: Mapping[str, Any]


Validator = Callable[[Path], ExternalPrerequisiteValidation]


def resolve_external_prerequisite(
    name: str,
    configured_path: Optional[str] = None,
    environment_variable: Optional[str] = None,
    *,
    required: bool = True,
    validator: Optional[Validator] = None,
    environment: Optional[Mapping[str, str]] = None,
    environment_variables: Sequence[str] = (),
) -> Optional[ResolvedExternalPrerequisite]:
    """Resolve and validate a configured external prerequisite root.

    Explicit configuration is authoritative, including when it is invalid.
    Explicitly named environment variables are consulted in the supplied order;
    PATH and ambient build-environment mutation are intentionally excluded.
    """
    if not name or not name.strip():
        raise ValueError("Prerequisite name must not be empty")

    config_key = f"external_prerequisite_paths.{name}"
    selected_path: Optional[str]
    source: Optional[str]
    candidates = tuple(
        dict.fromkeys(
            ([environment_variable] if environment_variable else []) + list(environment_variables)
        )
    )
    selected_environment_variable: Optional[str] = None
    if configured_path is not None:
        selected_path = configured_path
        source = RESOLUTION_CONFIG
    else:
        values = os.environ if environment is None else environment
        selected_environment_variable = next(
            (variable for variable in candidates if values.get(variable)), None
        )
        if selected_environment_variable is not None:
            selected_path = values[selected_environment_variable]
            source = RESOLUTION_ENVIRONMENT
        else:
            selected_path = None
            source = None

    if selected_path is None:
        if not required:
            return None
        env_hint = " or set " + " or ".join(candidates) if candidates else ""
        raise BuildError(
            name,
            f"Required external prerequisite is not configured; set {config_key}{env_hint}.",
            category=CATEGORY_EXTERNAL_PREREQUISITE,
            code=CODE_PREREQUISITE_MISSING,
        )

    if source is None:
        raise RuntimeError("Resolved prerequisite path has no provenance")
    location = str(selected_path)
    configure_hint = f"Configure {config_key}"
    if candidates:
        configure_hint += " or set " + " or ".join(candidates)
    try:
        if not location.strip():
            raise ValueError("path is empty")
        root = Path(location).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(f"{root} is not a directory")
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise BuildError(
            name,
            f"Selected location {location!r} from {source} is invalid: {exc}. "
            f"{configure_hint} to a valid prerequisite root.",
            category=CATEGORY_EXTERNAL_PREREQUISITE,
            code=CODE_PREREQUISITE_INVALID_PATH,
        ) from exc

    validation = validator(root) if validator is not None else ExternalPrerequisiteValidation()
    if not validation.usable:
        details = "; ".join(validation.diagnostics) or "technology-specific validation failed"
        raise BuildError(
            name,
            f"Prerequisite at {root} selected from {source} failed validation: {details}. "
            f"Check the prerequisite installation or {configure_hint}.",
            category=CATEGORY_EXTERNAL_PREREQUISITE,
            code=CODE_PREREQUISITE_VALIDATION_FAILED,
        )

    diagnostic_metadata = dict(validation.diagnostic_metadata)
    diagnostic_metadata.update(
        {
            "selected_value": location,
            "configuration_key": config_key,
            "environment_variable": selected_environment_variable,
        }
    )
    return ResolvedExternalPrerequisite(
        name=name,
        root=root,
        resolution_source=source,
        host_tools=dict(validation.host_tools),
        usable=validation.usable,
        diagnostics=tuple(validation.diagnostics),
        diagnostic_metadata=diagnostic_metadata,
        effective_values=dict(validation.effective_values),
    )
