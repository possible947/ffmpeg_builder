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
    """Undef wmemchr and btowc macros after glibc includes in wchar.h template.

    Patches the .in.h template file to add undefs after #@INCLUDE_NEXT@ directives.
    These undefs will appear in the generated .h file after make processes the template.
    """
    text = target.read_text(encoding="utf-8")

    # In .in.h templates, the include directive uses placeholders like #@INCLUDE_NEXT@
    # We need to insert undefs after the conditional block that includes system wchar.h
    # Pattern: #if @HAVE_WCHAR_H@ / # @INCLUDE_NEXT@ @NEXT_WCHAR_H@ / #endif

    marker = "#if @HAVE_WCHAR_H@\n# @INCLUDE_NEXT@ @NEXT_WCHAR_H@\n#endif"
    if marker in text:
        undef_block = (
            marker
            + "\n"
            + "\n"
            + "/* Undef glibc 2.41+ macros that use _Generic and conflict with gnulib\n"
            + "   function signatures when compiling with C23 defaults. */\n"
            + "#undef wmemchr\n"
            + "#undef btowc\n"
            + "#undef mbrtowc\n"
            + "#undef wcrtomb\n"
        )
        text = text.replace(marker + "\n", undef_block + "\n")
        target.write_text(text, encoding="utf-8")
        assert_patch_present(
            component.name, target, "#undef wmemchr", "wmemchr macro undef after wchar.h include"
        )
        return

    # Fallback: for already-generated headers, look for #include_next <wchar.h>
    marker = "#include_next <wchar.h>"
    if marker in text:
        undef_block = (
            marker
            + "\n"
            + "\n"
            + "/* Undef glibc 2.41+ macros that use _Generic and conflict with gnulib\n"
            + "   function signatures when compiling with C23 defaults. */\n"
            + "#undef wmemchr\n"
            + "#undef btowc\n"
            + "#undef mbrtowc\n"
            + "#undef wcrtomb\n"
        )
        text = text.replace(marker + "\n", undef_block + "\n")
        target.write_text(text, encoding="utf-8")
        assert_patch_present(
            component.name, target, "#undef wmemchr", "wmemchr macro undef after wchar.h include"
        )


def _fix_search_generic_collision(
    target: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    """Undef bsearch macros after glibc includes in search.h template.

    Patches the .in.h template file to add undefs after #@INCLUDE_NEXT@ directives.
    """
    text = target.read_text(encoding="utf-8")

    # In .in.h templates, look for the conditional include block
    marker = "#if @HAVE_SEARCH_H@\n# @INCLUDE_NEXT@ @NEXT_SEARCH_H@\n#endif"
    if marker in text:
        undef_block = (
            marker
            + "\n"
            + "\n"
            + "/* Undef glibc 2.41+ macros that use _Generic and conflict with gnulib\n"
            + "   function signatures when compiling with C23 defaults. */\n"
            + "#undef bsearch\n"
        )
        text = text.replace(marker + "\n", undef_block + "\n")
        target.write_text(text, encoding="utf-8")
        assert_patch_present(
            component.name, target, "#undef bsearch", "bsearch macro undef after search.h include"
        )
        return

    # Fallback: for already-generated headers
    marker = "#include_next <search.h>"
    if marker in text:
        undef_block = (
            marker
            + "\n"
            + "\n"
            + "/* Undef glibc 2.41+ macros that use _Generic and conflict with gnulib\n"
            + "   function signatures when compiling with C23 defaults. */\n"
            + "#undef bsearch\n"
        )
        text = text.replace(marker + "\n", undef_block + "\n")
        target.write_text(text, encoding="utf-8")
        assert_patch_present(
            component.name, target, "#undef bsearch", "bsearch macro undef after search.h include"
        )


def _fix_stdlib_generic_collision(
    target: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    """Undef bsearch macros after glibc includes in stdlib.h template.

    stdlib.h has TWO #@INCLUDE_NEXT@ blocks so we need to patch both.
    """
    text = target.read_text(encoding="utf-8")

    # The key is to replace the include_next lines with the line + undefs
    marker = "#@INCLUDE_NEXT@ @NEXT_STDLIB_H@"

    if marker not in text:
        # Try fallback for generated files
        marker = "#include_next <stdlib.h>"

    if marker not in text:
        return

    # Insert undefs after marker (works for both occurrences)
    undef_block = (
        "\n"
        + "/* Undef glibc 2.41+ macros that use _Generic and conflict with gnulib\n"
        + "   function signatures when compiling with C23 defaults. */\n"
        + "#undef bsearch\n"
    )

    # Replace ALL occurrences of "marker\n" with "marker\n undef_block"
    text = text.replace(marker + "\n", marker + undef_block)

    target.write_text(text, encoding="utf-8")
    assert_patch_present(
        component.name, target, "#undef bsearch", "bsearch macro undef after stdlib.h includes"
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

GETTEXT_RUNTIME_STDLIB_PATCH = SourcePatch(
    name="gettext_runtime_stdlib_generic",
    component_name="gettext",
    target_rel_path="gettext-runtime/gnulib-lib/stdlib.in.h",
    apply_fn=_fix_stdlib_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_RUNTIME_INTL_STDLIB_PATCH = SourcePatch(
    name="gettext_runtime_intl_stdlib_generic",
    component_name="gettext",
    target_rel_path="gettext-runtime/intl/gnulib-lib/stdlib.in.h",
    apply_fn=_fix_stdlib_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_RUNTIME_LIBASPRINTF_STDLIB_PATCH = SourcePatch(
    name="gettext_runtime_libasprintf_stdlib_generic",
    component_name="gettext",
    target_rel_path="gettext-runtime/libasprintf/gnulib-lib/stdlib.in.h",
    apply_fn=_fix_stdlib_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_TOOLS_STDLIB_PATCH = SourcePatch(
    name="gettext_tools_stdlib_generic",
    component_name="gettext",
    target_rel_path="gettext-tools/gnulib-lib/stdlib.in.h",
    apply_fn=_fix_stdlib_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_LIBGETTEXTPO_STDLIB_PATCH = SourcePatch(
    name="gettext_libgettextpo_stdlib_generic",
    component_name="gettext",
    target_rel_path="gettext-tools/libgettextpo/stdlib.in.h",
    apply_fn=_fix_stdlib_generic_collision,
    condition=_condition_gcc15_plus,
)

GETTEXT_LIBGREP_STDLIB_PATCH = SourcePatch(
    name="gettext_libgrep_stdlib_generic",
    component_name="gettext",
    target_rel_path="gettext-tools/libgrep/gnulib-lib/stdlib.in.h",
    apply_fn=_fix_stdlib_generic_collision,
    condition=_condition_gcc15_plus,
)

LIBTEXTSTYLE_STDLIB_PATCH = SourcePatch(
    name="libtextstyle_stdlib_generic",
    component_name="gettext",
    target_rel_path="libtextstyle/lib/stdlib.in.h",
    apply_fn=_fix_stdlib_generic_collision,
    condition=_condition_gcc15_plus,
)
