# Changelog

## Unreleased

### Added

- **Этап 1: Перенос стандартных раннеров систем сборки в `builders/base.py`** — реализации функций `build_autotools`, `build_cmake`, `build_meson`, `build_make_only`, `build_cargo`, `get_rustc_version`, `install_headers_only` и `_rmtree` перенесены из [builder.py](builder.py) в [builders/base.py](builders/base.py). В классе `FFmpegBuilder` оставлены методы-делегаты для сохранения 100% обратной совместимости.
- **Этап 2: Перенос 15 кастомных сборщиков компонентов в доменные модули `builders/`** — вся императивная логика сборки компонентов (`x264`, `x265`, `libvpx`, `zimg`, `libvorbis`, `libjxl`, `openssl`, `glslang`, `libplacebo`, `libvmaf`, `srt`, `libzmq`, `ninja`, `meson`, `ffmpeg`) вынесена из [builder.py](builder.py) в соответствующие подпакеты `builders/` (`codecs`, `crypto`, `graphics`, `network`, `tools`, `ffmpeg`). В `FFmpegBuilder` сохранены вызовы-делегаты.

### Changed
- **Phase 2 concrete platform implementations** — added `MacOSPlatform`, `LinuxGcc13Platform`, `LinuxGcc15Platform`, and `WindowsUcrt64Platform`, plus a resolver that picks the correct strategy for the detected OS/toolchain.
- **Phase 3 declarative source patch engine** — added the `patches/` package with version- and component-aware `SourcePatch` definitions, fail-fast assertion helpers, and concrete C23, C++ header, Darwin, and FFmpeg 9 patch implementations.
- **Phase 4 domain-specific builders** — added the `builders/` package, `ComponentBuildContext`, standard build-system entry points, and codec, crypto, graphics, network, tool, and FFmpeg builder modules.
- **Phase 5 orchestration boundary** — centralized custom and standard build selection in `builders.base.dispatch_component_build()` while preserving the existing `FFmpegBuilder` API and lifecycle behavior.

### Changed

- **Compiler metadata detection** — `PlatformDetector` now exposes `gcc_major_version` and `is_c23_default`, so GCC 13 and GCC 15+ default-mode differences are explicit and testable.
- **Builder integration** — `FFmpegBuilder` now resolves a platform strategy from `PlatformContext` and uses it for path normalization and environment setup.
- **Phase 2 stabilization** — tightened the strategy resolver for minimal mock platform objects, normalized Linux/MSYS2 path detection to POSIX-style values for detector diagnostics, and added the Windows-safe fallback for macOS release-bundle symlink alias handling.
- **Patch lifecycle integration** — `FFmpegBuilder` now applies registered source patches immediately after extraction and before configure/build steps, preserving clear failures when an upstream patch anchor changes.
- **Builder dispatch integration** — custom component functions and standard build-system lifecycle calls now resolve through the modular `builders/` boundary while preserving the existing `FFmpegBuilder` callable API.

### Verified

- `pytest tests/test_platform_detect.py tests/test_builder_split.py -q` passes with the target detector and release-bundle regression suite.
- `pytest tests/test_platform_strategy.py -q` passes with the focused Phase 1/2 regression suite.
- `pytest tests/test_patches.py tests/test_builder_split.py -q` passes with 23 tests.

## 2026-09-07 — Intel QSV: исправлен порядок детектирования VAAPI/QSV

На Fedora Linux 44 с Intel Arc A750 (iHD-драйвер, VAAPI работает) сборка FFmpeg 8.1 не включала поддержку Intel QSV, хотя оборудование и драйверы были в порядке.

- **`PlatformDetector.detect_all()` вычислял `qsv_available` до `vaapi_available`** — `_check_qsv()` на Linux требует `platform_info.vaapi_available == True`, прежде чем проверять `vainfo`/PCI ID на Intel GPU, но на момент вызова `vaapi_available` ещё хранил дефолт датакласса `False`, поэтому `_check_qsv()` всегда возвращал `False` независимо от реального оборудования. `ComponentRegistry` ([components.py](components.py)) пропускает сборку компонента `onevpl`, если `qsv_available` ложно, поэтому `--enable-libvpl` никогда не попадал в конфигурацию FFmpeg, и QSV-кодеки молча отсутствовали в собранном бинарнике. Исправлено переносом вызова `_check_vaapi()` перед блоком детектирования CUDA/QSV в [platform_detect.py](platform_detect.py).

Проверено: на Fedora 44 с Intel Arc A750 `qsv_available` теперь корректно возвращает `True`; `onevpl` собирается; итоговый бинарник FFmpeg 8.1 содержит `--enable-libvpl`, кодеки `h264_qsv`/`hevc_qsv`/`av1_qsv`/`vp9_qsv`/`mjpeg_qsv`/`mpeg2_qsv` в `-encoders`/`-decoders` и `qsv` в списке `-hwaccels`. Полный набор тестов (178 passed, 1 skipped) и `black --check .` — без ошибок. Полный текст — в `docs/CHANGELOG.md`.

