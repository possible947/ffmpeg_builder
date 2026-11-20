"""Linux/WSL host strategy for the isolated Windows LLVM-MinGW target."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from ..build_types import CATEGORY_TARGET_DATA, BuildError
from ..components import ComponentRegistry
from ..config import BuildConfig
from ..cross_environment import CrossEnvironment, construct_cross_environment
from ..wsl_mingw_preparation import WslMingwPreparation
from .base import BasePlatformStrategy
from .context import PlatformContext


class LinuxCrossMingwPlatform(BasePlatformStrategy):
    """Cross-compilation strategy; it never reuses native or MSYS2 behavior."""

    def prepare_cross_environment(
        self,
        preparation: WslMingwPreparation,
        config: BuildConfig,
        workspace: Path,
        source_archives_dir: Path,
        registry: ComponentRegistry,
    ) -> CrossEnvironment:
        """Consume the already-verified G2 result after the identity guard."""
        return construct_cross_environment(
            preparation,
            config,
            workspace,
            source_archives_dir,
            registry=registry,
        )

    def setup_environment(self, context: PlatformContext) -> Dict[str, str]:
        environment = context.cross_environment
        if environment is None:
            raise BuildError(
                "linux-cross-mingw platform",
                "The identity-guarded G3 cross environment has not been prepared.",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_cross_environment_missing",
            )
        return dict(environment.compiler_environment)

    def normalize_path(self, path: str | Path, context: PlatformContext) -> str:
        return Path(path).as_posix()

    def get_pkg_config_path(self, context: PlatformContext, for_posix_shell: bool = False) -> str:
        environment = context.cross_environment
        if environment is None:
            raise BuildError(
                "linux-cross-mingw platform",
                "Target pkg-config is unavailable before G3 environment preparation.",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_cross_environment_missing",
            )
        return environment.compiler_environment["PKG_CONFIG_LIBDIR"]

    def get_cflags(self, context: PlatformContext) -> str:
        environment = self._environment(context)
        return environment["CFLAGS"]

    def get_cxxflags(self, context: PlatformContext) -> str:
        environment = self._environment(context)
        return environment["CXXFLAGS"]

    def get_ldflags(self, context: PlatformContext) -> str:
        environment = self._environment(context)
        return environment["LDFLAGS"]

    def get_ldexeflags(self, context: PlatformContext) -> str:
        return ""

    def get_extralibs(self, context: PlatformContext) -> str:
        return ""

    def get_cpp_runtime_lib(self) -> str:
        return "-lc++"

    def get_ffmpeg_threading_flag(self) -> str:
        return "--enable-w32threads"

    def get_system_lib_paths(self, context: PlatformContext) -> List[Path]:
        environment = context.cross_environment
        if environment is None:
            return []
        return [
            environment.paths.target_prefix / "lib",
            environment.paths.target_prefix / "lib64",
        ]

    @staticmethod
    def _environment(context: PlatformContext) -> Dict[str, str]:
        environment = context.cross_environment
        if environment is None:
            raise BuildError(
                "linux-cross-mingw platform",
                "The identity-guarded G3 cross environment has not been prepared.",
                category=CATEGORY_TARGET_DATA,
                code="wsl_mingw_cross_environment_missing",
            )
        return dict(environment.compiler_environment)
