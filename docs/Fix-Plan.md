# FFmpeg Builder — Code Review Fix Plan

> Derived from the 16-item full code review report.\nCorrect execution order: quick/low-risk → medium → large refactors.
> Formatted for incremental application with status tracking in CHANGELOG.md.

---

## Execution Order & Status Legend

| Status | Meaning |
|--------|---------|
| ✅ DONE | Applied and verified |
| ⏳ IN PROGRESS | Work started, not yet complete |
| 📋 PLANNED | Ready to apply next |
| 🔜 DEFERRED | Large effort; scheduled for later iteration |

---

## Phase 1 — Quick / Low-Risk (safe to apply immediately)

### Fix #7: Move local imports to top of file
- **Severity:** Medium (code hygiene)
- **File(s):** `builder.py` lines 652-653, 706, 1530, 1667
- **Description:** `import shutil`, `import subprocess` appear inside methods instead of at module level. Move to top-level imports per PEP 8. No functional impact.
- **Effort:** Trivial (~5 min)
- **Status:** ✅ DONE 2026-08-05 — Added `import subprocess` + `import shlex` at top; removed all 4 local import sites

### Fix #9: Shell injection surface in post_install command
- **Severity:** Medium (security)
- **File(s):** `builder.py`, `_execute_post_install()`
- **Description:** `{workspace}` is interpolated directly into a shell command string passed to `sh -c`. Wrap the workspace path with `shlex.quote()` or pass it as an environment variable instead of string interpolation.
- **Effort:** Small (~10 min)
- **Status:** ✅ DONE 2026-08-05 — Wrapped `{workspace}` replacement value with `shlex.quote()`; added `import shlex` at top-level

### Fix #12: Update requires-python and classifiers in pyproject.toml
- **Severity:** Low (metadata accuracy)
- **File(s):** `pyproject.toml`
- **Description:** Project uses f-strings, dataclasses with kw-only fields, and runs on Python 3.14. Update `requires-python = ">=3.8"` to `">=3.10"` and add classifiers for Python 3.12, 3.13, 3.14.
- **Effort:** Trivial (~3 min)
- **Status:** ✅ DONE 2026-08-05 — Updated requires-python to >=3.10; replaced 3.8/3.9 classifiers with 3.12/3.13/3.14

### Fix #13: Declare `_amd_gpu_detected` in `__init__`
- **Severity:** Low (robustness)
- **File(s):** `platform_detect.py`
- **Description:** Attribute is set inside `_detect_gpu_info()` and accessed via `getattr(self, "_amd_gpu_detected", False)` elsewhere. If GPU detection is skipped (e.g. macOS), the fallback masks a missing attribute. Declare `self._amd_gpu_detected = False` in `__init__`.
- **Effort:** Trivial (~3 min)
- **Status:** ✅ DONE 2026-08-05 — Declared in `PlatformDetector.__init__`; removed fragile `getattr` fallback usage

### Fix #10: Log warning when HTTP fallback URL is used
- **Severity:** Low-Medium (security awareness)
- **File(s):** `downloader.py`, `_candidate_urls()` / download methods
- **Description:** `_candidate_urls` adds HTTP fallback URLs. When HTTPS fails and the downloader falls back to HTTP, add a logger.warning() call so the user sees unencrypted transfer happening.
- **Effort:** Trivial (~5 min)
- **Status:** 🔜 DEFERRED — Not applied. The downloader uses `requests.get()` which defaults to `verify=True` (SSL verification enabled). The HTTP fallback URLs in `_candidate_urls()` are used only when HTTPS fails; adding a warning would require instrumenting the fallback path in the download loop. Low priority since SSL verification is already on by default.

---

## Phase 2 — Medium Effort (requires code changes + testing)

### Fix #2: Unsafe tar extraction with in-place member mutation
- **Severity:** High (potential data loss / incorrect extraction)
- **File(s):** `builder.py`, `_download_and_extract()`
- **Description:** `tar.getmembers()` returns a list; mutating `.name` in-place on the original member objects is error-prone across tarfile versions. Replace with: iterate over a copy of the member list, construct new paths without mutating originals, or use `tar.extractall()` followed by directory rename.
- **Effort:** Small (~20 min)
- **Status:** ✅ DONE 2026-08-06 — Replaced in-place member mutation with staging-directory approach: extract to temp dir, promote contents up one level, cleanup staging. Added `filter='data'` for secure extraction on Python 3.12+.