## 2026-09-07 — Fedora 44: GCC≥15 + CUDA 12.2 — цепочка исправлений сборки (nvcc/gettext/OpenSSL/rav1e/libtiff/libvmaf/opencl-icd-loader)

Полная сборка FFmpeg 8.1 на Fedora Linux 44 (GCC 16.2.1, glibc ≥ 2.41, CUDA 12.2.140 SDK) выявила цепочку несовместимостей современного тулчейна. Все исправления ниже гейтятся `LinuxGcc15Platform` (класс резолвится при `gcc_major_version >= 15` — историческое имя, не буквально GCC 15) и проверены реальными сборками через `FFmpegBuilder` на настоящих исходных архивах, не только ручной компиляцией во временных директориях.

- **`nvcc` отвергает системный компилятор (CUDA 12.2 поддерживает GCC ≤ 12, Fedora 44 несёт GCC 16, пакета `gcc12` в репозиториях нет).** `PlatformDetector._detect_cuda_nvcc_support()` теперь пробует системный компилятор реальной компиляцией минимального `.cu`-файла, затем переменную окружения `CUDA_NVCC_CCBIN`, затем автоматически ищет `conda-forge`-пакеты `gcc_linux-64`/`gxx_linux-64` в conda/mamba/micromamba окружениях (`~/.local/share/mamba/envs`, `~/micromamba/envs`, `~/miniforge3/envs`, `~/miniconda3/envs`, `~/mambaforge/envs`, `/opt/conda/envs`). Найденный путь сохраняется в `platform_info.nvcc_ccbin` и используется как для базовой сборки FFmpeg (`--nvccflags='-ccbin=...'` в [builders/ffmpeg/ffmpeg.py](builders/ffmpeg/ffmpeg.py)), так и для CUDA-пути libvmaf (`NVCC_PREPEND_FLAGS=-ccbin=...` в [builders/graphics/vmaf.py](builders/graphics/vmaf.py)). Рецепт установки: `micromamba create -n cuda-gcc12 -c conda-forge gcc_linux-64=12 gxx_linux-64=12` — автоопределяется без ручной настройки. Подробности — в `docs/DeveloperReadme.md` → «Fedora 44+ / modern GCC (≥15) + CUDA 12.2 — nvcc host-compiler compatibility».
- **gettext: коллизия макроса `_Generic` gnulib и glibc ≥ 2.41.** 14 новых `SourcePatch` в [patches/c23_fixes.py](patches/c23_fixes.py) снимают макрос-коллизию `wmemchr`/`bsearch` в семи копиях gnulib, которые несёт gettext.
- **OpenSSL `./Configure` падает из-за отсутствующих Perl-модулей.** Новый [perl_shims.py](perl_shims.py) с чистыми реализациями `FindBin`/`IPC::Cmd`/`Time::Piece`, подключён через `PERL5LIB` в [builders/crypto/openssl.py](builders/crypto/openssl.py).
- **rav1e (`cargo-c`) падает: `-std=c11` ломает BSD-типы (`u_int`/`u_char`) в зависимости `libssh2-sys`.** В [builders/base.py](builders/base.py) `build_cargo()` теперь заменяет `-std=c11` → `-std=gnu11` в CFLAGS.
- **libtiff падает по той же причине (`fseeko`/`ftello` в `tiffset.c`/`tiff2pdf.c`).** Новый кастомный сборщик [builders/codecs/tiff.py](builders/codecs/tiff.py) (`build_libtiff`) добавляет `-std=gnu11`.
- **libvmaf CUDA: три последовательные проблемы совместимости с `nv-codec-headers`.** (1) `nv-codec` объявлен в `components.yaml` после `libvmaf` — исправлено инлайн-сборкой `nv-codec` внутри `build_libvmaf()` (`_ensure_nv_codec_headers()`); (2) закреплённая версия `nv-codec-headers` (совместимая с NVENC SDK 13.0 в FFmpeg 8.1) не содержит 6 символов CUDA driver API (`cuMemHostAlloc`, `cuMemFreeHost`, `cuMemFreeAsync`, `cuCtxGetStreamPriorityRange`, `cuStreamCreateWithPriority`, `cuCtxSynchronize`), нужных CUDA-экстракторам libvmaf 3.2.0 — исправлено обновлением версии по умолчанию до `13.0.19.1` (patch-релиз, подтверждено диффом апстрима, что затронуты только `dynlink_cuda.h`/`dynlink_loader.h`, а не `nvEncodeAPI.h`, т.е. совместимость с FFmpeg 8.1 сохранена) плюс защитный откат `_libvmaf_cuda_headers_compatible()`, отключающий CUDA-путь libvmaf при нехватке символов; (3) `libvmaf/src/meson.build` компилирует `.fatbin`-объекты через `custom_target` с жёстко прописанным `-I ../include`, не наследующим CFLAGS проекта — исправлено симлинком `_link_nv_codec_headers_into_source()` (`<workspace>/include/ffnvcodec` → `<libvmaf_dir>/include/ffnvcodec`). Подробности — в `docs/DeveloperReadme.md` → «libvmaf + nv-codec-headers CUDA compatibility».
- **`opencl-icd-loader` падает: сломанный тестовый таргет + устаревшие OpenCL-заголовки из CUDA SDK.** В [builders/base.py](builders/base.py) `build_cmake()`: (1) добавлен `-DBUILD_TESTING=OFF`, чтобы не собирать сломанный `test/layer/PrintLayer` (нужны GL/EGL заголовки, которых нет в headless-окружении); (2) из CFLAGS/CXXFLAGS для этого компонента убран `-I<cuda>/include`, т.к. CUDA SDK несёт собственный устаревший `CL/cl.h`, который из-за дедупликации `-isystem`/`-I` затенял свежий `opencl-headers` 2026.05.29.

