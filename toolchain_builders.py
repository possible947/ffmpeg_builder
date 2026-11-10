"""Build recipes for source-based toolchain acquisition.

Recipes are invoked by :class:`ffmpeg_builder.toolchain_store.ToolchainStore`
when a profile's ``build`` source variant is selected. Execution is delegated to
an injected ``runner`` so the recipes stay testable without spawning processes.

Registry-declared ``configure_args``/``build_args``/``install_args`` support
three placeholders, resolved by :func:`resolve_args` before a recipe runs:

- ``{prefix}`` — the installation prefix of the profile being built;
- ``{jobs}`` — the parallel job count for ``make``;
- ``{prereq:<name>}`` — the installation prefix of a bootstrap prerequisite.

Portability: toolchain builds must not inherit the FFmpeg build environment, so
:func:`default_runner` runs every command with a sanitized copy of the process
environment (build flags and vendor variables are dropped). The recipes
themselves use only paths derived from arguments, never host-specific absolute
paths.

Verification: :func:`verify_profile` probes an installed profile (compiler
version plus a real compile/link for compilers, expected artifacts for
libraries) and returns a list of failure descriptions. An empty list means the
profile is usable. Profiles that carry a declarative ``verify`` block (managed
binary cross toolchains) are checked by :func:`verify_declared` instead; the
store dispatches through :func:`verify_installation`.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .toolchain_registry import (
    CompileProbe,
    ToolchainExtra,
    ToolchainProfile,
    ToolchainSource,
)

# (command, cwd, log_file)
Runner = Callable[[List[str], Path, Path], None]

PREREQ_RE = re.compile(r"\{prereq:([A-Za-z0-9_.\-]+)\}")

# Variables that must not leak from the FFmpeg build into a toolchain bootstrap.
_SANITIZED_ENV_VARS = (
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
    "LIBRARY_PATH",
    "LD_LIBRARY_PATH",
    "PKG_CONFIG_PATH",
    "PKG_CONFIG_LIBDIR",
    "CUDA_NVCC_CCBIN",
    "CUDA_HOME",
    "CUDA_PATH",
    "NVCC_PREPEND_FLAGS",
    "VULKAN_SDK",
    "RUSTFLAGS",
    "CARGO_BUILD_RUSTFLAGS",
    "AS",
)


def clean_env() -> Dict[str, str]:
    """Return a sanitized copy of the process environment for toolchain builds."""
    env = dict(os.environ)
    for name in _SANITIZED_ENV_VARS:
        env.pop(name, None)
    return env


def resolve_args(
    args: List[str],
    prefix: Path,
    prerequisites: Optional[Dict[str, Path]] = None,
    jobs: Optional[int] = None,
) -> List[str]:
    """Expand ``{prefix}``/``{jobs}``/``{prereq:<name>}`` placeholders.

    Raises:
        ValueError: if a ``{prereq:<name>}`` placeholder names a prerequisite
            that was not installed (fail fast instead of configuring a library
            without its dependency).
    """
    resolved: List[str] = []
    for arg in args:
        value = arg.replace("{prefix}", str(prefix))
        if jobs is not None:
            value = value.replace("{jobs}", str(jobs))

        def _replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            path = (prerequisites or {}).get(name)
            if path is None:
                raise ValueError(
                    f"Argument {arg!r} references bootstrap prerequisite {name!r}, "
                    "which is not installed in the store"
                )
            return str(path)

        resolved.append(PREREQ_RE.sub(_replace, value))
    return resolved


def default_runner(command: List[str], cwd: Path, log_file: Path) -> None:
    """Run a build command, appending stdout/stderr to ``log_file``.

    The command runs with :func:`clean_env` so FFmpeg build flags and vendor
    variables cannot leak into a toolchain bootstrap.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(command)}  (cwd={cwd})\n")
        handle.flush()
        subprocess.run(
            command,
            cwd=str(cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=True,
            env=clean_env(),
        )


def build_autotools(
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
    configure_args: List[str],
    build_args: List[str],
    install_args: List[str],
    prerequisites: Optional[Dict[str, Path]] = None,
) -> None:
    """Configure/build/install a standard autotools source tree.

    ``prerequisites`` is accepted for symmetry with :func:`build_gcc` and for
    placeholder resolution by the caller; a bootstrap library such as MPFR or
    MPC receives its dependency locations through ``configure_args``
    (e.g. ``--with-gmp={prereq:gmp}``).
    """
    configure = source_dir / "configure"
    if not configure.exists():
        raise FileNotFoundError(f"No configure script in {source_dir}")
    runner(
        ["./configure", f"--prefix={prefix}", *configure_args],
        source_dir,
        log_dir / "configure.log",
    )
    runner(["make", *build_args], source_dir, log_dir / "make.log")
    runner(["make", "install", *install_args], source_dir, log_dir / "install.log")


def build_gcc(
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
    configure_args: List[str],
    build_args: List[str],
    install_args: List[str],
    prerequisites: Dict[str, Path],
) -> None:
    """Build GCC from source, linking GMP/MPFR/MPC from the store when present."""
    args = [
        f"--prefix={prefix}",
        "--disable-multilib",
        "--enable-languages=c,c++",
    ]
    for profile_name, installed_path in prerequisites.items():
        flag = {"gmp": "--with-gmp", "mpfr": "--with-mpfr", "mpc": "--with-mpc"}.get(profile_name)
        if flag is not None:
            args.append(f"{flag}={installed_path}")
    args.extend(configure_args)

    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    runner([str(source_dir / "configure"), *args], source_dir, log_dir / "configure.log")
    runner(["make", *build_args], source_dir, log_dir / "make.log")
    runner(["make", "install", *install_args], source_dir, log_dir / "install.log")


def build_rust_standalone(
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
) -> None:
    """Install a rust-lang standalone tarball into ``prefix``.

    The standalone distribution (``rust-<version>-<target>.tar.gz``) carries
    rustc + cargo (+ rust-docs/clippy) as component subdirectories plus an
    ``install.sh`` installer. Running ``install.sh --prefix=<prefix>`` lays
    out ``bin/`` (rustc, cargo), ``lib/`` and share trees under the prefix,
    matching the profile's ``materialize: {bin_subdir: bin}`` contract. The
    installer runs non-interactively with ``--verbose``; the backtrace and
    manifest components are disabled to keep the install hermetic.
    """
    installer = source_dir / "install.sh"
    if not installer.exists():
        raise FileNotFoundError(f"No install.sh in {source_dir}")
    runner(
        [
            "sh",
            str(installer),
            f"--prefix={prefix}",
            "--verbose",
            "--without=rust-docs",
            "--without=clippy-preview",
        ],
        source_dir,
        log_dir / "install.log",
    )


def install_rust_component(
    source_dir: Path,
    prefix: Path,
    component: str,
    log_dir: Path,
    runner: Runner,
) -> None:
    """Install one rust-installer component (e.g. a target ``rust-std``) into ``prefix``.

    Uses the component archive's own ``install.sh`` (the official installer
    mechanism, no rustup) so the files and the ``lib/rustlib/components``
    manifest land in the same prefix as the base toolchain.
    """
    installer = source_dir / "install.sh"
    if not installer.exists():
        raise FileNotFoundError(f"No install.sh in {source_dir}")
    runner(
        ["sh", str(installer), f"--prefix={prefix}", f"--components={component}", "--verbose"],
        source_dir,
        log_dir / f"install-{component}.log",
    )


def run_build(
    recipe: Optional[str],
    source_dir: Path,
    prefix: Path,
    log_dir: Path,
    runner: Runner,
    configure_args: List[str],
    build_args: List[str],
    install_args: List[str],
    prerequisites: Dict[str, Path],
) -> None:
    """Dispatch a source build recipe."""
    if recipe == "gcc":
        build_gcc(
            source_dir,
            prefix,
            log_dir,
            runner,
            configure_args,
            build_args,
            install_args,
            prerequisites,
        )
        return
    if recipe == "rust_standalone":
        build_rust_standalone(source_dir, prefix, log_dir, runner)
        return
    build_autotools(
        source_dir,
        prefix,
        log_dir,
        runner,
        configure_args,
        build_args,
        install_args,
        prerequisites,
    )


# ----------------------------------------------------------------------
# Post-install verification
# ----------------------------------------------------------------------


def _tool(prefix: Path, name: str) -> Path:
    """Locate a tool in the profile prefix, tolerating the Windows ``.exe`` suffix."""
    candidate = prefix / "bin" / name
    if candidate.exists():
        return candidate
    return prefix / "bin" / f"{name}.exe"


def _run_probe(command: List[str]) -> Optional[str]:
    """Run a probe command; return an error description, or ``None`` on success."""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=clean_env(),
        )
    except OSError as exc:  # pragma: no cover - missing binary / permission issues
        return f"{command[0]}: {exc}"
    if result.returncode != 0:
        output = (result.stdout or "").strip().splitlines()
        detail = output[-1] if output else "no output"
        return f"{command[0]} exited {result.returncode}: {detail}"
    return None


