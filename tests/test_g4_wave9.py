from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.g4_wave9 import Wave9Validator, select_components
from ffmpeg_builder.windows_probe import FeatureState


def _app(state, *, conditional=()):
    component = ComponentRegistry().get_by_name("opencl-headers")
    return SimpleNamespace(
        windows_target_state=state,
        _get_component_selection=lambda config: SimpleNamespace(
            target_inputs=[component], conditional_components=list(conditional)
        ),
    )


def _state(opencl=FeatureState.PRESENT, gpus=()):
    return SimpleNamespace(opencl=SimpleNamespace(state=opencl), gpus=gpus)


def test_selection_uses_windows_opencl_not_host_and_excludes_native_loader():
    selected = select_components(_app(_state()), object())
    assert [component.name for component in selected] == ["opencl-headers"]
    assert not select_components(_app(_state(FeatureState.ABSENT)), object())
    component = selected[0]
    with pytest.raises(BuildError, match="not selected"):
        select_components(_app(_state(), conditional=[component]), object())
    with pytest.raises(BuildError, match="unknown"):
        select_components(_app(_state(FeatureState.UNAVAILABLE)), object())


@pytest.mark.parametrize("vendor", ["8086", "1002"])
def test_selection_does_not_silently_discard_unsupported_physical_gpu(vendor):
    gpu = SimpleNamespace(name="GPU", vendor_id=vendor, adapter_type="hardware")
    with pytest.raises(BuildError, match="AMD/Intel GPU"):
        select_components(_app(_state(gpus=[gpu])), object())


def test_wave9_reuse_requires_all_target_headers_import_and_sources(tmp_path):
    prefix, workspace = tmp_path / "prefix", tmp_path / "workspace"
    prefix.mkdir()
    workspace.mkdir()
    env = SimpleNamespace(paths=SimpleNamespace(target_prefix=prefix, workspace_root=workspace))
    validator = Wave9Validator(env, "identity", {"opencl-headers": "source"}, _state())
    component = ComponentRegistry().get_by_name("opencl-headers")
    for relative in ("include/CL/cl.h", "include/CL/cl_platform.h", "lib/libOpenCL.a"):
        output = prefix / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(relative)
    validator.record_component(component, validator._owned_outputs(component))
    assert validator.manifest_matches(component)
    (prefix / "lib/libOpenCL.a").write_text("changed")
    assert not validator.manifest_matches(component)
    (prefix / "lib/libOpenCL.a").write_text("lib/libOpenCL.a")
    validator.sources = {"opencl-headers": "changed"}
    assert not validator.manifest_matches(component)


def test_opencl_target_metadata_records_import_and_external_runtime():
    metadata = ComponentRegistry().get_target_link_metadata(["opencl-headers"])
    assert metadata.import_interfaces == ("lib/libOpenCL.a",)
    assert [(item.name, item.classification) for item in metadata.runtime_dependencies] == [
        ("OpenCL.dll", "external-runtime-prerequisite")
    ]
