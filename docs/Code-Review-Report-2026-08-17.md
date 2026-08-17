# FFmpeg Builder — Code Review Report (2026-08-17)

Full review of all source modules, the component registry, configuration, scripts, and docs.
Follow-up to the 16-item review tracked in `docs/Fix-Plan.md`.

**Method:** static review of every `.py` module, `components.yaml`, `build_config.yaml`,
`pyproject.toml`, tests, and setup scripts; git-history cross-checks for regressions;
empirical verification on this machine (Windows 11, MSYS2 UCRT64 at `C:\msys64`,
Python 3.12.10) for the UCRT64 PATH handling and component availability logic.

**Verification commands (all run):**

```
pytest tests/ -q          -> 61 passed in 1.35s
mypy <17 source modules>  -> 33 errors (pre-existing baseline, see L2)
black --check .           -> 3 files would be reformatted (see L3)
```

---

## 1. Prior 16-item review — status audit

`docs/Fix-Plan.md` claims 14/16 done, #10 and #16 deferred. Audit result:

| # | Fix | Fix-Plan status | Actual status |
|---|-----|-----------------|---------------|
| 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15 | — | ✅ DONE | ✅ Verified present in code |
| 10 | HTTP fallback warning | 🔜 DEFERRED ("not applied") | **Implemented** — `downloader.py:109-114` logs a warning when a candidate URL falls back to `http://`. Fix-Plan.md is stale. |
| 16 | Split builder.py | 🔜 DEFERRED | **Partially done** — `build_steps.py` (run_step/run_make/run_install), `component_builders.py` (custom-builder registry), `release_bundle.py` (release bundling) exist, with `tests/test_builder_split.py` guarding the split surface. The ~20 `build_*` component functions still live in `builder.py` (2777 lines). Fix-Plan.md is stale. |

Note: Fix #2's completion note says `filter='data'` was added "for secure extraction on
Python 3.12+" — that version dependency was not guarded (see H3).

---

## 2. New findings

### HIGH

#### H1 — libplacebo `linux_only: true` in `components.yaml` silently drops libplacebo on macOS (regression)

`components.yaml:709-710`:

```yaml
- name: libplacebo
  ...
  linux_only: true
  windows_ucrt64_supported: true
```

Empirically verified (probe against the live registry):

```
platform=linux:   is_available=True,  in get_buildable=True
platform=darwin:  is_available=False, in get_buildable=False   <-- regression
platform=windows: is_available=True,  in get_buildable=True
```

Evidence this is a regression, not intended policy:

- `docs/CHANGELOG.md` Stage 3 (2026-08-01) explicitly records: *removed the `linux_only`
  restriction from the libplacebo component*, followed by a verified macOS build on 2026-08-02
  with `--enable-libplacebo` and Vulkan.
- Git: the pre-YAML hardcoded registry (`df616c6`, origin/master) had **no** `linux_only` on
  libplacebo (it did have it on nv-codec/amf/opencl-*/onevpl, all of which the YAML preserves).
  The flag was re-introduced in `5ba170f` (2026-08-06, YAML externalization) and the
  `b4b0191` merge (2026-08-09) kept the YAML side.
- `components.py:337-339` still comments "Always include; -Dvulkan= flag is resolved in
  build_libplacebo()", and `build_libplacebo`/`_find_macports_clang` retain full macOS
  (darwin) code paths that are now unreachable.

**Impact:** macOS builds no longer build libplacebo; `--enable-libplacebo` is never passed to
FFmpeg configure; all macOS libplacebo build code is dead.

**Fix:** delete `linux_only: true` from the libplacebo entry (the `windows_ucrt64_supported`
key then becomes a no-op for it and can also be removed).

#### H2 — `_setup_environment` PATH merge is broken on Windows UCRT64

`builder.py:446`:

```python
"PATH": f"{self._ws_str()}/bin:{os.environ.get('PATH', '')}",
```

The separator is hardcoded `:` (POSIX), but under MSYS2 UCRT64 the inherited `PATH` is a
Windows-style `;`-separated list. Empirically verified on this machine
(`MSYSTEM=UCRT64 /ucrt64/bin/python`):