def _verify_compiler(prefix: Path) -> List[str]:
    """Verify a C/C++ compiler toolchain with a real compile-and-link probe."""
    failures: List[str] = []
    cc = _tool(prefix, "gcc")
    cxx = _tool(prefix, "g++")
    for tool in (cc, cxx):
        if not tool.exists():
            failures.append(f"missing {tool}")
    if failures:
        return failures

    for tool in (cc, cxx):
        error = _run_probe([str(tool), "--version"])
        if error:
            failures.append(error)

    with tempfile.TemporaryDirectory(prefix="toolchain-verify-") as temp_dir:
        work = Path(temp_dir)
        c_source = work / "probe.c"
        c_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        cxx_source = work / "probe.cpp"
        cxx_source.write_text("int main() { return 0; }\n", encoding="utf-8")
        error = _run_probe([str(cc), str(c_source), "-o", str(work / "probe-c")])
        if error:
            failures.append(f"C compile/link failed: {error}")
        error = _run_probe([str(cxx), str(cxx_source), "-o", str(work / "probe-cxx")])
        if error:
            failures.append(f"C++ compile/link failed: {error}")
    return failures


def _verify_rust(
    prefix: Path,
    profile: Optional[ToolchainProfile] = None,
    extras: Sequence[ToolchainExtra] = (),
) -> List[str]:
    """Verify rustc and cargo in a rust profile prefix.

    With ``extras`` (target standard libraries) the candidate toolchain is also
    checked as one coherent installation: the compiler release equals the
    declared release, its sysroot is the candidate prefix, and every target std
    is laid out and usable (see :func:`_verify_rust_target`).
    """
    failures: List[str] = []
    for name in ("rustc", "cargo"):
        tool = _tool(prefix, name)
        if not tool.exists():
            failures.append(f"missing {tool}")
            continue
        error = _run_probe([str(tool), "--version"])
        if error:
            failures.append(error)
    if failures or not extras:
        return failures

    rustc = _tool(prefix, "rustc")
    root = prefix.resolve()
    for name in ("rustc", "cargo"):
        real = _tool(prefix, name).resolve()
        if not real.is_relative_to(root):
            failures.append(f"{name} resolves outside the installation: {real}")
    if failures:
        return failures

    env = _rust_probe_env(prefix)
    with tempfile.TemporaryDirectory(prefix="toolchain-verify-") as temp_dir:
        work = Path(temp_dir)
        code, text = _run_capture([str(rustc), "-vV"], env, work)
        release = next(
            (ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("release:")),
            "",
        )
        if code != 0 or not release:
            return [f"rustc -vV failed: {_last_line(text)}"]
        code, text = _run_capture([str(rustc), "--print", "sysroot"], env, work)
        sysroot = Path(text.strip()) if code == 0 and text.strip() else None
        if sysroot is None or sysroot.resolve() != root:
            return [
                f"rustc sysroot {str(sysroot)!r} is not the candidate prefix {root} "
                "(ambient rustc/rustup must not be used)"
            ]
        for extra in extras:
            if release != extra.release:
                failures.append(
                    f"{extra.kind} {extra.target}: rustc release {release} does not match "
                    f"the declared {extra.kind} release {extra.release}"
                )
                continue
            failures.extend(_verify_rust_target(rustc, root, extra, env, work))
    return failures


