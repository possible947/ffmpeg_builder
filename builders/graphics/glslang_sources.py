"""Pinned glslang optimizer inputs, matching upstream known_good.json."""

from __future__ import annotations

import json
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...build_types import BuildError
from ...target_inputs import _copy_header_tree
from ..codecs.jxl_sources import _extract_dependency

if TYPE_CHECKING:
    from ...builder import FFmpegBuilder


@dataclass(frozen=True)
class Dependency:
    name: str
    revision: str
    sha256: str

    @property
    def filename(self) -> str:
        return f"glslang-{self.name}-{self.revision}.tar.gz"

    @property
    def url(self) -> str:
        return f"https://api.github.com/repos/KhronosGroup/{self.name}/tarball/{self.revision}"


DEPENDENCIES = (
    Dependency(
        "SPIRV-Tools",
        "b707790a898e44038547df54580022fc1cf89c3d",
        "30164b5c8265e09e60c325c990b5ac3ae8e1ff804bb3f53f7710e3944d17704d",
    ),
    Dependency(
        "SPIRV-Headers",
        "29981f65241605e08b0ede4cfeb999fe3b723c6a",
        "28d7cdd8681fa2c47dcafded63e168a5b1086cd26d5a7990626fdab02dc2ddf9",
    ),
)


def install_spirv_headers(builder: FFmpegBuilder) -> None:
    """Expose the pinned glslang SPIR-V headers to target consumers."""
    cross = builder.platform_context.cross_environment
    if cross is None:
        raise BuildError("glslang", "A target cross environment is required for SPIR-V headers.")
    dependency = DEPENDENCIES[1]
    archive = builder.downloader.download(
        dependency.url,
        dependency.filename,
        expected_sha256=dependency.sha256,
        show_progress=False,
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="spirv-headers-",
            dir=cross.paths.generated_files_root,
        ) as directory:
            source = Path(directory) / "source"
            _extract_dependency(Path(archive), source)
            _copy_header_tree(
                source / "include" / "spirv", builder.target_prefix / "include" / "spirv", "glslang"
            )
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise BuildError("glslang", f"Cannot install pinned SPIR-V headers: {exc}") from exc


def prepare_glslang_sources(builder: FFmpegBuilder, source_dir: Path) -> None:
    try:
        known = json.loads((source_dir / "known_good.json").read_text(encoding="utf-8"))
        commits = {item["name"]: item["commit"] for item in known["commits"]}
        if (
            commits.get("spirv-tools") != DEPENDENCIES[0].revision
            or commits.get("spirv-tools/external/spirv-headers") != DEPENDENCIES[1].revision
        ):
            raise ValueError("Pinned SPIR-V revisions differ from glslang known_good.json")
        for dependency, destination in zip(
            DEPENDENCIES,
            (
                source_dir / "External" / "spirv-tools",
                source_dir / "External" / "spirv-tools" / "external" / "spirv-headers",
            ),
        ):
            archive = builder.downloader.download(
                dependency.url,
                dependency.filename,
                expected_sha256=dependency.sha256,
                show_progress=False,
            )
            _extract_dependency(Path(archive), destination)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, RuntimeError) as exc:
        raise BuildError("glslang", f"Cannot prepare pinned SPIR-V sources: {exc}") from exc
