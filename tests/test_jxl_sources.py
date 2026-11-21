import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.builders.codecs import jxl, jxl_sources
from ffmpeg_builder.components import ComponentRegistry
from ffmpeg_builder.downloader import Downloader


def _archive(path: Path, members: dict[str, bytes]) -> str:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    deps = []
    for name, revision in (("brotli", "a" * 40), ("highway", "b" * 40)):
        path = cache / f"libjxl-{name}-{revision}.tar.gz"
        root = f"google-{name}-{revision[:7]}"
        digest = _archive(path, {f"{root}/CMakeLists.txt": b"project(test)\n"})
        deps.append(jxl_sources.JxlDependency(name, revision, digest))
    monkeypatch.setattr(jxl_sources, "DEPENDENCIES", tuple(deps))
    source = tmp_path / "source"
    (source / "third_party").mkdir(parents=True)
    (source / "deps.sh").write_text(
        "".join(f'THIRD_PARTY_{d.name.upper()}="{d.revision}" # pin\n' for d in deps)
    )
    builder = SimpleNamespace(downloader=Downloader(cache, allow_network_downloads=False))
    return builder, source, deps


def test_pinned_sources_use_verified_cache_without_network(tmp_path, monkeypatch):
    builder, source, deps = _fixture(tmp_path, monkeypatch)
    for dep in deps:
        (source / "third_party" / dep.name).mkdir()
    result = jxl_sources.prepare_libjxl_sources(builder, source)
    assert result == {d.name: {"revision": d.revision, "sha256": d.sha256} for d in deps}
    for dep in deps:
        assert (source / "third_party" / dep.name / "CMakeLists.txt").is_file()
        assert (builder.downloader.packages_dir / dep.filename).is_file()


def test_missing_or_corrupt_cached_source_fails_closed(tmp_path, monkeypatch):
    builder, source, deps = _fixture(tmp_path, monkeypatch)
    (builder.downloader.packages_dir / deps[0].filename).write_bytes(b"corrupt")
    with pytest.raises(BuildError, match="SHA256 mismatch"):
        jxl_sources.prepare_libjxl_sources(builder, source)
    (builder.downloader.packages_dir / deps[0].filename).unlink()
    with pytest.raises(BuildError, match="missing"):
        jxl_sources.prepare_libjxl_sources(builder, source)


def test_source_extraction_rejects_traversal_and_existing_tree(tmp_path, monkeypatch):
    builder, source, deps = _fixture(tmp_path, monkeypatch)
    archive = builder.downloader.packages_dir / deps[0].filename
    root = deps[0].archive_root
    digest = _archive(archive, {f"{root}/CMakeLists.txt": b"ok", f"{root}/../../escape": b"bad"})
    monkeypatch.setattr(
        jxl_sources,
        "DEPENDENCIES",
        (jxl_sources.JxlDependency(deps[0].name, deps[0].revision, digest),),
    )
    with pytest.raises(BuildError, match="Unsafe archive member"):
        jxl_sources.prepare_libjxl_sources(builder, source)
    assert not (tmp_path / "escape").exists()
    existing = source / "third_party" / "brotli"
    existing.mkdir()
    (existing / "keep").write_text("preserve")
    with pytest.raises(BuildError, match="Refusing to overwrite"):
        jxl_sources.prepare_libjxl_sources(builder, source)
    assert (existing / "keep").read_text() == "preserve"


def test_cross_builder_uses_target_recipe(tmp_path, monkeypatch):
    component = ComponentRegistry().get_by_name("libjxl")
    prepared = []
    monkeypatch.setattr(jxl, "_build_cross", lambda *args: prepared.append(args))
    builder = SimpleNamespace(platform_context=SimpleNamespace(cross_environment=object()))
    jxl.build_libjxl(builder, component, tmp_path)
    assert prepared == [(builder, component, tmp_path)]


def test_native_builder_retains_existing_recipe(tmp_path, monkeypatch):
    component = ComponentRegistry().get_by_name("libjxl")
    calls = []
    monkeypatch.setattr(
        jxl, "prepare_libjxl_sources", lambda *args: pytest.fail("cross-only preparation")
    )
    builder = SimpleNamespace(
        platform_context=SimpleNamespace(cross_environment=None),
        get_build_env=lambda component: {},
        _ws_str=lambda: "/native",
        num_jobs=2,
        _run_step=lambda *args: calls.append(args),
    )
    jxl.build_libjxl(builder, component, tmp_path)
    assert calls[0][4] == ["./deps.sh"]
    assert calls[-1][4] == ["cmake", "--install", "."]


def test_registered_pins_match_libjxl_and_local_mirror():
    root = Path(__file__).resolve().parents[1]
    component = ComponentRegistry().get_by_name("libjxl")
    upstream = root / "third_party/sources" / f"libjxl-{component.version}.tar.gz"
    if upstream.read_bytes()[:7] == b"version":
        pytest.skip("Git LFS archives are not materialised in this checkout")
    with tarfile.open(upstream) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("/deps.sh"))
        deps = tar.extractfile(member).read().decode()
    attributes = (root / ".gitattributes").read_text()
    assert "third_party/sources/* filter=lfs" in attributes
    for dep in jxl_sources.DEPENDENCIES:
        assert f'THIRD_PARTY_{dep.name.upper()}="{dep.revision}"' in deps
        mirror = root / "third_party/sources" / dep.filename
        assert mirror.is_file()
        if mirror.read_bytes()[:7] != b"version":
            assert hashlib.sha256(mirror.read_bytes()).hexdigest() == dep.sha256


def test_offline_preparation_resolves_local_source_mirror(tmp_path, monkeypatch):
    builder, source, deps = _fixture(tmp_path, monkeypatch)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    for dep in deps:
        (builder.downloader.packages_dir / dep.filename).rename(mirror / dep.filename)
    builder.downloader = Downloader(
        tmp_path / "packages", source_archives_dir=mirror, allow_network_downloads=False
    )
    jxl_sources.prepare_libjxl_sources(builder, source)
    for dep in deps:
        assert (source / "third_party" / dep.name / "CMakeLists.txt").is_file()


def test_lfs_pointer_wrong_root_and_pin_drift_fail_clearly(tmp_path, monkeypatch):
    builder, source, deps = _fixture(tmp_path, monkeypatch)
    archive = builder.downloader.packages_dir / deps[0].filename
    original = archive.read_bytes()
    archive.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n")
    with pytest.raises(BuildError, match="unresolved Git LFS pointer"):
        jxl_sources.prepare_libjxl_sources(builder, source)
    digest = _archive(archive, {"google-brotli-other00/CMakeLists.txt": b"x"})
    monkeypatch.setattr(
        jxl_sources,
        "DEPENDENCIES",
        (jxl_sources.JxlDependency(deps[0].name, deps[0].revision, digest), deps[1]),
    )
    with pytest.raises(BuildError, match="does not match the pinned revision root"):
        jxl_sources.prepare_libjxl_sources(builder, source)
    archive.write_bytes(original)
    monkeypatch.setattr(jxl_sources, "DEPENDENCIES", tuple(deps))
    (source / "deps.sh").write_text(f'THIRD_PARTY_BROTLI="{"c" * 40}"\n')
    with pytest.raises(BuildError, match="deps.sh pins THIRD_PARTY_BROTLI"):
        jxl_sources.prepare_libjxl_sources(builder, source)
