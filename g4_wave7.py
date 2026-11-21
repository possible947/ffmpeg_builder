"""Bounded Vulkan, glslang and libplacebo target build and acceptance."""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .builders.codecs.jxl_sources import _extract_dependency
from .builders.graphics.glslang_sources import DEPENDENCIES
from .builders.graphics.vulkan_headers import _digest
from .components import Component, ComponentRegistry
from .config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g4_wave2 import Wave2TargetValidator
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave3b import Wave3BTargetValidator
from .g4_wave6 import validate_prior_manifests
from .state import ComponentStatus
from .windows_probe import WindowsInterop, WindowsProbeError

NAMES = ("vulkan-headers", "glslang", "fast-float", "libplacebo")
SPECS = {
    "vulkan-headers": _OutputSpec(
        "",
        ("include/vulkan/vulkan.h",),
        ("lib/libvulkan.a",),
        ("share/vulkan/registry/vk.xml", "lib/pkgconfig/vulkan.pc"),
        "",
    ),
    "fast-float": _OutputSpec(
        "", ("include/fast_float/fast_float.h",), (), ("include/fast_float",), ""
    ),
    "glslang": _OutputSpec(
        "",
        ("include/glslang/Public/ShaderLang.h",),
        ("lib/libglslang.a", "lib/libSPIRV.a", "lib/libSPIRV-Tools.a", "lib/libSPIRV-Tools-opt.a"),
        (
            "include/glslang",
            "include/spirv-tools",
            "include/spirv",
            "lib/cmake/glslang",
            "lib/cmake/SPIRV-Tools",
            "lib/cmake/SPIRV-Tools-opt",
            "lib/pkgconfig/glslang.pc",
            "lib/pkgconfig/SPIRV-Tools.pc",
        ),
        "",
    ),
    "libplacebo": _OutputSpec(
        "libplacebo",
        ("include/libplacebo/vulkan.h",),
        ("lib/libplacebo.a",),
        ("include/libplacebo", "lib/libplacebo.a", "lib/pkgconfig/libplacebo.pc"),
        "",
    ),
}
SMOKE = """#include <libplacebo/vulkan.h>
#include <libplacebo/dispatch.h>
#include <libplacebo/shaders/custom.h>
#include <stdint.h>
int main(void) {
    pl_vulkan vk = pl_vulkan_create(NULL, NULL);
    if (!vk) return 1;
    pl_gpu gpu = vk->gpu;
    pl_fmt fmt = pl_find_fmt(gpu, PL_FMT_UNORM, 4, 8, 8,
                            PL_FMT_CAP_RENDERABLE | PL_FMT_CAP_HOST_READABLE);
    if (!fmt) return 2;
    pl_tex tex = pl_tex_create(gpu, pl_tex_params(.w=4, .h=4, .format=fmt,
                                                  .renderable=true, .host_readable=true));
    pl_dispatch dp = pl_dispatch_create(NULL, gpu);
    if (!tex || !dp) return 3;
    pl_shader sh = pl_dispatch_begin(dp);
    if (!sh || !pl_shader_custom(sh, &(struct pl_custom_shader) {
            .body = "color = vec4(1.0, 0.0, 0.0, 1.0);",
            .output = PL_SHADER_SIG_COLOR
        })) return 4;
    if (!pl_dispatch_finish(dp, pl_dispatch_params(.shader=&sh, .target=tex))) return 5;
    uint8_t pixels[4 * 4 * 4] = {0};
    if (!pl_tex_download(gpu, pl_tex_transfer_params(.tex=tex, .ptr=pixels))) return 6;
    for (int i = 0; i < 16; i++)
        if (pixels[4*i] != 255 || pixels[4*i+1] != 0 ||
            pixels[4*i+2] != 0 || pixels[4*i+3] != 255) return 7;
    pl_dispatch_destroy(&dp);
    pl_tex_destroy(gpu, &tex);
    pl_vulkan_destroy(&vk);
    return 0;
}
"""


def select_components(registry: ComponentRegistry, selection: Any) -> list[Component]:
    candidates = [*selection.target_inputs, *selection.target_components_to_build]
    selected = {c.name: c for c in candidates}
    if len(selected) != len(candidates):
        raise BuildError("G4 Wave 7", "Configured target selection contains duplicates.")
    ordered: list[Component] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        if name in visiting or name not in selected or registry.get_by_name(name) is None:
            raise BuildError(name, "Missing, unconfigured, or cyclic Wave-7 dependency.")
        visiting.add(name)
        component = selected[name]
        for dependency in component.depends_on:
            if dependency not in NAMES:
                raise BuildError(name, f"Unexpected Wave-7 dependency: {dependency}")
            visit(dependency)
        visiting.remove(name)
        seen.add(name)
        ordered.append(component)

    for name in NAMES:
        visit(name)
    return ordered