Проверено: полная сборка FFmpeg 8.1 на Fedora 44 завершилась успешно; `workspace/release/ffmpeg` (реальный слинкованный бинарник) подтверждает работу всех исправленных компонентов: `--enable-cuda-nvcc --nvccflags='-ccbin=...'`, `--enable-cuvid --enable-nvdec --enable-nvenc --enable-ffnvcodec`, `--enable-openssl`, `--enable-librav1e`, `--enable-libvmaf` (включая фильтр `libvmaf_cuda`), `--enable-opencl` (19 OpenCL-фильтров, hwaccel `opencl`), `--enable-vulkan --enable-libglslang --enable-libplacebo` (Vulkan hwaccel функционально проверен реальной инициализацией GPU-устройства и smoke-тестом `scale_vulkan`). `manifest.json` — без пропущенных бинарников/зависимостей; реальная тестовая транскодировка через `libx264` прошла успешно. Полный текст — в `docs/CHANGELOG.md`.

## 2026-09-06 — Исправления по итогам ревью фаз 1–5

Ревью после слияния пяти фаз выявило дефекты; все ворота качества теперь зелёные.

- **Патч xvidcore ломал сборку сам** — [patches/c23_fixes.py](patches/c23_fixes.py) проверял отсутствие подстроки `typedef int bool;\n#endif`, но это ровно хвост вставляемого фрагмента, поэтому любая сборка xvidcore падала с `BuildError`. Лишняя проверка убрана; корректный `assert_patch_present` оставлен.
- **Пакет не импортировался вне корня репозитория** — `patches/` использовали импорты верхнего уровня (`from build_types import …`), поэтому `import ffmpeg_builder.builder` из другой директории падал с `ModuleNotFoundError: patches`. Все импорты переведены на абсолютную форму `ffmpeg_builder.*`.
- **В wheel/sdist не попадали новые пакеты** — в [pyproject.toml](pyproject.toml) добавлены `ffmpeg_builder.patches` и `ffmpeg_builder.platforms`.
- **Дублирование патчей** — шесть legacy-блоков инлайн-патчинга в [builder.py](builder.py) дублировали реестр. Пять пред-конфигурационных дублей удалены; патч OpenSSL `configdata.pm` оставлен инлайн, т.к. файл появляется только после `./Configure` (реестр до него не достаёт).
- **Мёртвый код** — убраны недостижимый цикл в [platforms/resolver.py](platforms/resolver.py), дублированный `_to_msys_path` в [platforms/windows_ucrt64.py](platforms/windows_ucrt64.py) и неиспользуемый импорт в [builders/codecs/x264.py](builders/codecs/x264.py).
- **Слепая зона mypy** — [scripts/check_mypy_baseline.py](scripts/check_mypy_baseline.py) сканировал только корневые модули и `ui/`; добавлены `platforms/`, `patches/`, `builders/`. Расширенный скан выявил реальные ошибки, которые исправлены: `.as_posix()` на `str` и `Path(None)` для Vulkan SDK в [platforms/linux_gcc13.py](platforms/linux_gcc13.py) и [platforms/darwin.py](platforms/darwin.py), рассинхрон `Optional` у `ComponentBuildContext.platform_strategy`. Добавлен протокол `PatchTarget` для типизации патчей и аннотации 15 заглушек сборщиков.
- **Нарушения black** — 9 файлов из фаз 1–5 не проходили `black --check`; отформатированы.
- **Дублирующийся раздел `## [Unreleased]`** — переименован в `## [2.0b0-dev] — pre-stable fixes`.