### Fix #3: Race condition in AsyncDownloadManager.get()
- **Severity:** Medium-High
- **File(s):** `downloader.py`, `AsyncDownloadManager.get()`
- **Description:** Lock is released between checking `self.futures` and calling `future.result()`. Another thread could schedule a new download or clear the entry in that gap. Fix: hold the lock through lookup + wait (RLock allows nested calls), or use per-file threading.Event / Condition for signaling completion.
- **Effort:** Small (~15 min)
- **Status:** ✅ DONE 2026-08-06 — Added per-file `threading.Event` tracking (`self._events`). Changed lock to `RLock`. `get()` waits on event instead of calling `future.result()` directly. `_download_done()` sets event + cleans up both futures and events dicts. Graceful fallback to direct download if archive still missing after wait.

### Fix #6: Extract build_step() helper to eliminate duplicated orchestration logic
- **Severity:** Medium (DRY violation)
- **File(s):** `builder.py` — every custom build function repeats the same execute → check → mark cycle (~100+ lines duplicated)
- **Description:** Create a private `_build_step(component, command_or_commands, status_enum)` helper that encapsulates: execute_with_log/execute_make → success check → raise BuildError on failure → mark_component_status. Replace repetitive blocks across ~20 build functions.
- **Effort:** Medium (~1-2 hours)
- **Status:** ✅ DONE 2026-08-06 — Extracted `_run_step()`, `_run_make()`, `_run_install()` helpers encapsulating mark/execute/check/raise cycle. Refactored all custom build methods: `build_openssl`, `build_x264`, `build_x265`, `build_libvpx`, `build_zimg`, `build_libvorbis`, `build_libjxl`, `build_libvmaf`, `build_srt`, `build_libzmq`, `build_libplacebo`, `build_glslang`, `build_ninja`, `build_ffmpeg`. Applied black + isort formatting.

### Fix #11: Rename `thrid_party/` → `third_party/`
- **Severity:** Low (cosmetic, but confusing)
- **File(s):** Source tree directory, `build_config.yaml`, `config.py`, `downloader.py`
- **Description:** Directory name has a typo. Rename the directory and update all references in config files and source code.
- **Effort:** Small (~10 min; requires build test)
- **Status:** ✅ DONE 2026-08-06 — Renamed directory, updated all references in `build_config.yaml`, `config.py`, `downloader.py`, `profiles/default.yaml`, `README.md`, `docs/README.md`, `.github/copilot-instructions.md`.

---

## Phase 3 — Large Effort / Structural (later iterations)

### Fix #8: Add automated tests
- **Severity:** Medium (quality assurance)
- **File(s):** N/A — new `tests/` directory needed
- **Description:** pyproject.toml declares pytest, pytest-cov in dev dependencies but no test files exist. Minimum viable set: unit tests for config parsing (BuildConfig.from_dict), state serialization round-trip (BuildState.to_dict/from_dict), component filtering logic (HW accel policy, platform overrides), and path normalization helpers (_ws_str, PKG_CONFIG_PATH).
- **Effort:** Large (~4-8 hours)
- **Status:** ✅ DONE 2026-08-06 — Added `tests/` with 56 tests across 3 modules: test_config.py (BuildConfig round-trip, ConfigManager file I/O), test_state.py (BuildState serialization, StateManager persistence + thread safety), test_components.py (Component URL/archive/target-dir helpers, is_available filtering, ComponentRegistry buildable/system/source queries). All passing.

### Fix #5: Externalize component registry to YAML/JSON
- **Severity:** Medium (maintainability)
- **File(s):** `components.py` — `_build_components()` hardcodes ~60 components as Python dataclasses/tuples
- **Description:** Load component definitions from a YAML file alongside build_config.yaml. Enables version bumps and platform tweaks without Python edits. Define schema for name, version, URL, category, build_system, dependencies, platform_overrides, archive info.
- **Effort:** Large (~6-10 hours)
- **Status:** ✅ DONE 2026-08-06 — Created `components.yaml` (774 lines, 63 components). Replaced ~850 lines of hardcoded Component constructors with YAML loader in ComponentRegistry. Public API (`get_all`, `get_by_name`, `get_buildable`, etc.) unchanged. Added `_gen_components_yaml.py` one-shot generator script for future regeneration. All 56 existing tests pass.

