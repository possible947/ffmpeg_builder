# Testing Plan — Build System Modernization

This document defines how the toolchain-modernization phases are validated. It is
maintained alongside `docs/CHANGELOG.md`; each phase adds its own checklist here.

## Constraints

- The development container used for these changes has **no Python interpreter**,
  so `pytest`, `black`, and the mypy baseline cannot be run there. Code is written
  to be black-clean (line length 100) and mypy-clean by inspection, and the
  automated suite is run on the target machines below.
- A full FFmpeg build is long and hardware-dependent; component-level changes are
  verified with the unit suite, and the end-to-end build is verified per platform.

## Environment order (manual end-to-end runs)

1. **macOS** — least problematic; validates the general path first.
2. **Linux + GCC 16** (e.g. Fedora 44) — validates system-toolchain resolution and
   the FFmpeg 9.0 path.
3. **Linux + GCC 12 via WSL** — validates the deployed/lower-profile path for
   FFmpeg 8.1 on a modern host.
4. **Windows (MSYS2 UCRT64)** — deferred until hardware access is restored; only
   the FFmpeg 8.1 path was exercised previously.

## Automated tests (run on the target machine)

```
pytest tests/ -q                                   # unit suite
black --check .                                    # formatting (line length 100)
python scripts/check_mypy_baseline.py              # type baseline
```

`mypy_baseline.txt` is a ratchet; refresh deliberately with `--update` only when
new typed surface is intentional.

## Per-phase checklists

### Phase 0 — Toolchain registry foundation
- [ ] `pytest tests/test_toolchain_registry.py tests/test_components.py -q`.
- [ ] `ToolchainRegistry()` loads the shipped `toolchains.yaml` (profiles,
      platform profiles, CUDA map) without error.
- [ ] Invalid registries (unknown kind/platform/source type, missing `sha256`,
      bad `source.default`, unknown references, bootstrap cycle) fail fast.
- [ ] `Component.toolchain` defaults to `"n/a"`; `components.yaml` `nv-codec`
      driver map targets only declared versions.
- [ ] No build behavior changed (full `pytest tests/`).

### Phase 1 — Resolution and plan (read-only)
- [ ] `pytest tests/test_toolchain_registry.py tests/test_screens.py -q`.
- [ ] Resolver: system tool **in range** → `system`; **out of range** → deploy
      `fallback_toolchain`; **out of range with no fallback** → unresolved
      (`None`); **missing system version** → treated as out of range.
- [ ] Explicit component `toolchain: <name>` → deploy that profile; `"n/a"`
      inherits the platform-profile effective toolchain.
- [ ] `plan_component_toolchains()` returns one entry per component.
- [ ] Start screen `t` (Toolchain plan) renders the resolved plan with the
      current config/platform; no deploy occurs.
- [ ] Manual smoke: on macOS, plan shows `system` for both FFmpeg 8.1/9.0; on
      Linux+GCC16, FFmpeg 8.1 shows `deploy gcc12` and 9.0 shows `system`.
- [ ] No build behavior changed.

### Phase 2 — Toolchain preparation mode
- [ ] Persistent store layout, locks, independent state, path guards.
- [ ] Build (default) and alternative acquisition variants selectable in the TUI.
- [ ] `gcc12` builds locally from source using the system compiler; GMP/MPFR/MPC
      bootstrap resolved from pinned archives.
- [ ] Deploy/delete/clean profile actions.

### Phase 3 — Effective environment in the build
- [ ] Resolved toolchain injected into per-component env (explicit `CC`/`CXX`).
- [ ] `ffmpeg_policy` removed/replaced by the platform-profile range check.
- [ ] Auto-substitution removed (conda-nvcc, `prefer_system` fallbacks, driver
      nv-codec selection → `driver_nvenc_api_map`).
- [ ] Missing toolchain → skip component (error only for `rav1e`).
- [ ] FFmpeg 8.1 builds on Linux+GCC16 via deployed `gcc12`; 9.0 uses system GCC.

### Phase 3b — Toolchain-gated skip and CUDA-table consumption (done 2026-10-15)
- [x] CUDA Toolkit version detected (`nvcc --version` → `PlatformInfo.cuda_version`).
- [x] Per-component toolchain gate in `get_buildable()` (`toolchain_checker`);
      unavailable explicit profiles excluded, `rav1e` always kept and errors
      (`BuildError`, never `SkipComponent`) on missing/too-old rustc.
- [x] `cuda` table consumed: store-installed `CC` probed as `-ccbin`
      (`ToolchainStore.cuda_host_compiler()`); unmapped/absent → `None`.
- [x] Toolchain plan shows per-component resolution + "CUDA host" for `nv-codec`;
      Component info shows per-component skip reasons.
- [ ] Remaining (needs staged pins + Linux runs): toolchain pins
      (`gmp`/`mpfr`/`mpc`/`gcc12`/`gcc13`/`rust`/`cargo-c`, Git LFS archives);
      real bootstrap on Linux+GCC16; FFmpeg 8.1 end-to-end with deployed `gcc12`.
      See Phase 3c for the provisioning/portability work this depends on.

### Phase 3c — Toolchain provisioning and portability (next)

Goal: every declared profile provisions successfully, is verified after
installation, and is usable on each supported Linux-family backend (native Linux
and WSL2), either by building on the target or by deploying a prebuilt archive.

Known blockers found in the current code (fix before running a real bootstrap):