- Merged value becomes `E:/.../workspace/bin:C:\msys64\usr\local\bin;C:\msys64\ucrt64\bin;...`
- Entry 0 (`E:/.../workspace/bin:C:\msys64\usr\local\bin`) is a **single invalid entry**
  (`os.path.isdir` → False) and the original first PATH entry is swallowed into it.
- `shutil.which("make")` still resolves only because `make` sits in a surviving `;` entry.

**Impact:**

1. The `workspace/bin` prepend is ineffective for native Windows tools — cmake/ninja/meson/
   pkg-config are PE binaries located via the surviving `;` entries. If any of them is ever
   built from source into `workspace/bin` on Windows (the registry supports it), it will be
   invisible to the build, which silently falls back to system versions or fails.
2. The user's first PATH entry is destroyed for every child process.

**Fix:** use `os.pathsep` and only prepend an existing directory:

```python
path = os.environ.get("PATH", "")
ws_bin = f"{self._ws_str()}/bin"
self.env["PATH"] = f"{ws_bin}{os.pathsep}{path}" if os.path.isdir(ws_bin) else path
```

#### H3 — `tar.extractall(..., filter="data")` requires Python ≥ 3.12, but the package declares ≥ 3.10

`builder.py:996` and `builder.py:1014` pass `filter="data"` (PEP 706, added in Python 3.12).
`pyproject.toml` declares `requires-python = ">=3.10"` (set by Fix #12). On Python 3.10/3.11
the first archive extraction raises `TypeError: extractall() got an unexpected keyword
argument 'filter'`.

The current dev environments (3.13 MSYS2, 3.14 Fedora) are unaffected, but any user on a
declared-supported interpreter crashes on the first component download.

**Fix:** either bump `requires-python` to `>=3.12`, or guard:

```python
if sys.version_info >= (3, 12):
    tar.extractall(staging, filter="data")
else:
    tar.extractall(staging)
```

### MEDIUM

#### M1 — `build_ffmpeg` mutates `self.extralibs` / `self.ldflags`; flags accumulate on retry

`builder.py:2587-2667` appends to the instance attributes (`self.extralibs += " -lvulkan"`
at 2641, `self.ldflags += " -Wl,-rpath,/usr/local/lib"` at 2648, etc.) and then reads them
back at 2666-2667. The UI retry path (`app.py:243-250`) re-runs `build_component` on the
**same** builder instance after a failed component, so a retried FFmpeg build receives
duplicated `-l`/rpath entries. Harmless in most cases but a latent statefulness bug.
**Fix:** build local copies inside `build_ffmpeg` instead of mutating instance state.

#### M2 — meson component declares `build_system: autotools`; source fallback is broken

`components.yaml:110-118`: meson is a system component (`system_tool_name: meson`). If the
system meson is absent and the source fallback runs, it executes `./configure` — but
`meson-1.8.2.tar.gz` contains **no** `configure` script (verified in the archive). The
fallback can never succeed. **Fix:** give meson a `custom_build_fn` (meson self-bootstraps:
`python meson.py setup ... && python meson.py install`).

#### M3 — `_build_cargo` runs `cargo install cargo-c` on every build

`builder.py:1272-1298`: no presence check for `cargo-c`; every rav1e build re-installs and
re-compiles cargo-c (network + minutes), violating the offline-first principle.
**Fix:** `shutil.which("cargo-c")` first; install only when missing.

#### M4 — `setup_windows_msys2_ucrt64.ps1` package list is missing `perl`

The committed default `build_config.yaml` has `gpl_enabled: true`, which puts the openssl
source build (`build_system: custom`, `custom_build_fn: build_openssl`, `gpl_only: true`)
into the build set. OpenSSL's `./Configure` is a Perl script. The script's package list
(`scripts/setup_windows_msys2_ucrt64.ps1:116-158`) installs no `perl` package (and
`base-devel` does not provide it), so on a fresh UCRT64 environment the openssl build fails
with a bad-interpreter error. This machine happens to have `/usr/bin/perl` from a manual
install. **Fix:** add `perl` to the package list (or document it as a prerequisite).

#### M5 — make/install steps have no timeout

`executor.py:160-200`: `execute_make` and `execute_install` do not accept or pass a
`timeout`, even though `execute()`/`execute_with_log()` support one. All `make -jN` and
`make install` steps (8 call sites in `builder.py`) run unbounded; a hung parallel build
blocks the whole session with no way to intervene short of killing the process.
**Fix:** thread a configurable timeout through `run_make`/`run_install` → `execute_make`/
`execute_install`.

#### M6 — `BuildConfig.from_dict` crashes on empty YAML and on unknown keys

`config.py:63-77` + `config.py:100-101`: an empty `build_config.yaml` makes
`yaml.safe_load` return `None` → `AttributeError: 'NoneType' object has no attribute 'get'`.
Any unknown top-level key → `TypeError` from `cls(**config_data)` (no filtering). Both
crash the app at startup with an unhelpful traceback. **Fix:** `data = data or {}` and
filter `config_data` to known fields (or warn-and-ignore unknown keys).

#### M7 — `profiles/default.yaml` is dead and broken

No code references `profiles/` anywhere (0 matches in all `.py` files). And it cannot be
loaded as-is: `macos: {clang: ..., openmp: true}` → `MacOSConfig(**...)` raises
`TypeError` because `MacOSConfig` only has a `clang` field (`config.py:10-14`); `openmp`
is a top-level `BuildConfig` field. **Fix:** delete the file, or wire it up and fix the
schema mapping.

#### M8 — README-documented CLI options do not exist

The README documents `--help`, `--workspace`, `--config` options; there is no `argparse`
(or equivalent) anywhere in the codebase — `python -m ffmpeg_builder` takes no arguments
(`app.py:58` `run()`). **Fix:** implement the options or remove them from the README.

#### M9 — No integrity verification of downloaded archives

`components.yaml` has no checksum field and `downloader.py` verifies nothing beyond
download success (atomic `.part` rename only). A corrupted or MITM'd archive (HTTP fallback
is explicitly supported, see Fix #10) is extracted and built. **Fix:** add optional
`sha256` to the registry schema and verify in `downloader.py`.

#### M10 — `build_giflib` is dead code

`builder.py:1370` + the `build_giflib` entry in `component_builders.py`: `build_component`
intercepts giflib as a system component unconditionally (`builder.py:694-711`) **before**
any custom-build dispatch, so `build_giflib` is unreachable. **Fix:** remove the function
and its registry entry.

#### M11 — waflib is a dead registry entry

`components.yaml:337-344` registers waflib as `build_system: headers_only`, but
`_install_headers_only` (`builder.py:1325-1368`) has branches only for VapourSynth,
fast-float, and amf — waflib falls through with **no** branch (silent no-op: the component
is marked COMPLETED without installing anything). Additionally, no component lists waflib
in `depends_on` (0 references outside its own entry). **Fix:** remove the waflib entry, or
add the missing install branch if it is actually needed.

### LOW

- **L1 — Local imports remain** (Fix #7 covered only `builder.py`): `app.py:285-286`
  (`shutil`, `stat` in `_cleanup`), `ui/screens.py:476-477` (`platform`, `shutil` in
  `_install_binaries`), `system_report.py` (`os` inside `generate`), `downloader.py`
  (`logging` inside the download loop), `ui/error_handler.py` (`_show_help` imports
  `.screens._ERROR_KEYS`).
- **L2 — mypy: 33 errors** across the 17 source modules: missing `yaml`/`tqdm` stubs
  (add `types-PyYAML`, `tqdm` to dev deps or inline ignores); untyped defs
  (`__main__.py:9`, `app.py:289`, `builder.py:35`, `platform_detect.py:114`);
  `build_steps.py` `BuildStepContext` declares `executor: object` / `state_manager: object`
  (attr errors at call sites); `platform_detect.py:635-637` variable-reuse inference;
  `system_report.py:146-155` `Dict[str, str | None]` mismatch.
- **L3 — black: 3 files would reformat**: `tests/test_builder_split.py`,
  `component_builders.py`, `release_bundle.py` (all added by the post-Format split work).
- **L4 — `build_all()` is dead code** (`builder.py:640`): defined, never called — the app
  drives its own loop (`app.py:198-258`). Remove or use it.
- **L5 — `_cleanup` deletes packages twice** (once inside `workspace`, once as a child of
  it) — redundant rmtree work.
- **L6 — `FinalReportScreen._install_binaries` on Windows** looks for `ffmpeg` without the
  `.exe` suffix (`ui/screens.py:470+`) → the release-binary install silently copies nothing
  on Windows.
- **L7 — `app.py:105` catch-all** in `run()` swallows the traceback (prints message only);
  `ConfigScreen` `int(workers)` can raise `ValueError` and crash the app on bad input.
- **L8 — Stale docs** (beyond the Fix-Plan items in §1):
  - `components.yaml` header and `components.py:231` error message reference
    `_gen_components_yaml.py`, which does not exist in the repo.
  - `docs/DeveloperReadme.md` UCRT64 HW-accel policy list omits onevpl/libplacebo.
  - Root `changelog.md:10` (2026-08-17 entry) records a successful **FFmpeg 9.0** build,
    but the committed registry/config target **8.1** and no uncommitted version bump exists
    (the only uncommitted change is `enable_libvmaf_cuda`) — either the version bump was
    reverted after the build or the entry is a typo.
- **L9 — `release_bundle.py:91`** `queue.pop(0)` is O(n²) over the dependency queue (use a
  `deque`); flat-copy name collisions are skipped silently.
- **L10 — `components.py:313`** hardcodes `comp.name not in ("rav1e",)` to bypass the
  `requires_tools` check; `is_available`'s `tools: Dict` is untyped and uses an anonymous
  class as a missing-tool sentinel.
- **L11 — Non-atomic writes**: `config.py:119` and `state.py:138` `save()` write directly
  with `open(..., "w")` — a crash mid-write corrupts the config/state file (the downloader
  already does this correctly with `.part` + rename).
- **L12 — libplacebo YAML `configure_args`** still contain static `-Dvulkan=enabled
  -Dglslang=enabled` (`components.yaml:696-697`) although Stage 3 moved Vulkan to a
  runtime decision; currently masked because `build_libplacebo` appends its own args later.
- **L13 — Minor**: cmake component's `CXXFLAGS_EXTRA` is likely a no-op (cmake reads
  `CXXFLAGS` only in some generators); `get_num_jobs` does unvalidated `int()` on config.

---

## 3. Positives

- **61/61 unit tests pass**; test coverage of config/state/components/split-surface is
  solid and the split is guarded by `test_builder_split.py`.
- Thread-safety fixes from the prior review are correctly in place: `StateManager` RLock,
  per-file `threading.Event` completion signaling in `AsyncDownloadManager`, dashboard RLock.
- Atomic `.part` + rename downloads; staged tar extraction to a temp dir before promotion.
- The UCRT64 sh-wrapping rule in `executor.py:71-84` (wrap only `./` commands; `.py`
  scripts run via `sys.executable`; PE binaries called directly) is exactly right and matches
  the documented 2026-08-17 incident.
- Declarative component registry with `platform_overrides` / `skip_condition` /
  `custom_build_fn` keeps per-component branching out of the builder core.
- Thorough, honest documentation (changelogs record failures and fixes, verified
  environments are listed with hardware).

---

## 4. Recommended priority

1. **H1** — one-line YAML fix; restores the documented, previously-verified macOS
   libplacebo build.
2. **H2** — small `builder.py` fix; prevents a class of silent tool-resolution failures on
   the primary Windows backend.
3. **H3** — one-line guard or metadata bump; unblocks 3.10/3.11 users.
4. **M4** — add `perl` to the setup script so fresh UCRT64 + default config actually works.
5. **M6/M7** — startup robustness (empty/unknown config keys, dead profile file).
6. **M1/M2/M3/M5/M9** — build-correctness hardening.
7. **L8** — sync the docs (Fix-Plan statuses, `_gen_components_yaml.py`, changelog 9.0 line)
   so the next reviewer isn't misled.