def _rust_probe_env(prefix: Path) -> Dict[str, str]:
    """Environment in which only the candidate rustc can be used (no rustup/ambient)."""
    env = _probe_env(prefix)
    for name in (
        "RUSTUP_TOOLCHAIN",
        "RUSTUP_HOME",
        "RUSTC",
        "RUSTC_WRAPPER",
        "RUSTC_WORKSPACE_WRAPPER",
        "CARGO_BUILD_TARGET",
        "RUSTC_BOOTSTRAP",
    ):
        env.pop(name, None)
    return env


_COFF_MACHINES = {"x86_64": b"\x64\x86", "aarch64": b"\x64\xaa"}
_RUST_PROBE = (
    "pub fn probe() -> usize {\n"
    "    let mut v: Vec<u32> = Vec::new();\n"
    "    v.push(1);\n"
    '    format!("{}", v.len()).len() + std::mem::size_of::<usize>()\n'
    "}\n"
)


def _verify_rust_target(
    rustc: Path, root: Path, extra: ToolchainExtra, env: Dict[str, str], work: Path
) -> List[str]:
    """Layout, target awareness and a link-free compile probe for one target std."""
    label = f"{extra.kind} {extra.target}"
    sysroot_lib = root / "lib" / "rustlib" / extra.target / "lib"
    if not sysroot_lib.is_dir():
        return [f"{label}: target library directory missing: {sysroot_lib}"]
    if not any(item.is_file() for item in sysroot_lib.glob("libcore-*.rlib")):
        return [f"{label}: no libcore-*.rlib under {sysroot_lib}"]
    if not any(item.is_file() for item in sysroot_lib.glob("libstd-*.rlib")):
        return [f"{label}: no libstd-*.rlib under {sysroot_lib}"]

    code, text = _run_capture(
        [str(rustc), "--target", extra.target, "--print", "target-libdir"], env, work
    )
    if code != 0:
        return [f"{label}: rustc does not accept the target: {_last_line(text)}"]
    if Path(text.strip()).resolve() != sysroot_lib.resolve():
        return [f"{label}: rustc target-libdir {text.strip()!r} is not {sysroot_lib}"]

    source = work / "probe.rs"
    source.write_text(_RUST_PROBE, encoding="utf-8")
    output = work / f"probe-{extra.target}.o"
    code, text = _run_capture(
        [
            str(rustc),
            "--edition",
            "2021",
            "--target",
            extra.target,
            "--crate-type",
            "lib",
            "--crate-name",
            "probe",
            "--emit",
            "obj",
            "-o",
            str(output),
            str(source),
        ],
        env,
        work,
    )
    if code != 0 or not output.exists():
        return [f"{label}: compile probe failed: {_last_line(text)}"]
    arch = extra.target.split("-", 1)[0]
    expected = _COFF_MACHINES.get(arch)
    if "windows" in extra.target and expected is not None:
        if output.read_bytes()[:2] != expected:
            return [f"{label}: probe object is not COFF {arch}"]
    return []