Проверено: `pytest tests/` (175 passed, 2 skipped, 1 failed — единственный сбой, `test_release_bundle_macos_rewrites_install_names_and_rpaths`, это ранее существовавшая Windows-специфичная проблема путей в macOS-ветке кода, воспроизводится и на коммите до рефакторинга; CI на Ubuntu не затронут); `black --check .` — OK (70 файлов); `python scripts/check_mypy_baseline.py` — OK с расширенной областью. Полный текст — в `docs/CHANGELOG.md`.

## 2026-09-05 — Windows: автоматический выбор версии nv-codec-headers по драйверу NVENC

Добавлена поддержка автоматического подбора версии `nv-codec-headers` под фактически установленный драйвер NVIDIA, поскольку закреплённая `13.0.19.0` несовместима с окружениями, где драйвер поддерживает только NVENC API до 12.2.

- **`platform_detect.py`: `_detect_nvenc_api_version()`.** Новый детектор запрашивает `NvEncodeAPIGetMaxSupportedVersion()` из библиотеки драйвера (`nvEncodeAPI64.dll` на Windows, `libnvidia-encode.so.1` на Linux) через `ctypes` и сохраняет максимальную поддерживаемую версию NVENC API в `PlatformInfo.nvenc_api_version` (`"<major>.<minor>"`) вместе с `nvenc_reason` для диагностики. В отличие от `cuda_available` (который проверяет только наличие `nvcc` в PATH), это чисто драйверная проверка — она работает даже без установленного CUDA toolkit и корректно возвращает `None`, если `nvEncodeAPI64.dll`/`libnvidia-encode.so.1` не найдены (нет драйвера NVIDIA) или вызов вернул ненулевой `NVENCSTATUS`.
- **`components.yaml`: `nv-codec` теперь объявляет `versions` для `13.0.19.0` (по умолчанию) и `12.2.72.0` (fallback).** Архив `nv-codec-headers-12.2.72.0.tar.gz` (sha256 `c295a2ba8a06434d4bdc5c2208f8a825285210d71d91d572329b2c51fd0d4d03`) добавлен в `third_party/sources` (Git LFS).
- **`components.py`: `ComponentRegistry.get_nv_codec_component()`.** Выбирает из объявленных `versions` компонента `nv-codec` самую новую версию, чьи `major.minor` не превышают версию, о которой сообщил драйвер (`platform_info.nvenc_api_version`); если драйвер сообщает версию старее всех объявленных релизов, используется самый старый объявленный релиз вместо потенциально несовместимого значения по умолчанию. Если детектирование не выполнялось или ничего не нашло, сохраняется объявленная по умолчанию версия (`13.0.19.0`) — поведение не меняется на системах без NVIDIA. `get_buildable()` теперь прогоняет компонент `nv-codec` через этот резолвер так же, как `ffmpeg`-таргет резолвится через `get_ffmpeg_component()`.
- Покрыто тестами: `tests/test_platform_detect.py::TestNvencApiVersionDetection` (Linux/Windows пути, отсутствие библиотеки драйвера, ненулевой `NVENCSTATUS`) и `tests/test_components.py::TestNvCodecVersionSelection` (fallback на 12.2, использование 13.x при поддерживающем драйвере, отсутствие детектирования, некорректное значение версии, интеграция через `get_buildable()`).

Полный текст — в `docs/CHANGELOG.md` (авторитетный источник по истории фиксов).

---

## 2026-09-05 — Windows MSYS2 UCRT64: nv-codec NVENC build fix + setup script fix

При сборке FFmpeg 8.1 на чистом Windows 11 + MSYS2 UCRT64 обнаружены и исправлены две проблемы.

- **`nv-codec` (NVENC headers) 13.1.15.0 ломает сборку FFmpeg 8.1.** `components.yaml` был закреплён на `nv-codec-headers` версии `13.1.15.0`, но `libavcodec/nvenc.c`/`nvenc.h` в FFmpeg 8.1 писаны под фичи NVENC SDK **13.0** (`nvenc.h:104`, `NVENCAPI_CHECK_VERSION(13, 0)`). В SDK 13.1 структуру `NV_ENC_CLOCK_TIMESTAMP_SET` изменили — битовое поле `countingType` разбили на `countingTypeLSB`/`countingTypeMSB`, из-за чего `nvenc_fill_time_code()` не компилировался (`has no member named 'countingType'`). Причина — версия компонента в реестре была продвинута вперёд без проверки совместимости с уже закреплённой версией FFmpeg 8.1. Исправлено закреплением `nv-codec` на `13.0.19.0` (sha256 проверен, архив уже лежит в `third_party/sources` под Git LFS, сеть не требуется) — последний релиз с ожидаемой FFmpeg 8.1 раскладкой структуры. Проверено: полная сборка на Windows MSYS2 UCRT64 прошла успешно, `workspace/release/ffmpeg.exe` собран с `--enable-cuvid --enable-nvdec --enable-nvenc --enable-ffnvcodec` и рабочими `h264_nvenc`/`hevc_nvenc`/`av1_nvenc`/`*_cuvid`.
- **`scripts/setup_windows_msys2_ucrt64.ps1` падал на чистой установке MSYS2 до установки пакетов.** Предварительная проверка `which gcc; ...` вызывалась до `pacman -S` и была фатальной (`Invoke-MsysBash` бросает исключение на любой ненулевой код возврата), поэтому на чистой MSYS2 (без `mingw-w64-ucrt-x86_64-toolchain`) скрипт падал с "gcc not found", ни разу не дойдя до установки пакетов. Отдельно — пост-инсталляционная проверка инструментов вызывала `bash.exe` напрямую без `$env:MSYSTEM = "UCRT64"`/`$env:CHERE_INVOKING = "1"`, поэтому `/etc/profile` не добавлял `/ucrt64/bin` в `PATH`, и `which <tool>` показывал "missing" даже сразу после успешной установки пакетов. Исправлено: предварительная проверка стала информационной (не бросает исключение), а `MSYSTEM`/`CHERE_INVOKING` теперь выставляются (и восстанавливаются) вокруг обоих вызовов bash. Проверено: скрипт теперь доходит до `pacman -Syu`/`pacman -S`, а пост-инсталляционная проверка корректно находит `gcc`/`cmake`/`ninja`/`meson`/`nasm`/`pkg-config`/`python`.

