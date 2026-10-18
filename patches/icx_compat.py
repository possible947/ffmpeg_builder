"""Intel oneAPI ICX/ICPX compiler compatibility patches."""

from __future__ import annotations

from pathlib import Path

from ffmpeg_builder.patches.base import (
    PatchStrategy,
    PatchTarget,
    SourcePatch,
    assert_patch_present,
)

# x264's configure treats unknown compilers (icx reports as IntelLLVM, not
# icc) as GCC: it probes -mpreferred-stack-boundary=6 and appends it to
# CFLAGS while declaring STACK_ALIGNMENT=64 to the NASM assembly. icx
# accepts the flag syntactically (cc_check passes) but ignores it
# semantically (warning #10430), so the NASM AVX/AVX-512 paths that require
# 64-byte stack alignment segfault.
#
# Per upstream x264 guidance (Henrik Gramner): -mstack-alignment may be
# safely dropped ONLY together with the matching ASFLAGS setting, keeping
# both sides consistent. The default ABI alignment (16) then applies and
# x264 performs realignment where needed (it self-aligns at library/thread
# entrypoints). So for ICX the patch REPLACES the boundary=6 probe block
# with a fixed 16 (both CFLAGS append and STACK_ALIGNMENT define), without
# appending any compiler flag. GCC builds are untouched (condition-gated).
_X264_ICX_STACK_OLD = (
    "        if cc_check '' -mpreferred-stack-boundary=6 ; then\n"
    '            CFLAGS="$CFLAGS -mpreferred-stack-boundary=6"\n'
    "            stack_alignment=64\n"
    "        elif cc_check '' -mstack-alignment=64 ; then\n"
    '            CFLAGS="$CFLAGS -mstack-alignment=64"\n'
    "            stack_alignment=64\n"
    "        elif [ $stack_alignment -lt 16 ] ; then\n"
)
_X264_ICX_STACK_NEW = (
    "        # ICX/oneAPI: -mpreferred-stack-boundary is accepted but ignored\n"
    "        # (warning #10430), which breaks the 64-byte contract with the\n"
    "        # NASM assembly. Fall back to default ABI alignment (x264 then\n"
    "        # realigns at entrypoints as upstream documents).\n"
    "        if [ $stack_alignment -lt 16 ] ; then\n"
)


def _is_icx_cc(component: PatchTarget) -> bool:
    cc = getattr(component, "extra_env", {}).get("CC", "")
    cxx = getattr(component, "extra_env", {}).get("CXX", "")
    names = (Path(cc).name, Path(cxx).name)
    return any(
        name.startswith(("icx", "icpx", "dpcpp", "clang")) or "intel" in name.lower()
        for name in names
    )


def _patch_x264_icx_stack_alignment(
    path: Path, component: PatchTarget, strategy: PatchStrategy
) -> None:
    content = path.read_text(encoding="utf-8")
    if "ICX/oneAPI: -mpreferred-stack-boundary is accepted but ignored" in content:
        return
    if _X264_ICX_STACK_OLD not in content:
        raise AssertionError(
            "x264 configure pattern changed; ICX stack-alignment patch needs review"
        )
    path.write_text(content.replace(_X264_ICX_STACK_OLD, _X264_ICX_STACK_NEW, 1), encoding="utf-8")
    assert_patch_present(
        component.name,
        path,
        "ICX/oneAPI: -mpreferred-stack-boundary is accepted but ignored",
        "x264 ICX stack alignment",
    )


def _x264_icx_condition(component: PatchTarget, strategy: PatchStrategy) -> bool:
    return _is_icx_cc(component)


X264_ICX_STACK_ALIGNMENT_PATCH = SourcePatch(
    name="x264-icx-stack-alignment",
    component_name="x264",
    target_rel_path="configure",
    apply_fn=_patch_x264_icx_stack_alignment,
    condition=_x264_icx_condition,
)

__all__ = ["X264_ICX_STACK_ALIGNMENT_PATCH"]
