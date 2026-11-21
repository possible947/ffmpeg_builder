"""Installed-output contracts for normal Windows dependency builds.

The normal orchestrator builds components, then calls ``validate_component`` and
``record_component``. Validators retain their historical wave behavior; only this
adapter uses the shared ``cross-components-acceptance.json`` manifest. Construct
the adapter before building so staged installers can track newly created outputs.
``builder.windows_target_state`` (or ``target_state=``) supplies physical OpenCL
evidence. Source inputs always pass through the builder's download policy.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from . import (
    g4_wave1,
    g4_wave2,
    g4_wave3a,
    g4_wave3b,
    g4_wave4a,
    g4_wave4b,
    g4_wave4c,
    g4_wave5a,
    g4_wave5b,
    g4_wave5c,
    g4_wave6,
    g4_wave7,
    g4_wave8,
    g4_wave9,
)
from .build_types import BuildError
from .builders.codecs.jxl_sources import DEPENDENCIES as JXL_DEPENDENCIES
from .builders.graphics.glslang_sources import DEPENDENCIES as GLSLANG_DEPENDENCIES
from .components import Component
from .windows_probe import WindowsInterop, WindowsProbeError

MANIFEST_NAME = "cross-components-acceptance.json"
_PROFILE = "normal-component-v1"


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _source(builder: Any, source: Any) -> tuple[str, str]:
    checksum = source.sha256
    if not checksum or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
        raise BuildError(source.name, "Source has no pinned SHA-256 contract.")
    component = isinstance(source, Component)
    try:
        path = Path(
            builder.downloader.download(
                source.get_url() if component else source.url,
                source.get_archive_filename() if component else source.filename,
                expected_sha256=checksum,
                show_progress=False,
            )
        )
        with path.open("rb") as stream:
            pointer = stream.read(128).startswith(b"version https://git-lfs.github.com/spec/v1")
        digest = _sha256(path)
    except (OSError, RuntimeError) as exc:
        raise BuildError(source.name, f"Cannot verify source contract: {exc}") from exc
    if pointer or digest.lower() != checksum.lower():
        raise BuildError(source.name, f"Source checksum mismatch or Git LFS pointer: {path}")
    version = source.version if component else source.revision
    return f"{source.name}-{version}", digest


def _software_smoke(validator: Any, component: Component, source_text: str) -> None:
    env = dict(validator.environment.compiler_environment)
    metadata = validator.prefix / f"lib/pkgconfig/{component.name}.pc"
    g4_wave1.Wave1TargetValidator(
        validator.environment, validator.identity_key
    )._validate_metadata_paths(metadata)
    flags = validator._pkg_config(
        component.name, ["--static", "--cflags", "--libs", component.name], env
    )
    validator._validate_target_flags(component.name, flags)
    library = "-lvmaf" if component.name == "libvmaf" else "-lplacebo"
    tokens = set(shlex.split(flags))
    if not {library, "-lc++", "-lunwind"}.issubset(tokens):
        raise BuildError(component.name, f"Incomplete static metadata: {flags}")
    if tokens.intersection({"-lcuda", "-lcudart", "-lvulkan", "-lOpenCL", "-lglslang"}):
        raise BuildError(component.name, "Software contract links a GPU interface.")
    tools = validator.environment.target_tools
    with tempfile.TemporaryDirectory(
        prefix="cross-software-", dir=validator.environment.paths.generated_files_root
    ) as directory:
        root = Path(directory)
        source, executable = root / "consumer.c", root / "consumer.exe"
        source.write_text(source_text, encoding="utf-8")
        result = subprocess.run(
            [
                str(tools.cc),
                *shlex.split(env.get("CFLAGS", "")),
                str(source),
                *shlex.split(flags),
                *shlex.split(env.get("LDFLAGS", "")),
                "-o",
                str(executable),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            raise BuildError(
                component.name, f"Software target link failed: {result.stderr[-6000:]}"
            )
        g4_wave2.Wave2TargetValidator(
            validator.environment, validator.identity_key
        )._validate_executable(component.name, executable)
        imports = subprocess.run(
            [str(tools.readobj), "--coff-imports", str(executable)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
        dlls = re.findall(r"^\s*Name: (.+)$", imports, re.MULTILINE)
        if any(re.match(r"(?:nvcuda|vulkan-1|opencl|cudart.*)\.dll$", name, re.I) for name in dlls):
            raise BuildError(component.name, "Software consumer imports a GPU runtime.")
        for name in dlls:
            for location in (tools.prefix / "x86_64-w64-mingw32/bin", tools.prefix / "bin"):
                runtime = location / name
                if runtime.is_file():
                    shutil.copyfile(runtime, root / name)
                    break
        try:
            windows_path = (
                subprocess.run(
                    ["wslpath", "-w", str(executable)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                .stdout.strip()
                .replace("'", "''")
            )
            runtime = WindowsInterop().run_powershell(
                f"$ErrorActionPreference='Stop'; & '{windows_path}'; exit $LASTEXITCODE",
                timeout=90,
            )
        except WindowsProbeError as exc:
            raise BuildError(component.name, f"Windows software smoke failed: {exc}") from exc
        if runtime.returncode:
            raise BuildError(component.name, f"Windows software smoke failed: {runtime.stderr!r}")


class _SoftwareVmaf(g4_wave8.Wave8Validator):
    @staticmethod
    def _spec(component: Component) -> g4_wave3a._OutputSpec:
        return g4_wave3a._OutputSpec(
            "libvmaf",
            ("include/libvmaf/libvmaf.h",),
            ("lib/libvmaf.a",),
            ("include/libvmaf", "lib/libvmaf.a", "lib/pkgconfig/libvmaf.pc"),
            "",
        )

    def _smoke(self, component: Component) -> None:
        source = self.environment.paths.packages_root / component.get_target_dir()
        candidates = [
            source / "libvmaf/build/meson-info/intro-buildoptions.json",
            source / "build/meson-info/intro-buildoptions.json",
        ]
        try:
            path = next(path for path in candidates if path.is_file())
            options = json.loads(path.read_text(encoding="utf-8"))
            cuda = next(option["value"] for option in options if option["name"] == "enable_cuda")
        except (OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
            raise BuildError(
                component.name, f"Missing software libvmaf build evidence: {exc}"
            ) from exc
        if cuda is not False:
            raise BuildError(component.name, "Software libvmaf was configured with CUDA enabled.")
        _software_smoke(
            self,
            component,
            "#include <libvmaf/libvmaf.h>\n"
            "int main(void) { VmafContext *ctx = 0; VmafConfiguration cfg = {0}; "
            "if (vmaf_init(&ctx, cfg)) return 1; return vmaf_close(ctx); }\n",
        )


class _SoftwarePlacebo(g4_wave7.Wave7Validator):
    @staticmethod
    def _spec(component: Component) -> g4_wave3a._OutputSpec:
        return g4_wave3a._OutputSpec(
            "libplacebo",
            ("include/libplacebo/log.h",),
            ("lib/libplacebo.a",),
            ("include/libplacebo", "lib/libplacebo.a", "lib/pkgconfig/libplacebo.pc"),
            "",
        )

    def _smoke(self, component: Component) -> None:
        env = dict(self.environment.compiler_environment)
        for variable in ("pl_has_vulkan", "pl_has_glslang", "pl_has_vk_proc_addr"):
            if (
                self._pkg_config(
                    component.name, [f"--variable={variable}", "libplacebo"], env
                ).strip()
                != "0"
            ):
                raise BuildError(component.name, f"Software libplacebo enables {variable}.")
        _software_smoke(
            self,
            component,
            "#include <libplacebo/log.h>\n"
            "int main(void) { pl_log log = pl_log_create(PL_API_VER, 0); "
            "if (!log) return 1; pl_log_destroy(&log); return 0; }\n",
        )


_VALIDATORS = (
    (g4_wave1.G4_WAVE1_COMPONENTS, g4_wave1.Wave1TargetValidator),
    (g4_wave2.G4_WAVE2_COMPONENTS, g4_wave2.Wave2TargetValidator),
    (g4_wave3a.G4_WAVE3A_COMPONENTS, g4_wave3a.Wave3ATargetValidator),
    (g4_wave3b.G4_WAVE3B_COMPONENTS, g4_wave3b.Wave3BTargetValidator),
    (g4_wave4a.G4_WAVE4A_COMPONENTS, g4_wave4a.Wave4ATargetValidator),
    (g4_wave4b.G4_WAVE4B_COMPONENTS, g4_wave4b.Wave4BTargetValidator),
    (g4_wave4c.G4_WAVE4C_COMPONENTS, g4_wave4c.Wave4CTargetValidator),
    (g4_wave5a.G4_WAVE5A_COMPONENTS, g4_wave5a.Wave5ATargetValidator),
    (g4_wave5b.G4_WAVE5B_COMPONENTS, g4_wave5b.Wave5BTargetValidator),
    (g4_wave5c.G4_WAVE5C_COMPONENTS, g4_wave5c.Wave5CTargetValidator),
)


class ComponentContract:
    """Delegate output validation, independently recording normal-build evidence."""

    def __init__(
        self,
        builder: Any,
        component: Component,
        identity: str,
        validator: Any,
        sources: dict[str, str],
        dependencies: Sequence[str],
    ):
        self.builder = builder
        self.validator = validator
        self.component = component
        self.environment = validator.environment
        self.prefix: Path = validator.prefix
        self.identity_key = identity
        self.sources = sources
        self.dependencies = tuple(dependencies)
        self.manifest_path = self.environment.paths.workspace_root / MANIFEST_NAME
        self.validator.manifest_path = self.manifest_path
        self.recipe = f"{_PROFILE}:{type(validator).__name__}"
        self.created_outputs: list[Path] = []
        for attribute in (
            "_wave5c_created_outputs",
            "_wave6_created_outputs",
            "_wave7_created_outputs",
        ):
            setattr(builder, attribute, self.created_outputs)

    def _owned_outputs(self, component: Component) -> list[str]:
        if isinstance(
            self.validator, (g4_wave1.Wave1TargetValidator, g4_wave2.Wave2TargetValidator)
        ):
            return [
                name
                for name in self.validator._spec(component).files
                if (self.prefix / name).is_file()
            ]
        return list(self.validator._owned_outputs(component))

    def finish_build(self) -> None:
        """Complete the Vulkan target input's matching registry and metadata."""
        if self.component.name == "vulkan-headers":
            paths = [
                self.prefix / p for p in ("share/vulkan/registry/vk.xml", "lib/pkgconfig/vulkan.pc")
            ]
            if not all(path.is_file() for path in paths):
                g4_wave7.install_vulkan(self.builder, self.component)

    def validate_component(self, component: Component) -> list[str]:
        self.finish_build()
        outputs = list(self.validator.validate_component(component))
        if not outputs:
            raise BuildError(component.name, "Output contract produced no installed files.")
        for relative in outputs:
            self._checked_output(component.name, relative)
        return outputs

    def _checked_output(self, component: str, relative: str) -> Path:
        path = self.prefix / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not path.resolve().is_relative_to(self.prefix)
        ):
            raise BuildError(component, f"Output contract escaped target prefix: {relative}")
        if not path.is_file():
            raise BuildError(component, f"Output contract file is missing: {relative}")
        return path

    def _dependencies(self, manifest: dict[str, Any]) -> dict[str, Any]:
        entries = manifest.get("components", {})
        result = {}
        for name in self.dependencies:
            entry = entries.get(name)
            if not isinstance(entry, dict) or not entry.get("sha256"):
                raise BuildError(
                    self.component.name, f"Dependency has no normal output acceptance: {name}"
                )
            for relative, expected in entry["sha256"].items():
                if _sha256(self._checked_output(name, relative)) != expected:
                    raise BuildError(
                        self.component.name, f"Accepted dependency output changed: {name}"
                    )
            result[name] = entry["sha256"]
        return result

    def manifest_matches(self, component: Component) -> bool:
        try:
            manifest = self.validator._read_manifest()
            if (
                not manifest
                or manifest.get("identity_key") != self.identity_key
                or manifest.get("target_prefix") != str(self.prefix)
            ):
                return False
            entry = manifest.get("components", {}).get(component.name)
            if (
                not isinstance(entry, dict)
                or entry.get("version") != component.version
                or entry.get("recipe") != self.recipe
                or entry.get("sources") != self.sources
            ):
                return False
            outputs = self._owned_outputs(component)
            if not outputs or set(entry.get("sha256", {})) != set(outputs):
                return False
            return entry.get("dependencies") == self._dependencies(manifest) and all(
                _sha256(self._checked_output(component.name, path)) == entry["sha256"][path]
                for path in outputs
            )
        except (BuildError, OSError, ValueError, TypeError, AttributeError):
            return False

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        manifest = self.validator._read_manifest()
        if manifest is None and self.manifest_path.exists():
            raise BuildError(component.name, "Normal acceptance manifest is unreadable.")
        manifest = manifest or {
            "schema_version": 1,
            "identity_key": self.identity_key,
            "target_prefix": str(self.prefix),
            "components": {},
        }
        if manifest.get("identity_key") != self.identity_key or manifest.get(
            "target_prefix"
        ) != str(self.prefix):
            raise BuildError(component.name, "Normal acceptance manifest identity mismatch.")
        if not outputs or set(outputs) != set(self._owned_outputs(component)):
            raise BuildError(component.name, "Incomplete installed-output acceptance contract.")
        hashes = {p: _sha256(self._checked_output(component.name, p)) for p in outputs}
        manifest["components"][component.name] = {
            "version": component.version,
            "outputs": list(outputs),
            "sha256": hashes,
            "recipe": self.recipe,
            "sources": self.sources,
            "dependencies": self._dependencies(manifest),
        }
        manifest.setdefault("source_sha256", {}).update(self.sources)
        self.validator._write_manifest(manifest)

    def validate_prefix(self) -> None:
        self.validator.validate_prefix()

    def remove_component_outputs(self, component: Component) -> None:
        self.validator.remove_component_outputs(component)
        for path in self.created_outputs:
            path.unlink(missing_ok=True)