def _verify_library(prefix: Path) -> List[str]:
    """Verify a bootstrap library installed headers and a static/dynamic archive."""
    failures: List[str] = []
    lib_dir = prefix / "lib"
    include_dir = prefix / "include"
    if not include_dir.is_dir():
        failures.append(f"missing headers directory {include_dir}")
    archives: List[Path] = []
    if lib_dir.is_dir():
        archives = sorted(
            item
            for item in lib_dir.iterdir()
            if item.name.startswith("lib")
            and (item.suffix in (".a", ".so", ".dylib") or ".so." in item.name)
        )
    if not archives:
        failures.append(f"no static or shared library found under {lib_dir}")
    return failures


def verify_profile(recipe: Optional[str], prefix: Path) -> List[str]:
    """Verify an installed profile; return a list of failures (empty means usable).

    Recipes are checked with capability probes rather than file presence alone:
    compilers must build and link a real C and C++ program, Rust profiles must
    report a version for rustc and cargo, and bootstrap libraries must provide
    headers plus a library archive.
    """
    if recipe == "gcc":
        return _verify_compiler(prefix)
    if recipe == "rust_standalone":
        return _verify_rust(prefix)
    if recipe == "autotools":
        return _verify_library(prefix)

    binary_dir = prefix / "bin"
    if binary_dir.is_dir() and any(binary_dir.iterdir()):
        return []
    return [f"no binaries installed under {binary_dir}"]


# ----------------------------------------------------------------------
# Declarative (profile-driven) verification
# ----------------------------------------------------------------------

_PE_MACHINES = {"x86_64": "COFF-x86-64"}
_C_PROBE = '#include <stdio.h>\nint main(void) { puts("probe"); return 0; }\n'
_CXX_PROBE = (
    "#include <string>\n" 'int main() { std::string s("probe"); return s.size() == 5 ? 0 : 1; }\n'
)


def _probe_env(prefix: Path) -> Dict[str, str]:
    """Probe environment whose PATH cannot supply the toolchain being verified.

    Only the candidate ``bin`` directory plus the system essentials needed by the
    wrapper scripts are on PATH; tools are always invoked by absolute path.
    """
    env = clean_env()
    env["PATH"] = os.pathsep.join([str(prefix / "bin"), "/usr/bin", "/bin"])
    return env


def _run_capture(command: List[str], env: Dict[str, str], cwd: Path) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(cwd),
        )
    except OSError as exc:
        return 127, f"{command[0]}: {exc}"
    return result.returncode, result.stdout or ""


def _last_line(output: str) -> str:
    lines = output.strip().splitlines()
    return lines[-1] if lines else "no output"