class Wave7Validator(Wave3BTargetValidator):
    def __init__(self, environment: Any, identity: str, sources: dict[str, str]):
        super().__init__(environment, identity)
        self.manifest_path = self.workspace / "g4-wave7-acceptance.json"
        self.sources = sources

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        return SPECS[component.name]

    def _owned_outputs(self, component: Component) -> list[str]:
        paths = set(super()._owned_outputs(component))
        if component.name == "glslang":
            for pattern in (
                "lib/libglslang*.a",
                "lib/libSPIRV*.a",
                "lib/libMachineIndependent.a",
                "lib/libOSDependent.a",
                "lib/libOGLCompiler.a",
                "lib/libGenericCodeGen.a",
                "lib/cmake/SPIRV-Tools*/**/*",
                "lib/pkgconfig/SPIRV-Tools*.pc",
            ):
                paths.update(
                    p.relative_to(self.prefix).as_posix()
                    for p in self.prefix.glob(pattern)
                    if p.is_file()
                )
        return sorted(paths)

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest() or {}
        entry = manifest.get("components", {}).get(component.name, {})
        return (
            entry.get("recipe") == "wave7-v1"
            and entry.get("sources") == self.sources
            and Wave3ATargetValidator.manifest_matches(self, component)
        )

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        assert manifest is not None
        manifest["components"][component.name].update(recipe="wave7-v1", sources=self.sources)
        self._write_manifest(manifest)

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        for name in (*spec.headers, *spec.libraries):
            path = self.prefix / name
            if not path.is_file():
                raise BuildError(component.name, f"Missing target output: {name}")
            self._require_inside_prefix(component.name, path)
        if component.name == "vulkan-headers":
            if not (self.prefix / "share/vulkan/registry/vk.xml").is_file():
                raise BuildError(component.name, "Missing matching Vulkan registry.")
        if component.name == "glslang":
            for relative in self._owned_outputs(component):
                if relative.endswith(".a"):
                    self._validate_archive(component.name, self.prefix / relative)
        if component.name == "libplacebo":
            self._validate_archive(component.name, self.prefix / "lib/libplacebo.a")
            self._smoke(component)
        return self._owned_outputs(component)

    def _smoke(self, component: Component) -> None:
        env = dict(self.environment.compiler_environment)
        for variable in ("pl_has_vulkan", "pl_has_glslang", "pl_has_vk_proc_addr"):
            value = self._pkg_config(
                component.name, [f"--variable={variable}", "libplacebo"], env
            ).strip()
            if value != "1":
                raise BuildError(component.name, f"Vulkan feature {variable} is {value!r}.")
        flags = self._pkg_config(
            component.name, ["--static", "--cflags", "--libs", "libplacebo"], env
        )
        self._validate_target_flags(component.name, flags)
        required = {
            "-lplacebo",
            "-lglslang",
            "-lSPIRV",
            "-lSPIRV-Tools",
            "-lSPIRV-Tools-opt",
            "-lc++",
            "-lunwind",
            "-lvulkan",
        }
        if not required.issubset(set(shlex.split(flags))):
            raise BuildError(component.name, f"Incomplete static metadata: {flags}")
        with tempfile.TemporaryDirectory(
            prefix="g4-wave7-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            root = Path(tmp)
            source, exe = root / "consumer.c", root / "consumer.exe"
            source.write_text(SMOKE, encoding="utf-8")
            result = subprocess.run(
                [
                    str(self.environment.target_tools.cc),
                    str(source),
                    *shlex.split(flags),
                    "-o",
                    str(exe),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode:
                raise BuildError(
                    component.name, f"Target consumer link failed: {result.stderr[-6000:]}"
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component.name, exe
            )
            output = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(exe)],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            ).stdout
            dlls = set(re.findall(r"^\s*Name: (.+)$", output, re.MULTILINE))
            print(f"Wave-7 PE imports: {sorted(dlls)}", flush=True)
            if "vulkan-1.dll" not in {name.lower() for name in dlls}:
                raise BuildError(component.name, "Windows Vulkan loader import missing.")
            runtime = self.environment.target_tools.prefix / "x86_64-w64-mingw32/bin"
            for name in dlls:
                path = runtime / name
                if path.is_file():
                    shutil.copyfile(path, root / name)
            windows_path = (
                subprocess.run(
                    ["wslpath", "-w", str(exe)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                .stdout.strip()
                .replace("'", "''")
            )
            try:
                runtime_result = WindowsInterop().run_powershell(
                    f"$ErrorActionPreference='Stop'; & '{windows_path}'; exit $LASTEXITCODE",
                    timeout=90,
                )
            except WindowsProbeError as exc:
                raise BuildError(component.name, f"Windows Vulkan smoke failed: {exc}") from exc
            if runtime_result.returncode:
                raise BuildError(
                    component.name, f"Windows Vulkan smoke failed: {runtime_result.stderr!r}"
                )


def install_vulkan(builder: FFmpegBuilder, component: Component) -> None:
    cross = builder.platform_context.cross_environment
    assert cross is not None
    archive = builder.downloader.download(
        component.get_url(),
        component.get_archive_filename(),
        expected_sha256=component.sha256,
        show_progress=False,
    )
    with tempfile.TemporaryDirectory(
        prefix="wave7-vulkan-", dir=cross.paths.generated_files_root
    ) as tmp:
        source = Path(tmp) / "source"
        _extract_dependency(Path(archive), source)
        registry = source / "registry/vk.xml"
        if _digest(source / "include", registry) != _digest(
            builder.target_prefix / "include", registry
        ):
            raise BuildError(
                component.name, "Materialized Vulkan headers do not match registry source."
            )
        dest = builder.target_prefix / "share/vulkan/registry/vk.xml"
        pc = builder.target_prefix / "lib/pkgconfig/vulkan.pc"
        for path in (dest, pc):
            if path.exists() or path.is_symlink():
                raise BuildError(component.name, f"Refusing to overwrite {path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with registry.open("rb") as src, dest.open("xb") as dst:
            getattr(builder, "_wave7_created_outputs").append(dest)
            shutil.copyfileobj(src, dst)
        pc.parent.mkdir(parents=True, exist_ok=True)
        with pc.open("x", encoding="utf-8") as file:
            getattr(builder, "_wave7_created_outputs").append(pc)
            file.write(
                f"prefix={builder.target_prefix}\nlibdir=${{prefix}}/lib\nincludedir=${{prefix}}/include\n"
                f"Name: Vulkan\nDescription: Windows Vulkan loader interface\nVersion: {component.version}\n"
                "Libs: -L${libdir} -lvulkan\nCflags: -I${includedir}\n"
            )


def run_g4_wave7(app: Any, config: BuildConfig) -> int:
    if (
        config.target_backend != TARGET_BACKEND_CROSS_MINGW
        or not config.native_build
        or not config.openmp
        or not config.enable_libplacebo_vulkan
    ):
        raise BuildError("G4 Wave 7", "Cross native/OpenMP profile with Vulkan enabled required.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        env = app.prepare_cross_environment(config, session)
        selected = select_components(app.registry, app._get_component_selection(config))
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, selected),
            cross_environment=env,
        )
        sources = {}
        for component in selected:
            path = builder.downloader.download(
                component.get_url(),
                component.get_archive_filename(),
                expected_sha256=component.sha256,
                show_progress=False,
            )
            with Path(path).open("rb") as stream:
                sources[component.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        for dependency in DEPENDENCIES:
            builder.downloader.download(
                dependency.url,
                dependency.filename,
                expected_sha256=dependency.sha256,
                show_progress=False,
            )
            sources[dependency.name] = dependency.sha256
        validator = Wave7Validator(env, session.identity.build_key, sources)
        validator.validate_prefix()
        prior_count = validate_prior_manifests(
            validator.workspace,
            validator.prefix,
            validator.identity_key,
            validator.manifest_path.name,
        )
        print(f"Wave-7: verified {prior_count} prior manifest-recorded files", flush=True)
        built, reused = [], []
        for component in selected:
            state = app.state_manager.get().components.get(component.name)
            if state and state.status == ComponentStatus.COMPLETED:
                if not validator.manifest_matches(component):
                    raise BuildError(
                        component.name, "Accepted output or source changed; refusing reuse."
                    )
                validator.validate_component(component)
                reused.append(component.name)
                continue
            if validator._owned_outputs(component):
                raise BuildError(component.name, "Unaccepted outputs exist; refusing overwrite.")
            created_outputs: list[Path] = []
            setattr(builder, "_wave7_created_outputs", created_outputs)
            try:
                app.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.PENDING,
                    component.version,
                    detail="Wave-7 target build",
                )
                if component.name == "vulkan-headers":
                    install_vulkan(builder, component)
                else:
                    builder.build_component(component)
                outputs = validator.validate_component(component)
                validator.validate_prefix()
                validator.record_component(component, outputs)
                app.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.COMPLETED,
                    component.version,
                    detail="Wave-7 acceptance passed",
                )
                built.append(component.name)
            except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
                for path in created_outputs:
                    path.unlink(missing_ok=True)
                app.state_manager.mark_component_status(
                    component.name,
                    ComponentStatus.FAILED,
                    component.version,
                    str(exc),
                    detail="Wave-7 build or acceptance failed",
                )
                raise
            print(f"Wave-7 {component.name}: accepted", flush=True)
        manifest = validator._read_manifest()
        assert manifest is not None
        manifest.update(source_sha256=sources, built=built, reused=reused, ffmpeg_built=False)
        validator._write_manifest(manifest)
        validator.validate_prefix()
        if (
            validate_prior_manifests(
                validator.workspace,
                validator.prefix,
                validator.identity_key,
                validator.manifest_path.name,
            )
            != prior_count
        ):
            raise BuildError("G4 Wave 7", "Prior manifest inventory changed.")
    except (BuildError, SkipComponent, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-7 build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 7 built={built}, reused={reused}", flush=True)
    return 0
