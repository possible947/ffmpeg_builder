"""Bounded Windows-target OpenSSL and SRT dependency-chain acceptance."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .build_types import BuildError, SkipComponent
from .builder import FFmpegBuilder
from .components import BuildRole, Component, ComponentRegistry
from .config import PROJECT_ROOT, TARGET_BACKEND_CROSS_MINGW, BuildConfig
from .g4_wave1 import Wave1TargetValidator
from .g4_wave2 import Wave2TargetValidator
from .g4_wave3a import Wave3ATargetValidator, _OutputSpec
from .g4_wave3b import Wave3BTargetValidator
from .state import ComponentStatus

G4_WAVE5A_COMPONENTS = ("openssl", "srt")


def _cpu_flags(environment: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            flag
            for variable in ("CFLAGS", "CXXFLAGS")
            for flag in shlex.split(environment.compiler_environment.get(variable, ""))
            if flag.startswith(("-march=", "-mtune=", "-mcpu="))
        )
    )


_SPECS = {
    "openssl": _OutputSpec(
        "openssl",
        ("include/openssl/ssl.h", "include/openssl/evp.h", "include/openssl/opensslconf.h"),
        ("lib/libssl.a", "lib/libcrypto.a"),
        (
            "include/openssl",
            "lib/libssl.a",
            "lib/libcrypto.a",
            "lib/pkgconfig/openssl.pc",
            "lib/pkgconfig/libssl.pc",
            "lib/pkgconfig/libcrypto.pc",
        ),
        "",
    ),
    "srt": _OutputSpec(
        "srt",
        ("include/srt/srt.h",),
        ("lib/libsrt.a",),
        ("include/srt", "lib/libsrt.a", "lib/pkgconfig/srt.pc", "lib/pkgconfig/haisrt.pc"),
        "#include <srt/srt.h>\n#include <zlib.h>\n#include <openssl/ssl.h>\n"
        "int main(void) { srt_startup(); SRTSOCKET s = srt_create_socket(); "
        'char pass[] = "password123"; srt_setsockflag(s, SRTO_PASSPHRASE, pass, 11); '
        "srt_close(s); srt_cleanup(); SSL_CTX *ctx = SSL_CTX_new(TLS_method()); "
        "SSL_CTX_free(ctx); return zlibVersion() == 0; }\n",
    ),
}
_SMOKES = {
    "libcrypto": "#include <openssl/evp.h>\nint main(void) { EVP_MD_CTX *c=EVP_MD_CTX_new(); "
    "EVP_DigestInit_ex(c, EVP_sha256(), 0); EVP_MD_CTX_free(c); return 0; }\n",
    "libssl": "#include <openssl/ssl.h>\nint main(void) { SSL_CTX *c=SSL_CTX_new(TLS_method()); "
    "SSL_CTX_free(c); return 0; }\n",
}


def verify_sources(registry: ComponentRegistry, archives: Path) -> dict[str, str]:
    result = {}
    for name in G4_WAVE5A_COMPONENTS:
        component = registry.get_by_name(name)
        if component is None or not component.sha256:
            raise BuildError(name, "Missing registered source/checksum.")
        path = archives / component.get_archive_filename()
        try:
            with path.open("rb") as stream:
                header = stream.read(128)
                stream.seek(0)
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise BuildError(name, f"Project-managed source unavailable: {path}: {exc}") from exc
        if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise BuildError(name, f"Unmaterialised Git LFS source: {path}")
        if digest.lower() != component.sha256.lower():
            raise BuildError(name, f"Source checksum mismatch: {path}")
        result[name] = digest
    return result


def select_components(
    registry: ComponentRegistry, selected: Sequence[Component]
) -> list[Component]:
    available = {item.name: item for item in selected}
    if len(available) != len(selected):
        raise BuildError("G4 Wave 5A", "Duplicate configured component.")
    ordered: list[Component] = []
    seen = {"zlib"}
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        if name in visiting:
            raise BuildError(name, "Cycle in Wave-5A dependency graph.")
        visiting.add(name)
        component = available.get(name)
        if component is None or component.build_role != BuildRole.TARGET_COMPONENT:
            raise BuildError(name, "Target component not configured.")
        expected = {"openssl": {"zlib"}, "srt": {"openssl"}}[name]
        if set(component.depends_on) != expected:
            raise BuildError(name, "Unexpected target dependency graph.")
        if registry.get_by_name(name) is None:
            raise BuildError(name, "Missing registry component.")
        for dependency in component.depends_on:
            if registry.get_by_name(dependency) is None:
                raise BuildError(name, f"Missing dependency {dependency}.")
            visit(dependency)
        ordered.append(component)
        visiting.remove(name)
        seen.add(name)

    for name in G4_WAVE5A_COMPONENTS:
        visit(name)
    return ordered


class Wave5ATargetValidator(Wave3BTargetValidator):
    def __init__(self, environment: Any, identity_key: str):
        super().__init__(environment, identity_key)
        self.manifest_path = self.workspace / "g4-wave5a-acceptance.json"

    @staticmethod
    def _spec(component: Component) -> _OutputSpec:
        try:
            return _SPECS[component.name]
        except KeyError as exc:
            raise BuildError(component.name, "No Wave-5A output contract.") from exc

    def manifest_matches(self, component: Component) -> bool:
        manifest = self._read_manifest()
        entry = manifest.get("components", {}).get(component.name, {}) if manifest else {}
        if entry.get("recipe") != "wave5a-v2":
            return False
        if component.name == "srt":
            digest = self._openssl_digest()
            if not digest or entry.get("openssl_sha256") != digest:
                return False
        return Wave3ATargetValidator.manifest_matches(self, component)

    def _openssl_digest(self) -> str:
        manifest = self._read_manifest()
        entries = manifest.get("components", {}) if manifest else {}
        entry = entries.get("openssl", {})
        hashes = entry.get("sha256") if isinstance(entry, dict) else None
        if (
            not manifest
            or manifest.get("identity_key") != self.identity_key
            or manifest.get("target_prefix") != str(self.prefix)
            or not isinstance(hashes, dict)
            or not hashes
        ):
            return ""
        for relative, expected in hashes.items():
            path = self.prefix / relative
            if (
                not self._inside(path, self.prefix)
                or not path.is_file()
                or self._sha256(path) != expected
            ):
                return ""
        return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()

    def record_component(self, component: Component, outputs: Sequence[str]) -> None:
        Wave3ATargetValidator.record_component(self, component, outputs)
        manifest = self._read_manifest()
        if manifest is None:
            raise BuildError(component.name, "Acceptance manifest missing.")
        entry = manifest["components"][component.name]
        entry["recipe"] = "wave5a-v2"
        if component.name == "srt":
            digest = self._openssl_digest()
            if not digest:
                raise BuildError(component.name, "Accepted OpenSSL dependency is invalid.")
            entry["openssl_sha256"] = digest
        self._write_manifest(manifest)

    def validate_prefix(self) -> None:
        Wave1TargetValidator(self.environment, self.identity_key).validate_prefix()
        Wave2TargetValidator(self.environment, self.identity_key).validate_prefix()

    def validate_component(self, component: Component) -> list[str]:
        spec = self._spec(component)
        for relative in (*spec.headers, *spec.libraries, f"lib/pkgconfig/{spec.pkg_config}.pc"):
            path = self.prefix / relative
            if not path.is_file():
                raise BuildError(component.name, f"Missing required target output: {path}")
            self._require_inside_prefix(component.name, path)
        for relative in spec.libraries:
            archive = self.prefix / relative
            self._validate_archive(component.name, archive)
            listed = subprocess.run(
                [str(self.environment.target_tools.ar), "t", str(archive)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            members = listed.stdout.splitlines()
            inspected = subprocess.run(
                [str(self.environment.target_tools.readobj), "--file-headers", str(archive)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            formats = re.findall(r"^Format:\s*(\S+)", inspected.stdout, re.MULTILINE)
            if (
                listed.returncode
                or inspected.returncode
                or not members
                or len(members) != len(formats)
                or any(item != "COFF-x86-64" for item in formats)
            ):
                raise BuildError(component.name, f"Non-COFF or invalid archive: {archive}")
            print(f"Wave-5A {archive.name}: {len(members)} COFF members", flush=True)
        env = dict(self.environment.compiler_environment)
        packages = ("libcrypto", "libssl", "openssl") if component.name == "openssl" else ("srt",)
        for package in packages:
            path = self.prefix / "lib/pkgconfig" / f"{package}.pc"
            Wave1TargetValidator(self.environment, self.identity_key)._validate_metadata_paths(path)
            value = self._pkg_config(component.name, [f"--variable=prefix", package], env).strip()
            if Path(value).resolve() != self.prefix:
                raise BuildError(component.name, f"Wrong pkg-config prefix: {value}")
            flags = self._pkg_config(
                component.name, ["--static", "--cflags", "--libs", package], env
            )
            self._validate_target_flags(component.name, flags)
            if "-lstdc++" in shlex.split(flags):
                raise BuildError(component.name, "Host C++ runtime in target metadata.")
            if package in ("libcrypto", "srt"):
                expected = set(component.target_link_metadata.consumer_link_requirements)
                missing = expected - set(shlex.split(flags))
                if missing:
                    raise BuildError(component.name, f"Static metadata missing: {sorted(missing)}")
            if package == "srt" and "-lomp" in shlex.split(flags):
                raise BuildError(component.name, "SRT metadata incorrectly requires OpenMP.")
        self._validate_configuration(component)
        for package, source in (
            _SMOKES.items() if component.name == "openssl" else (("srt", spec.smoke_source),)
        ):
            self._link(component.name, package, source, env)
        return self._owned_outputs(component)

    def _validate_configuration(self, component: Component) -> None:
        source = self.environment.paths.packages_root / component.get_target_dir()
        if component.name == "srt":
            path = source / "build-cross-mingw/CMakeCache.txt"
            try:
                text = path.read_text()
            except OSError as exc:
                raise BuildError(component.name, f"Missing CMake cache: {exc}") from exc
            for value in (
                "ENABLE_ENCRYPTION:BOOL=ON",
                "ENABLE_SHARED:BOOL=OFF",
                "ENABLE_STATIC:BOOL=ON",
                "USE_ENCLIB:STRING=openssl-evp",
                "SSL_FOUND:INTERNAL=1",
                str(self.environment.cmake_toolchain_file),
                str(self.prefix),
                *_cpu_flags(self.environment),
            ):
                if value not in text:
                    raise BuildError(component.name, f"Missing SRT cross configuration: {value}")
        else:
            try:
                text = (source / "Makefile").read_text()
                log = (self.workspace / "logs/openssl_configure.log").read_text()
            except OSError as exc:
                raise BuildError(
                    component.name, f"Missing OpenSSL cross configuration: {exc}"
                ) from exc
            for value in ("Configure mingw64", "no-shared", "no-module", "--with-zlib-lib="):
                if value not in log:
                    raise BuildError(component.name, f"Missing OpenSSL target option: {value}")
            for value in (str(self.environment.target_tools.cc), *_cpu_flags(self.environment)):
                if value not in text:
                    raise BuildError(component.name, f"Missing OpenSSL target setting: {value}")

    def _link(self, component: str, package: str, source: str, env: dict[str, str]) -> None:
        flags = self._pkg_config(component, ["--static", "--cflags", "--libs", package], env)
        with tempfile.TemporaryDirectory(
            prefix="g4-wave5a-", dir=self.environment.paths.generated_files_root
        ) as tmp:
            src = Path(tmp) / "smoke.c"
            exe = Path(tmp) / "smoke.exe"
            src.write_text(source)
            command = [
                str(self.environment.target_tools.cc),
                *[f for f in shlex.split(env.get("CFLAGS", "")) if f != "-fopenmp"],
                str(src),
                *shlex.split(flags),
                "-o",
                str(exe),
            ]
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=120, check=False
            )
            if result.returncode:
                raise BuildError(
                    component, f"{package} target link failed: {result.stderr[-6000:]}"
                )
            Wave2TargetValidator(self.environment, self.identity_key)._validate_executable(
                component, exe
            )
            imports = subprocess.run(
                [str(self.environment.target_tools.readobj), "--coff-imports", str(exe)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if imports.returncode:
                raise BuildError(component, "Cannot inspect target imports.")
            dlls = sorted(re.findall(r"^\s*Name: (.+)$", imports.stdout, re.MULTILINE))
            print(f"Wave-5A {package} imports: {dlls}", flush=True)
            for omit in (
                ("-lz", "-lcrypt32")
                if package == "libcrypto"
                else (
                    ("-lz", "-lcrypto", "-lpthread", "-lc++", "-lws2_32")
                    if package == "srt"
                    else ()
                )
            ):
                negative = subprocess.run(
                    [item for item in command if item != omit],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if not negative.returncode:
                    raise BuildError(component, f"{package} linked without required {omit}.")


def build_or_reuse(
    component: Component, builder: Any, manager: Any, validator: Wave5ATargetValidator
) -> bool:
    prior = manager.get().components.get(component.name)
    if prior and prior.status == ComponentStatus.COMPLETED:
        if not validator.manifest_matches(component):
            raise BuildError(
                component.name, "Accepted output or dependency changed; refusing reuse."
            )
        try:
            validator.validate_component(component)
            validator.validate_prefix()
        except BuildError as exc:
            manager.mark_component_status(
                component.name,
                ComponentStatus.FAILED,
                component.version,
                str(exc),
                detail="Wave-5A reuse validation failed",
            )
            raise
        return False
    try:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.PENDING,
            component.version,
            detail="Wave-5A target outputs require build",
        )
        builder.build_component(component)
        outputs = validator.validate_component(component)
        validator.validate_prefix()
        validator.record_component(component, outputs)
        manager.mark_component_status(
            component.name,
            ComponentStatus.COMPLETED,
            component.version,
            detail="Wave-5A target acceptance passed",
        )
        return True
    except (BuildError, SkipComponent, OSError, subprocess.SubprocessError) as exc:
        validator.remove_component_outputs(component)
        manager.mark_component_status(
            component.name,
            ComponentStatus.FAILED,
            component.version,
            str(exc),
            detail="Wave-5A build or acceptance failed",
        )
        if isinstance(exc, BuildError):
            raise
        raise BuildError(component.name, f"Wave-5A build or acceptance failed: {exc}") from exc


def run_wave_components(
    ordered: Sequence[Component], builder: Any, manager: Any, validator: Wave5ATargetValidator
) -> tuple[list[str], list[str]]:
    if tuple(item.name for item in ordered) != G4_WAVE5A_COMPONENTS:
        raise BuildError("G4 Wave 5A", "Execution must contain only ordered OpenSSL and SRT.")
    built: list[str] = []
    reused: list[str] = []
    for component in ordered:
        changed = build_or_reuse(component, builder, manager, validator)
        (built if changed else reused).append(component.name)
        print(
            f"Wave-5A {component.name}: {'built' if changed else 'revalidated and reused'}",
            flush=True,
        )
    return built, reused


def run_g4_wave5a(app: Any, config: BuildConfig) -> int:
    if config.target_backend != TARGET_BACKEND_CROSS_MINGW or config.allow_network_downloads:
        raise BuildError("G4 Wave 5A", "Cross backend and offline sources are required.")
    if not config.native_build or not config.openmp:
        raise BuildError("G4 Wave 5A", "The primary native/OpenMP profile is required.")
    archives = Path(config.source_archives_dir)
    if not archives.is_absolute():
        archives = PROJECT_ROOT / archives
    sources = verify_sources(app.registry, archives)
    print(f"Verified Wave-5A sources: {sources}", flush=True)
    try:
        session = app.prepare_target(config, resume=True)
        session.stamp(app.state_manager)
        env = app.prepare_cross_environment(config, session)
        if env.paths.target_prefix.resolve() != (session.workspace / "prefix").resolve():
            raise BuildError("G4 Wave 5A", "Cross environment does not match identity.")
        zlib = app.registry.get_by_name("zlib")
        state = app.state_manager.get().components.get("zlib")
        zvalidator = Wave1TargetValidator(env, session.identity.build_key)
        if (
            zlib is None
            or state is None
            or state.status != ComponentStatus.COMPLETED
            or not zvalidator.manifest_matches(zlib)
        ):
            raise BuildError("zlib", "Accepted same-identity target zlib is unavailable.")
        zvalidator.validate_component(zlib)
        print("Wave-5A zlib: revalidated and reused", flush=True)
        ordered = select_components(
            app.registry, app._get_component_selection(config).target_components_to_build
        )
        validator = Wave5ATargetValidator(env, session.identity.build_key)
        validator.validate_prefix()
        builder = FFmpegBuilder(
            config,
            app.workspace,
            app.packages,
            app.state_manager,
            app.platform_detector,
            toolchain_env=app._component_toolchain_env(config, ordered),
            cross_environment=env,
        )
        built, reused = run_wave_components(ordered, builder, app.state_manager, validator)
        manifest = validator._read_manifest()
        if manifest is None:
            raise BuildError("G4 Wave 5A", "Acceptance manifest missing.")
        manifest.update(source_sha256=sources, built=built, reused=reused, ffmpeg_built=False)
        validator._write_manifest(manifest)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Wave-5A build or acceptance: {exc}", flush=True)
        return 1
    print(f"PASS: Wave 5A built={built}, reused={reused}", flush=True)
    return 0