Полный текст — в `docs/CHANGELOG.md` (авторитетный источник по истории фиксов).

---

## 2026-08-31 — Production-readiness review, step 5: L10 (CI)

В рамках ревью для production-использования закрыт пункт L10 — отсутствие CI, из-за которого версии black/mypy дрейфовали между машинами и бейзлайн тихо менялся. Коммит после каждого шага.

- **Пины dev-инструментов.** `requirements-dev.txt` с точными версиями (pytest 9.1.1, black 26.5.1, mypy 2.3.1) — CI и локальная разработка устанавливают один и тот же набор; bump пина требует осознанного re-run `black .` и обновления mypy-бейзлайна в том же коммите.
- **Реформат зафиксированным black 26.5.1.** Репозиторий был отформатирован старой версией black; зафиксированная 26.5.1 переносила длинные lambda/вызовы в 3 файлах (`component_builders.py`, `release_bundle.py`, `tests/test_builder_split.py`). После реформата `black --check .` проходит.
- **Mypy-бейзлайн заморожен.** `scripts/check_mypy_baseline.py` + `mypy_baseline.txt`: скрипт гоняет зафиксированный mypy по исходникам (топ-уровень + `ui/`, никогда не `.` — чтобы не ползти по `workspace/`), нормализует ошибки (без номеров строк, дубликаты схлопнуты) и падает только на ошибках, которых нет в бейзлайне — «рычаг»: починка ошибок всегда зелёная, добавление новых — красная. Бейзлайн: 34 ошибки mypy (26 уникальных). `--update` обновляет бейзлайн осознанно.
- **CI workflow.** `.github/workflows/ci.yml` (push в `master` + PR): pytest + `black --check .` + mypy-бейзлайн, Python 3.12, ubuntu. Проверено end-to-end на чистом clone с чистым venv (LFS-указатели, без `workspace/`): все три шага зелёные.
- **Тест зеркала устойчив к LFS-указателям.** `third_party/sources` — Git LFS; в CI-чекауте без `git lfs pull` лежат pointer-файлы, и хеширование pointer'а дало бы ложный sha256-mismatch. Тест теперь пропускает pointer-архивы (реальные архивы по-прежнему проверяются), и CI не требует ~1 GB `git lfs pull`.

Полный текст — в `docs/CHANGELOG.md` (авторитетный источник по истории фиксов).

---

## 2026-08-31 — Production-readiness review, step 4: M5 + M6 + M9

В рамках ревью для production-использования закрываются medium-severity пункты step 4 (M5, M6, M9). Коммит после каждого пункта.

- **M5 — CWD-зависимые пути.** `workspace/`, `build_config.yaml` и `source_archives_dir: third_party/sources` резолвились относительно *текущего* рабочего каталога, тогда как `components.yaml` уже был закреплён за файлом модуля. Запуск `ffmpeg_builder` из любого каталога, кроме корня репозитория, молча использовал другие workspace/конфиг/архивы и ломал offline-first поиск архивов непонятной ошибкой. Все project-relative пути теперь закреплены за корнем проекта через `PROJECT_ROOT = Path(__file__).resolve().parent` (flat layout: пакет = корень репозитория): `__main__.py` (workspace), `app.py` (workspace + путь к конфигу), дефолт `ConfigManager` (`config.py`), дефолт `StateManager` (`state.py`), `FFmpegBuilder.source_archives` (`builder.py`) — относительный `source_archives_dir` резолвится от корня проекта, абсолютный сохраняется как есть. Явно переданные пути (например `FFmpegBuilderApp(workspace=...)`, тестовые фикстуры) не изменились. Тесты: дефолтный путь конфига, дефолтный путь состояния, якорение относительного archive-каталога, сохранение абсолютного — каждый с `monkeypatch.chdir` в временный CWD.

