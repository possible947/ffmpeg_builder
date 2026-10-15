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
