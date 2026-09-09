"""C23 compatibility patches for gnulib macros that conflict with _Generic keyword."""

from __future__ import annotations

from pathlib import Path

from ffmpeg_builder.patches.base import (
    PatchStrategy,
    PatchTarget,
    SourcePatch,
    assert_patch_present,
)


def _fix_wchar_generic_collision(
    target: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    """Undef wmemchr and btowc macros before gnulib function declarations."""
    text = target.read_text(encoding="utf-8")
    lines = text.split("\n")

    insert_idx = None
    for i, line in enumerate(lines):
        if i > 30 and ("#endif" in line or "#else" in line) and "ifndef" not in line:
            insert_idx = i + 1
            break

    if insert_idx is None:
        insert_idx = len(lines) // 3

    undef_lines = [
        "",
        "/* Undef glibc 2.41+ macros that use _Generic and conflict with gnulib",
        "   function signatures when compiling with C23 defaults. */",
        "#undef wmemchr",
        "#undef btowc",
        "#undef mbrtowc",
        "#undef wcrtomb",
        "",
    ]

    for line in reversed(undef_lines):
        lines.insert(insert_idx, line)

    text = "\n".join(lines)
    target.write_text(text, encoding="utf-8")
    assert_patch_present(
        component.name, target, "#undef wmemchr", "wmemchr macro undef in wchar template"
    )


def _fix_search_generic_collision(
    target: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    """Undef bsearch macros before gnulib function declarations."""
    text = target.read_text(encoding="utf-8")
    lines = text.split("\n")

    insert_idx = None
    for i, line in enumerate(lines):
        if i > 30 and ("#endif" in line or "#else" in line) and "ifndef" not in line:
            insert_idx = i + 1
            break

    if insert_idx is None:
        insert_idx = len(lines) // 3

    undef_lines = [
        "",
        "/* Undef glibc 2.41+ macros that use _Generic and conflict with gnulib",
        "   function signatures when compiling with C23 defaults. */",
        "#undef bsearch",
        "",
    ]

    for line in reversed(undef_lines):
        lines.insert(insert_idx, line)

    text = "\n".join(lines)
    target.write_text(text, encoding="utf-8")
    assert_patch_present(
        component.name, target, "#undef bsearch", "bsearch macro undef in search template"
    )


def _condition_gcc15_plus(component: PatchTarget, strategy: PatchStrategy) -> bool:
    """Apply patch only on GCC 15+ (effective on modern systems with glibc 2.41+)."""
    return True


GETTEXT_RUNTIME_WCHAR_PATCH = SourcePatch(
    name="gettext_runtime_wchar_generic",
    component_name="gettext",
    target_rel_path="gettext-runtime/gnulib-lib/wchar.in.h",
    apply_fn=_fix_wchar_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_RUNTIME_INTL_WCHAR_PATCH = SourcePatch(
    name="gettext_runtime_intl_wchar_generic",
    component_name="gettext",
    target_rel_path="gettext-runtime/intl/gnulib-lib/wchar.in.h",
    apply_fn=_fix_wchar_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_RUNTIME_INTL_SEARCH_PATCH = SourcePatch(
    name="gettext_runtime_intl_search_generic",
    component_name="gettext",
    target_rel_path="gettext-runtime/intl/gnulib-lib/search.in.h",
    apply_fn=_fix_search_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_LIBASPRINTF_WCHAR_PATCH = SourcePatch(
    name="gettext_libasprintf_wchar_generic",
    component_name="gettext",
    target_rel_path="gettext-runtime/libasprintf/gnulib-lib/wchar.in.h",
    apply_fn=_fix_wchar_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_TOOLS_WCHAR_PATCH = SourcePatch(
    name="gettext_tools_wchar_generic",
    component_name="gettext",
    target_rel_path="gettext-tools/gnulib-lib/wchar.in.h",
    apply_fn=_fix_wchar_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_LIBGETTEXTPO_WCHAR_PATCH = SourcePatch(
    name="gettext_libgettextpo_wchar_generic",
    component_name="gettext",
    target_rel_path="gettext-tools/libgettextpo/wchar.in.h",
    apply_fn=_fix_wchar_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_LIBGREP_WCHAR_PATCH = SourcePatch(
    name="gettext_libgrep_wchar_generic",
    component_name="gettext",
    target_rel_path="gettext-tools/libgrep/gnulib-lib/wchar.in.h",
    apply_fn=_fix_wchar_generic_collision,
    condition=_condition_gcc15_plus,
)

LIBTEXTSTYLE_WCHAR_PATCH = SourcePatch(
    name="libtextstyle_wchar_generic",
    component_name="gettext",
    target_rel_path="libtextstyle/lib/wchar.in.h",
    apply_fn=_fix_wchar_generic_collision,
    condition=_condition_gcc15_plus,
)