def verify_declared(profile: ToolchainProfile, prefix: Path) -> List[str]:
    """Verify a candidate installation against the profile's ``verify`` contract.

    Runs against ``prefix`` only (a staging directory or an active install):

    1. every required tool exists, is executable and resolves inside ``prefix``;
    2. each version check matches the tool's ``--version`` output;
    3. the compile probe builds (and links) a minimal C and, when declared, C++
       program for the declared Windows target; the output must be a PE/COFF
       image of the declared architecture importing the declared C runtime.

    The probe never executes the produced binary and writes its outputs to a
    temporary directory outside ``prefix``.
    """
    spec = profile.verify
    if spec is None:
        return [f"profile {profile.name!r} declares no verify contract"]

    failures: List[str] = []
    root = prefix.resolve()
    resolved: Dict[str, Path] = {}
    for rel in spec.required_tools:
        candidate = prefix / rel
        if not candidate.exists():
            failures.append(f"missing required tool {rel} under {prefix}")
            continue
        real = candidate.resolve()
        if not real.is_relative_to(root):
            failures.append(f"required tool {rel} resolves outside the installation: {real}")
            continue
        if not (real.is_file() and os.access(real, os.X_OK)):
            failures.append(f"required tool {rel} is not an executable file")
            continue
        resolved[rel] = candidate
    if failures:
        return failures

    env = _probe_env(prefix)
    with tempfile.TemporaryDirectory(prefix="toolchain-verify-") as temp_dir:
        work = Path(temp_dir)
        outputs: Dict[str, str] = {}
        for check in spec.version_checks:
            code, output = _run_capture([str(resolved[check.tool]), "--version"], env, work)
            outputs[check.tool] = output
            if code != 0:
                failures.append(f"{check.tool} --version exited {code}: {_last_line(output)}")
            elif re.search(check.pattern, output) is None:
                failures.append(
                    f"{check.tool} version does not match /{check.pattern}/: "
                    f"{output.strip().splitlines()[0] if output.strip() else 'no output'}"
                )
        if failures:
            return failures
        if spec.compile_probe is not None and profile.target is not None:
            failures.extend(
                _verify_compile_probe(profile, spec.compile_probe, resolved, root, env, work)
            )
    return failures


def _verify_compile_probe(
    profile: ToolchainProfile,
    probe: CompileProbe,
    resolved: Dict[str, Path],
    root: Path,
    env: Dict[str, str],
    work: Path,
) -> List[str]:
    assert profile.target is not None
    target = profile.target
    failures: List[str] = []
    inspector = resolved[probe.inspector]
    expected_format = _PE_MACHINES[target.arch]

    jobs = [("c", probe.cc, "probe.c", _C_PROBE)]
    if probe.cxx is not None:
        jobs.append(("c++", probe.cxx, "probe.cpp", _CXX_PROBE))
    for label, tool, source_name, source_text in jobs:
        source = work / source_name
        source.write_text(source_text, encoding="utf-8")
        output = work / f"probe-{label.replace('+', 'x')}.exe"
        compiler = resolved[tool]
        code, text = _run_capture([str(compiler), str(source), "-o", str(output)], env, work)
        if code != 0 or not output.exists():
            failures.append(f"{label} compile/link probe failed: {_last_line(text)}")
            continue
        head = output.read_bytes()[:4]
        if head[:2] != b"MZ":
            failures.append(f"{label} probe output is not a PE image (magic {head!r})")
            continue
        code, text = _run_capture(
            [str(inspector), "--file-headers", "--coff-imports", str(output)], env, work
        )
        if code != 0:
            failures.append(f"{label} probe inspection failed: {_last_line(text)}")
            continue
        if f"Format: {expected_format}" not in text:
            failures.append(
                f"{label} probe is not {expected_format} (target {target.triple}): "
                f"{next((ln for ln in text.splitlines() if 'Format:' in ln), 'no format line')}"
            )
        if target.runtime == "ucrt" and "api-ms-win-crt-" not in text:
            failures.append(f"{label} probe does not import the UCRT (api-ms-win-crt-*)")

    code, text = _run_capture([str(resolved[probe.cc]), "--version"], env, work)
    installed_dir = next(
        (ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("InstalledDir:")),
        None,
    )
    if installed_dir is not None and not Path(installed_dir).resolve().is_relative_to(root):
        failures.append(f"compiler driver resolves outside the installation: {installed_dir}")
    return failures


def verify_installation(
    profile: Optional[ToolchainProfile],
    source: Optional[ToolchainSource],
    prefix: Path,
    extras: Sequence[ToolchainExtra] = (),
) -> List[str]:
    """Verify ``prefix`` with the profile's declared contract or its recipe.

    ``extras`` are the additive artifacts that must be present and usable in
    this installation (the base toolchain is always verified).
    """
    if profile is not None and profile.verify is not None:
        return verify_declared(profile, prefix)
    if source is not None and source.recipe == "rust_standalone":
        return _verify_rust(prefix, profile, extras)
    return verify_profile(source.recipe if source is not None else None, prefix)
