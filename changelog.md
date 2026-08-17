# Changelog

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