- **M6 — cargo-c теперь фиксируется версией.** `cargo install cargo-c` ставил *последнюю* версию с crates.io в момент сборки: разрыв воспроизводимости, сетевая зависимость даже в offline-first режиме и компиляция из исходников (~1–2 мин) на каждой машине. Команда теперь `cargo install cargo-c --version 0.10.25` — версия вынесена в константу `CARGO_C_VERSION` в `builder.py` (обновлять осознанно, когда новый релиз проверен). Тест: команда установки содержит зафиксированную версию, шаг `cargo cinstall` за ней выполняется.

- **M9 — curl убран из required-инструментов.** Downloader использует `requests`, а не `curl` (вызовов curl в исходниках нет), но `curl` числился в required-списке — на системах без curl стартовый экран показывал ложный "missing required tool". Убран из `required` в `system_report.py` и из списка детекции в `platform_detect.py` (`_detect_tools`). Тесты: curl отсутствует в required-списке (Linux + macOS) и не детектится `_detect_tools`.

Полный текст — в `docs/CHANGELOG.md` (авторитетный источник по истории фиксов).

---

## 2026-08-31 — Production-readiness review, step 1: H1 + H2

В рамках ревью для production-использования закрыты два high-severity пункта (step 1). Это **новый** пакет H1/H2 из текущего ревью; не путать с пакетом H1-H3 от 2026-08-17 выше (там другие находки: libplacebo/macOS, PATH UCRT64, Python 3.12).

- **H1 — целостность архивов при загрузке из сети.** Инфраструктура проверки `sha256` в `downloader.py` существовала, но была мёртвым кодом: ни один компонент в `components.yaml` хеш не объявлял, поэтому при `allow_network_downloads=true` повреждённый или подменённый архив (особенно через plain-HTTP fallback на `ftp.osuosl.org`/Xiph) извлекался и собирался без обнаружения. Исправление: (1) заполнен `sha256` для всех 63 компонентов — хеши посчитаны по локальному зеркалу `third_party/sources` и проверяются новым тестом; (2) добавлена политика `require_sha256_for_network` (по умолчанию `True`, вынесена в `BuildConfig.require_sha256_for_network` и проброшена через `FFmpegBuilder` в downloader), которая отказывается качать архив из сети без sha256 и падает быстро вместо загрузки непроверяемого файла. Локальные архивы зеркала по-прежнему используются без чека (доверенные локальные файлы), но теперь логгируют warning. Покрыты sync- и async-пути. Тесты: отказ от unverified-загрузки, допуск verified-загрузки, отключение политики, local-without-sha с warning, корректный sha256 у всех компонентов, совпадение хешей с зеркалом.

- **H2 — `get_buildable()` теперь учитывает инструменты, которые собирает сама сборка.** Раньше `requires_tools` прогонялся только по *системным* инструментам до сборки. Инструменты, которые сами являются компонентами раньше по порядку (`meson`, `ninja`, `cmake`) собираются из исходников в workspace, когда их нет в системе, поэтому потребители этих инструментов молча выпадали на минимальных машинах, хотя провайдер был бы установлен первым. Проверка eligibility вынесена в `_is_eligible()` (всё кроме tool-gate), а `requires_tools` теперь оценивается по эффективному набору: системные инструменты + любой инструмент, предоставленный уже принятым более ранним компонентом (компонент предоставляет инструмент, если он `system_component` с `system_tool_name`, см. `_tool_provided_by()`). Спец-кейс `rav1e` (остаётся в списке, пропускается во время сборки) сохранён. Эффект на машине без meson/ninja: `dav1d`, LV2-стек (`lv2`/`serd`/`zix`/`sord`/`sratom`/`lilv`) и `libvmaf` теперь включаются, а не выпадают молча; при этом требования только к `python3` (например `glslang`) по-прежнему гейтятся системой, т.к. ни один компонент реестра `python3` не предоставляет. Тесты: build-provided инструменты удовлетворяют потребителей, python3 всё ещё гейтит, rav1e special case, `_tool_provided_by`, инвариант «провайдер раньше потребителя».

Полный текст — в `docs/CHANGELOG.md` (авторитетный источник по истории фиксов).

---

## 2026-08-17 — Закрытие review-фиксов H1-H3 и M1-M11

В кодовой базе применён полный пакет исправлений по результатам ревью:

