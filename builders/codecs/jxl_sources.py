"""Pinned source inputs for the Windows-target libjxl build."""

from __future__ import annotations

import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ...build_types import BuildError

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder


@dataclass(frozen=True)
class JxlDependency:
    name: str
    revision: str
    sha256: str

    @property
    def filename(self) -> str:
        return f"libjxl-{self.name}-{self.revision}.tar.gz"

    @property
    def url(self) -> str:
        return f"https://api.github.com/repos/google/{self.name}/tarball/{self.revision}"


DEPENDENCIES = (
    JxlDependency(
        "brotli",
        "028fb5a23661f123017c060daa546b55cf4bde29",
        "eb5f7dadf215d0670665fd81566e1fe2dfdc154d983f09142de7299df4c182e6",
    ),
    JxlDependency(
        "highway",
        "457c891775a7397bdb0376bb1031e6e027af1c48",
        "666239316815dc8c04a38e56fc9ca18a1fca6a4c46dfe774e9ec2a16c6d2a26d",
    ),
)


def _extract_dependency(archive: Path, destination: Path) -> None:
    """Extract one verified archive without overwriting an existing source tree."""
    if destination.is_symlink() or (
        destination.exists() and (not destination.is_dir() or any(destination.iterdir()))
    ):
        raise ValueError(f"Refusing to overwrite existing source tree: {destination}")
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise ValueError(f"Invalid source parent: {destination.parent}")
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
            roots = {PurePosixPath(member.name).parts[0] for member in members if member.name}
            if len(roots) != 1 or not members:
                raise ValueError(f"Unexpected archive layout: {archive}")
            root = next(iter(roots))
            for member in members:
                parts = PurePosixPath(member.name).parts
                if (
                    not parts
                    or parts[0] != root
                    or PurePosixPath(member.name).is_absolute()
                    or any(part in (".", "..") for part in parts)
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError(f"Unsafe archive member: {member.name}")
            tar.extractall(staging, members=members, filter="data")
        extracted = staging / root
        if not extracted.is_dir() or not (extracted / "CMakeLists.txt").is_file():
            raise ValueError(f"Missing dependency CMakeLists.txt: {archive}")
        if destination.exists():
            destination.rmdir()
        extracted.rename(destination)


def prepare_libjxl_sources(builder: FFmpegBuilder, source_dir: Path) -> dict[str, dict[str, str]]:
    """Use the project's verified archive cache and download policy."""
    result: dict[str, dict[str, str]] = {}
    try:
        for dependency in DEPENDENCIES:
            archive = builder.downloader.download(
                dependency.url,
                dependency.filename,
                expected_sha256=dependency.sha256,
                show_progress=False,
            )
            _extract_dependency(Path(archive), source_dir / "third_party" / dependency.name)
            result[dependency.name] = {
                "revision": dependency.revision,
                "sha256": dependency.sha256,
            }
    except (OSError, ValueError, tarfile.TarError, RuntimeError) as exc:
        raise BuildError("libjxl", f"Cannot prepare pinned dependency sources: {exc}") from exc
    return result
