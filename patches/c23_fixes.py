"""Compiler and C23 compatibility source patches."""

from __future__ import annotations

from pathlib import Path

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.patches.base import (
    PatchStrategy,
    PatchTarget,
    SourcePatch,
    assert_patch_absent,
    assert_patch_present,
)


def _patch_xvidcore_encoder_h(path: Path, component: PatchTarget, strategy: PatchStrategy) -> None:
    if component.name != "xvidcore":
        return
    content = path.read_text(encoding="utf-8")
    legacy = "typedef int bool;"
    patched = (
        "#if !defined(__STDC_VERSION__) || __STDC_VERSION__ < 202311L\n"
        "typedef int bool;\n"
        "#endif"
    )
    if legacy in content and patched not in content:
        path.write_text(content.replace(legacy, patched, 1), encoding="utf-8")
    final = path.read_text(encoding="utf-8")
    if legacy in final and patched not in final:
        raise BuildError(
            component.name,
            f"Source patch did not take effect in {path}: unguarded '{legacy}' still present "
            "(C23 bool typedef gate). The xvidcore version may have changed.",
        )
    assert_patch_present(
        component.name,
        path,
        "#if !defined(__STDC_VERSION__) || __STDC_VERSION__ < 202311L",
        "C23 bool typedef gate",
    )


def _patch_gettext_gnulib_wchar_h(
    path: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    """Undo glibc's ISO C23 ``_Generic`` macro shadowing gnulib's ``wmemchr``.

    gettext's bundled gnulib snapshot adds a GCC >= 15 "Declarations for ISO
    C N3322" block that re-declares ``wmemchr`` as a plain extern function.
    On glibc >= 2.41 (e.g. Fedora 44), ``_GNU_SOURCE`` unconditionally
    implies ``_ISOC23_SOURCE``, which makes glibc's own ``<wchar.h>`` define
    ``wmemchr`` as a const-qualifier-overloading ``_Generic`` macro. gnulib's
    plain re-declaration then collides with that macro expansion, producing
    "expected identifier or '(' before '_Generic'". Undefining the macro
    right before gnulib's own declaration resolves the collision without
    disabling ``_GNU_SOURCE`` (which gnulib needs for other declarations).
    """
    content = path.read_text(encoding="utf-8")
    legacy = (
        "# ifndef __cplusplus\n"
        "_GL_EXTERN_C wchar_t *wmemchr (const wchar_t *__s, wchar_t __wc, size_t __n)\n"
    )
    patched = (
        "# ifndef __cplusplus\n"
        "#  undef wmemchr\n"
        "_GL_EXTERN_C wchar_t *wmemchr (const wchar_t *__s, wchar_t __wc, size_t __n)\n"
    )
    if legacy in content and patched not in content:
        path.write_text(content.replace(legacy, patched, 1), encoding="utf-8")
    assert_patch_present(
        component.name,
        path,
        "#  undef wmemchr",
        "gnulib GCC>=15 N3322 wmemchr redeclaration vs. glibc >= 2.41 _Generic macro",
    )


def _patch_gettext_gnulib_stdlib_h(
    path: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    """Undo glibc's ISO C23 ``_Generic`` macro shadowing gnulib's ``bsearch``.

    Same root cause and fix as ``_patch_gettext_gnulib_wchar_h``, applied to
    the ``bsearch`` declaration in gnulib's GCC >= 15 N3322 block in
    ``stdlib.in.h``: glibc >= 2.41 defines ``bsearch`` as a ``_Generic``
    macro whenever ``_ISOC23_SOURCE`` is active (implied by ``_GNU_SOURCE``),
    which collides with gnulib's plain re-declaration.
    """
    content = path.read_text(encoding="utf-8")
    legacy = "_GL_EXTERN_C void *bsearch (const void *__key,"
    patched = "#  undef bsearch\n_GL_EXTERN_C void *bsearch (const void *__key,"
    if legacy in content and patched not in content:
        path.write_text(content.replace(legacy, patched, 1), encoding="utf-8")
    assert_patch_present(
        component.name,
        path,
        "#  undef bsearch",
        "gnulib GCC>=15 N3322 bsearch redeclaration vs. glibc >= 2.41 _Generic macro",
    )


def _patch_openssl_configdata(path: Path, component: PatchTarget, strategy: PatchStrategy) -> None:
    content = path.read_text(encoding="utf-8")
    if "-std=c11" in content:
        updated = content.replace("-std=c11", "-std=gnu11")
        path.write_text(updated, encoding="utf-8")
    assert_patch_absent(component.name, path, "-std=c11", "configdata.pm -std=c11 -> -std=gnu11")


XVIDCORE_BOOL_PATCH = SourcePatch(
    name="xvidcore-c23-bool-gate",
    component_name="xvidcore",
    target_rel_path="src/encoder.h",
    apply_fn=_patch_xvidcore_encoder_h,
)

OPENSSL_C11_PATCH = SourcePatch(
    name="openssl-c11-gnu11",
    component_name="openssl",
    target_rel_path="configdata.pm",
    apply_fn=_patch_openssl_configdata,
)

# gettext vendors its own copy of the affected gnulib wchar.in.h/stdlib.in.h
# pair once per gnulib-lib import (gettext-runtime, gettext-runtime/intl,
# gettext-runtime/libasprintf, gettext-tools, gettext-tools/libgettextpo,
# gettext-tools/libgrep, libtextstyle), so every copy needs the same fix.
# Scoped to LinuxGcc15Platform only: the collision requires both gnulib's
# GCC >= 15 N3322 re-declaration block and a glibc new enough (>= 2.41) to
# make _GNU_SOURCE imply _ISOC23_SOURCE; GCC < 15 builds never hit gnulib's
# guarded block in the first place.
_GETTEXT_GNULIB_DIRS = (
    "gettext-runtime/gnulib-lib",
    "gettext-runtime/intl/gnulib-lib",
    "gettext-runtime/libasprintf/gnulib-lib",
    "gettext-tools/gnulib-lib",
    "gettext-tools/libgettextpo",
    "gettext-tools/libgrep/gnulib-lib",
    "libtextstyle/lib",
)

_GETTEXT_LINUX_GCC15_CONDITION = (
    lambda component, strategy: type(strategy).__name__ == "LinuxGcc15Platform"
)

GETTEXT_GNULIB_WCHAR_H_PATCHES = [
    SourcePatch(
        name=f"gettext-gnulib-wmemchr-generic-{gnulib_dir.replace('/', '-')}",
        component_name="gettext",
        target_rel_path=f"{gnulib_dir}/wchar.in.h",
        apply_fn=_patch_gettext_gnulib_wchar_h,
        condition=_GETTEXT_LINUX_GCC15_CONDITION,
    )
    for gnulib_dir in _GETTEXT_GNULIB_DIRS
]

GETTEXT_GNULIB_STDLIB_H_PATCHES = [
    SourcePatch(
        name=f"gettext-gnulib-bsearch-generic-{gnulib_dir.replace('/', '-')}",
        component_name="gettext",
        target_rel_path=f"{gnulib_dir}/stdlib.in.h",
        apply_fn=_patch_gettext_gnulib_stdlib_h,
        condition=_GETTEXT_LINUX_GCC15_CONDITION,
    )
    for gnulib_dir in _GETTEXT_GNULIB_DIRS
]

__all__ = [
    "XVIDCORE_BOOL_PATCH",
    "OPENSSL_C11_PATCH",
    "GETTEXT_GNULIB_WCHAR_H_PATCHES",
    "GETTEXT_GNULIB_STDLIB_H_PATCHES",
]