- **H1-H3:** восстановлена сборка `libplacebo` на macOS, исправлена сборка `PATH` для UCRT64 через `os.pathsep`, зафиксирована совместимость извлечения архивов через Python `>=3.12`.
- **M1:** устранено накопление `extralibs/ldflags` при повторных запусках `build_ffmpeg`.
- **M2:** для `meson` добавлен корректный source fallback через `custom_build_fn: build_meson`.
- **M3:** установка `cargo-c` выполняется только при отсутствии в окружении.
- **M4:** в bootstrap MSYS2 UCRT64 добавлен пакет `perl` (нужен для OpenSSL).
- **M5:** добавлены конфигурируемые таймауты `make_timeout_seconds` и `install_timeout_seconds`.
- **M6:** `BuildConfig.from_dict()` теперь устойчив к пустому YAML и неизвестным ключам.
- **M7:** удалён неиспользуемый `profiles/default.yaml`.
- **M8:** документация синхронизирована с фактическим запуском (`python -m ffmpeg_builder` без CLI-аргументов).
- **M9:** добавлена опциональная проверка `sha256` архивов в `components.yaml`/`downloader.py`.
- **M10:** удалён недостижимый `build_giflib`.
- **M11:** удалён мёртвый компонент-реестр `waflib`.

## 2026-08-17 — Successful full build on Windows 11 (MSYS2 UCRT64)

**Среда:** Windows 11, x86_64, GCC 16.2 (MSYS2 UCRT64), Python 3.13 (MSYS2 venv)
**Режим:** полная сборка с нуля в среде `windows-msys2-ucrt64`

### Результат

Все компоненты собраны и установлены успешно. Полная сборка FFmpeg 9.0 завершена.

### Устранённые проблемы

#### Неполная среда MSYS2 (cmake не установлен)

После первичной установки через `setup_windows_msys2_ucrt64.ps1` cmake не был доступен — скрипт
использовал `pacman -Sy` (только синхронизация БД без обновления пакетов). Это приводило к тому,
что cmake и другие пакеты из обновлённых репозиториев не устанавливались корректно при наличии
устаревшей базы данных пакетов.

**Симптом:** cmake configure failed с пустыми stdout/stderr и кодом возврата `3221225781`
(0xC0000135, STATUS_DLL_NOT_FOUND / команда не найдена).

**Исправление в `setup_windows_msys2_ucrt64.ps1`:**
- Последовательность `pacman -Sy; pacman -S` заменена на двухшаговую: сначала
  `pacman -Syu --noconfirm` (полное обновление системы и БД), затем `pacman -S --needed --noconfirm`
  для установки пакетов проекта
- Добавлена обязательная верификация инструментов после установки: gcc, cmake, ninja, meson,
  nasm, python, pkg-config — скрипт выводит версии и завершается с ошибкой, если что-то отсутствует

#### Ошибочная обёртка cmake/ninja через sh.exe

В попытке исправить cmake-ошибку, cmake и ninja были обёрнуты через `sh.exe` в `executor.py`.
Это сломало сборку dav1d (`cannot execute binary file`, exit 126): `sh.exe` не может запустить
Windows PE бинарник ninja напрямую.

**Исправление:** обёртка через `sh.exe` корректна только для `./script`-команд (autotools
configure, shell-скрипты). cmake и ninja в MSYS2 UCRT64 являются нативными Windows PE
исполняемыми файлами и вызываются напрямую через `subprocess.run()`.

---

## 2026-08-10 — Successful full build on Fedora Linux 44 (Python 3.14)

**Среда:** Fedora Linux 44, x86_64, GCC 16, Python 3.14 (system)
**Режим:** полная сборка с нуля, свежий Python venv, свежая конфигурация

### Результат

Все компоненты собраны и установлены успешно. Полная сборка FFmpeg 8.1 (`58/58` компонентов) за **20.5 минут**.

### Конфигурация

- `gpl_enabled`: true, `make_release`: true, `native_build`: true
- `full_static`: false, `openmp`: true
- `enable_libvmaf`: true, `enable_libvmaf_cuda`: true
- `enable_libplacebo_vulkan`: true, `disable_lv2`: false
- `num_jobs`: auto, `async_downloads`: true
- **Полный стек аппаратного ускорения** — CUDA, Vulkan, OpenCL, AMF, libplacebo

---

## 2026-08-06 — Post-refactor debugging session

### Bug: `archive_strip_components=1` создаёт двойную вложенность директорий при распаковке

**Файл:** `builder.py`, метод `_download_and_extract()` (строки ~897–904)

**Причина:**
Код перемещал элементы из staging-директории напрямую (`item.rename(target_dir / item.name)`),
но при `archive_strip_components=1` архивы содержат единую верхнеуровневую директорию
(например `giflib-5.2.2/`). В результате исходники оказывались по пути:

```
packages/giflib-5.2.2/giflib-5.2.2/Makefile   ← двойная вложенность
```

Вместо ожидаемого:

```
packages/giflib-5.2.2/Makefile                ← правильно
```

Из-за этого `make` запускался без Makefile: `No targets specified and no makefile found`.

