"""Promote staged Windows target outputs without overwriting existing files."""

from pathlib import Path
import shutil
from typing import Any

from ...build_types import BuildError


def promote_staged(component: str, staged: Path, prefix: Path, builder: Any) -> None:
    files = sorted(path for path in staged.rglob("*") if path.is_file() or path.is_symlink())
    if not files:
        raise BuildError(component, "Staged installation is empty.")
    for path in files:
        destination = prefix / path.relative_to(staged)
        if path.name.lower().endswith((".exe", ".dll", ".dll.a", ".so", ".dylib")):
            raise BuildError(component, f"Unexpected non-static target output: {path}")
        if path.is_symlink() or destination.exists() or destination.is_symlink():
            raise BuildError(component, f"Refusing unsafe target output: {destination}")
        if not destination.resolve().is_relative_to(prefix.resolve()):
            raise BuildError(component, f"Target output escapes prefix: {destination}")
    for path in files:
        destination = prefix / path.relative_to(staged)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source, destination.open("xb") as target:
            created = getattr(builder, "_wave7_created_outputs", None)
            if created is not None:
                created.append(destination)
            shutil.copyfileobj(source, target)
        shutil.copystat(path, destination)