def component_validator(
    builder: Any,
    component: Component,
    identity: str,
    selected: Sequence[Component] = (),
    *,
    target_state: Any = None,
) -> ComponentContract:
    """Choose one fail-closed output contract without invoking a wave runner."""
    environment = builder.platform_context.cross_environment
    if environment is None:
        raise BuildError(component.name, "Windows component contract requires a cross environment.")
    validator_class: Any = next(
        (cls for names, cls in _VALIDATORS if component.name in names), None
    )
    config = builder.config
    software_placebo = component.name == "libplacebo" and not config.enable_libplacebo_vulkan
    software_vmaf = component.name == "libvmaf" and not config.enable_libvmaf_cuda
    if component.name == "rav1e":
        validator_class = g4_wave6.Wave6TargetValidator
    elif component.name in g4_wave7.NAMES:
        validator_class = _SoftwarePlacebo if software_placebo else g4_wave7.Wave7Validator
    elif component.name in ("libvmaf", "nv-codec"):
        validator_class = _SoftwareVmaf if software_vmaf else g4_wave8.Wave8Validator
    elif component.name == "opencl-headers":
        validator_class = g4_wave9.Wave9Validator
    if validator_class is None:
        raise BuildError(component.name, "No normal Windows installed-output contract is defined.")
    sources = dict([_source(builder, component)])
    extras: list[Any] = []
    if component.name == "glslang":
        extras.extend(GLSLANG_DEPENDENCIES)
    elif component.name == "libjxl":
        extras.extend(JXL_DEPENDENCIES)
    elif component.name == "VapourSynth":
        ffmpeg = builder.registry.get_by_name("ffmpeg")
        if ffmpeg is None:
            raise BuildError(component.name, "Missing pinned FFmpeg source definition.")
        extras.append(ffmpeg.with_version("9.0"))
    elif component.name == "libvmaf" and not software_vmaf:
        extras.append(builder.registry.get_nv_codec_component())
        extras.extend(c for c in selected if c.name == "nv-codec")
        if hasattr(builder, "platform_detector"):
            extras.append(
                builder.registry.get_nv_codec_component(builder.platform_detector.platform_info)
            )
    elif component.name == "opencl-headers":
        loader = builder.registry.get_by_name("opencl-icd-loader")
        if loader is None:
            raise BuildError(component.name, "Missing pinned OpenCL loader source definition.")
        extras.append(loader)
    for source in extras:
        sources.update([_source(builder, source)])
    validator: Any
    if validator_class is g4_wave6.Wave6TargetValidator:
        validator = validator_class(
            environment, identity, sources[f"{component.name}-{component.version}"]
        )
    elif issubclass(validator_class, g4_wave9.Wave9Validator):
        target_state = target_state or getattr(builder, "windows_target_state", None)
        if target_state is None:
            raise BuildError(component.name, "Physical Windows OpenCL evidence is required.")
        validator = validator_class(environment, identity, sources, target_state)
    elif issubclass(validator_class, g4_wave7.Wave7Validator):
        validator = validator_class(environment, identity, sources)
    elif validator_class is g4_wave5c.Wave5CTargetValidator:
        validator = validator_class(environment, identity, downloader=builder.downloader)
    else:
        validator = validator_class(environment, identity)
    dependencies = component.depends_on
    if software_placebo:
        dependencies = [name for name in dependencies if name not in ("vulkan-headers", "glslang")]
    return ComponentContract(builder, component, identity, validator, sources, dependencies)