- [x] **Bootstrap prerequisites are not plumbed to autotools profiles.**
      `toolchain_builders.build_autotools()` ignores `prerequisites`, and
      `run_build()` only forwards them to `build_gcc()`. `mpfr` requires
      `--with-gmp` and `mpc` requires `--with-gmp --with-mpfr`, so as pinned
      today neither can link the store GMP/MPFR. Fix: pass prerequisites into
      the autotools recipe and support a `{prereq:<name>}`-style placeholder in
      the registry's `configure_args` (the registry already permits
      `configure_args`/`build_args`/`install_args` on a `build` variant,
      `toolchain_registry.py:502-514`).
- [x] **No parallel jobs for toolchain builds.** `build_args` is empty for every
      shipped profile, so `make` runs serially (a GCC bootstrap is hours serial,
      ~30-90 min with `-j`). Fix: a `{jobs}` placeholder resolved from the same
      policy as the FFmpeg build (`PlatformDetector.get_num_jobs`,
      `platform_detect.py:1545`).
- [x] **No post-install verification.** Nothing checks that an acquired profile
      actually works, so a half-built GCC is recorded as `installed`. Fix: a
      verify step per recipe (gcc: `gcc --version` + a C and C++ compile/link
      hello; rust: `rustc --version` + `cargo --version`; bootstrap libs: the
      expected `lib/` archives and headers exist), stamped into the store state,
      with `ToolchainStore.component_env()` refusing unverified profiles.
- [x] **Clean bootstrap environment.** Bootstrap must not inherit FFmpeg build
      flags (`CFLAGS`/`CXXFLAGS`, CUDA include paths, `-march=native`,
      `-fopenmp`) — see the risk table in `docs/toolchain_analysis_report.md`.
- [x] **Headless provisioning entry point.** The store is only reachable through
      the TUI today; add a script/module entry so a full provisioning run is
      repeatable and CI-able (`scripts/`), reusing `ToolchainStore.acquire()`.

Portability:

- [ ] Decide and document the model: build-on-target (offline, slow) vs prebuilt
      `source.binary` archives published per `(profile, platform, arch)` with
      `sha256`, keeping `build` as the fallback. The registry already supports
      `binary`/`download` variants, so this is data plus a distribution step.
- [ ] Make the installed prefix relocatable, or fix it. GCC bakes `--prefix`
      into its search paths; acceptance is: move `toolchains/` to another
      absolute path, re-run verification, and a probe compile still links.
      Verify whether the pinned GCC supports a relocation option before relying
      on it; otherwise require a stable store root and document it.
- [ ] Artifact identity per platform/arch: `installed/<profile>-<version>-<platform>-<arch>`
      so two targets can coexist; extend `ToolchainProfile.platforms` or the
      profile naming if an arch dimension is needed.
- [ ] WSL2 backend: treat as `linux` for ELF compatibility, but verify the store
      lives on an ext4 filesystem (fcntl locking is unreliable on `/mnt/*`),
      and that symlink/permission handling in extraction is correct.
- [x] Native Linux run: provision `gmp → mpfr → mpc → gcc12` and `rust` on the
      GCC 16 host, then build FFmpeg 8.1 with the deployed `gcc12` and FFmpeg
      9.0 with the system GCC (Phase 3 acceptance). `gmp 6.3.0`, `mpfr 4.2.1`,
      `mpc 1.3.1`, `rust 1.99.0`, and `gcc12 12.5.0` are installed and verified;
      **FFmpeg 8.1 end-to-end with deployed gcc12 is done (58/58, 2026-10-16)**:
      `built with gcc 12.5.0`, 18/18 expected encoders, `-march=native
      -mtune=native -fopenmp`, smoke encode passed, release pulls nothing from
      the toolchain store (static workspace libs, host libgomp/libgcc_s).
      **FFmpeg 9.0 end-to-end with the system GCC is done (58/58, 2026-10-16)**:
      stale `workspace_90` removed, `built with gcc 16`, same 58-component
      composition (all `system` except `rav1e` on store rust), 18/18 encoders,
      same flags, smoke encode passed, same static release shape. Both
      converter halves are built: `workspace_81` + `workspace_90`.
- [ ] CI: a long-running, manually triggered toolchain job (nightly or
      `workflow_dispatch`); the unit suite keeps using recording runners.
      (Provisioning workflow created: `.github/workflows/toolchains.yml`.)

### Backlog — cargo-c into the store (after Phase 3c)

- [ ] Give the `cargo-c` profile a real acquisition source so
      `builders/base.py`'s implicit `cargo install cargo-c --version 0.10.25`
      (`builders/base.py:366-378`) disappears: either a `binary`/`download`
      archive or a `cargo`-based recipe with `CARGO_HOME` inside the store
      (vendored crates for a true offline run), bootstrapped after `rust`.
- [ ] Then `build_cargo()` resolves `cargo-c` from the store env
      (`PATH_PREPEND`) and never reaches the network, restoring the
      offline-first contract flagged in `docs/toolchain_analysis_report.md`.

### Phase 4 — Release and ABI/AppImage tail
- [ ] Release bundles copy all non-system runtime deps (including toolchain-prefix
      runtime); glibc/loader stay host.
- [ ] Two autonomous releases (`8.1`, `9.0`) consumable by the base project.
- [ ] Linux `$ORIGIN` rpath applied if non-system dynamic deps remain.
- [ ] AppImage packaging succeeds on Linux.

### Phase 5 — Cleanup and documentation
- [ ] Nix (`shell.nix`) and the conda fallback removed.
- [ ] Docs updated; CI green (pytest + black + mypy baseline).

### Phase 6 — Windows (deferred)
- [ ] Platform profile for `windows-msys2-ucrt64`; re-validate 8.1/9.0.