**Исправление:**
При единственной директории верхнего уровня в staging — её содержимое теперь рекурсивно
перемещается в target_dir (реализация strip-components). Fallback для нескольких элементов
сохранён.

**Проявлялось:** Ошибка первого компонента сборки `giflib` сразу после очистки workspace.
После исправления giflib, gettext, openssl, dav1d, rav1e, x264, svtav1 собраны успешно.

### Bug: `build_x265` — бесконечная зависание на шаге merge-libs (ar -M без stdin)

**Файл:** `builder.py`, метод `_run_step()` (строки ~139–185) и `build_x265()` (строки ~1575–1585)

**Причина:**
В ходе рефакторинга (Fix #6, извлечение `_run_step` хелпера) потеряна передача `stdin`
в методе `build_x265`. Команда `ar -M` читает скрипт объединения архивов из stdin.
После рефакторинга переменная `m_script` определялась, но в `_run_step` не передавалась —
команда вешалась навсегда, ожидая ввод с клавиатуры. До рефакторинга `stdin` передавался
непосредственно в `execute_with_log`.

Это проявлялось как бесконечный повтор этапов "конфигурация → сборка": статус компонента
зацикливался между CONFIGURING и BUILDING, так как процесс не завершался ни с успехом,
ни с ошибкой.

**Исправление:**
1. Добавлен параметр `stdin: Optional[str] = None` в `_run_step()`, передаваемый
   далее в `execute_with_log(..., stdin=stdin)`.
2. Вызов `_run_step()` для merge-libs в `build_x265()` теперь использует `stdin=m_script`.

**Подтверждение:** Сборка x265 завершена успешно: configure/build для 12bit, 10bit, 8bit,
merge-libs (`ar -M` → Return code: 0), install → все артефакты установлены (libx265.a,
x265.h, x265.pc, бинарный x265).

## Полная сборка FFmpeg 8.1 — успешна (Fedora Linux 44)

**Дата:** 2026-08-06  
**Среда:** Fedora Linux 44 Workstation Edition, x86_64  
**CPU:** Intel Xeon E5-2697A v4 @ 2.60 GHz (dual socket), 128 cores/threads, gcc 16  
**RAM:** 188 GiB total, ~177 GiB available  
**Режим:** сборка с нуля (`build`, не resume)

### Время сборки

| Параметр | Значение |
|----------|----------|
| Полная сборка с нуля | **~20 минут** (рекорд; ранее было ~24 минуты) |
| Предыдущий рекорд | 24+ минут |
| Ускорение | ~17% быстрее |

### Конфигурация сборки

- `ffmpeg_version`: "8.1" (commit hash `34bd300`)
- `gpl_enabled`: true, `make_release`: true, `native_build`: true
- `full_static`: false, `openmp`: true
- `enable_libvmaf`: true, `enable_libvmaf_cuda`: false
- `enable_libplacebo`: true, `disable_lv2`: false
- `num_jobs`: auto (64 parallel jobs), `async_downloads`: true
- C standard: gnu11, CXX standard: c++17
- Компилятор: GCC 16, x86_64

### Результат

Все компоненты собраны и установлены успешно. Артефакты в `workspace/`:

| Тип | Файлы |
|-----|-------|
| Бинарники | `bin/ffmpeg`, `bin/ffprobe`, `bin/ffplay` |
| Библиотеки | `lib/libx265.a` (27 MB merged multi-bitdepth), libdav1d, libsvtav1, librav1e, x264, и др. |
| Заголовки | `include/x265.h`, `include/gif_lib.h`, и др. |
| pkg-config | `lib/pkgconfig/` — все .pc файлы |
| Логи | `logs/` — по одному файлу на каждый шаг каждого компонента |

### Верификация FFmpeg

```
ffmpeg version 34bd300 Copyright (c) 2000-2026 the FFmpeg developers
built with gcc 16 (GCC)
configuration: --disable-debug --disable-shared --enable-static --enable-version3
  ...
License: nonfree and unredistributable
```

Включены библиотеки и кодеки:

| Категория | Подтверждено |
|-----------|-------------|
| Видео кодеки | libdav1d, libsvtav1, librav1e, libx264, libx265, libvpx, libaom, xvidcore, zimg |
| Аудио кодеки | libmp3lame, libopus, libvorbis, libtheora, libfdk-aac, libsoxr, lv2 stack |
| Шифрование/сети | openssl, libsrt, libzmq |
| GPU/HW accel | vulkan, libglslang, libplacebo, amf, opencl |
| Image кодеки | libjxl, libwebp, libfreetype |

Всего enabled encoders с `lib` префиксом: **28**, decoders с `lib` префиксом: **20**.

### Замечания

- Сборка с нуля заняла ~20 минут — новый рекорд для данной конфигурации
- Все 58 buildable компонентов прошли успешно (system + source builds)
- Двойная вложенность директорий при распаковке (giflib) и зависание ar -M (x265) устранены