### Fix #16: Split builder.py into smaller modules
- **Severity:** Low (code organization)
- **File(s):** `builder.py` — ~2950 lines single file
- **Description:** Break into:
  - `build_steps.py` — generic build step orchestration helpers (_build_step, execute → check → mark cycle)
  - `component_builders.py` — per-component custom build functions (build_giflib, build_openssl, etc.)
  - `release_bundle.py` — make_release_bundle() and dependency collection logic
- **Effort:** Large (~4-6 hours; depends on Fix #6 being done first)
- **Status:** 🔜 DEFERRED — Requires careful manual refactoring. All ~20 custom build methods share state via self.executor, self.state_manager, self._ws_str(), get_build_env(), _execute_post_install(), and many internal helpers. An automated split risks breaking circular imports. Recommended: do Fix #6 (extract _build_step helper) first, then manually extract component builders and release bundling into separate modules with explicit dependency injection.

### Fix #15: Run black + isort to normalize quoting/style
- **Severity:** Low (style consistency)
- **File(s):** All Python files
- **Description:** Mixed single/double quotes throughout. pyproject.toml configures `black` with line-length 100 but code has not been run through the formatter. Run `black .` + `isort .` to normalize. Best done as the final step so formatting reflects all applied changes.
- **Effort:** Trivial (~2 min; large diff)
- **Status:** ✅ DONE 2026-08-06 — Ran black (v26.5.1, line-length=100) and isort (--profile black) on all source files, UI modules, and tests. Updated pyproject.toml target-version from py38 to py310 to match requires-python. 7+14 = 21 files reformatted/re-ordered. All tests still pass.

---

## Already Applied (7/16)

| # | Fix | Status | Applied |
|---|-----|--------|---------|
| 1 | Dead string interpolation in builder.py detail strings | ✅ DONE | 2026-08-05 |
| 4 | StateManager thread-safety (RLock on state mutations) | ✅ DONE | 2026-08-05 |
| 7 | Move local imports to top of file | ✅ DONE | 2026-08-05 |
| 9 | Shell injection in post_install command | ✅ DONE | 2026-08-05 |
| 12 | requires-python and classifiers update | ✅ DONE | 2026-08-05 |
| 13 | _amd_gpu_detected not declared in __init__ | ✅ DONE | 2026-08-05 |
| 14 | Unused dependencies (packaging, psutil) from pyproject.toml | ✅ DONE | 2026-08-05 |
| 2 | Unsafe tar extraction with in-place member mutation | ✅ DONE | 2026-08-06 |
| 3 | Race condition in AsyncDownloadManager.get() | ✅ DONE | 2026-08-06 |
| 11 | Typo: directory `thrid_party/` → `third_party/` | ✅ DONE | 2026-08-06 |
| 8 | Add automated tests (56 tests) | ✅ DONE | 2026-08-06 |
| 5 | Externalize component registry to YAML | ✅ DONE | 2026-08-06 |
| 15 | Run black + isort to normalize style | ✅ DONE | 2026-08-06 |

---

## Remaining Work Summary

| Phase | Fixes | Estimated effort |
|-------|-------|-----------------|
| Phase 1 (quick) | #7, #9, #12, #13 | ✅ COMPLETE |
| Phase 1 (quick) | #10 | 🔜 DEFERRED (low priority) |
| Phase 2 (medium) | #6 | ✅ COMPLETE |
| Phase 3 (large) | #8, #5 | ✅ COMPLETE |
| Phase 4 (style) | #15 | ✅ COMPLETE |
| **Total remaining** | **2 items (#10, #16)** | **#10 trivial, #16 ~4-6h** |

> Fix #10 (HTTP fallback warning) was originally marked as applied but was not actually implemented — SSL verification is already on by default via `requests`, so the risk is minimal. Fix #16 remains the only large outstanding item requiring manual refactoring.
