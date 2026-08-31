"""Tests for downloader checksum validation."""

import hashlib
from pathlib import Path

import pytest

from ffmpeg_builder.downloader import Downloader


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_download_uses_existing_file_when_checksum_matches(tmp_path: Path):
    data = b"archive-content"
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(data)

    downloader = Downloader(tmp_path, allow_network_downloads=False)
    out = downloader.download(
        "https://example.invalid/test.tar.gz",
        filename="test.tar.gz",
        expected_sha256=_sha256_bytes(data),
        show_progress=False,
    )
    assert out == archive


def test_download_rejects_existing_file_on_checksum_mismatch(tmp_path: Path):
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"bad-data")
    downloader = Downloader(tmp_path, allow_network_downloads=False)

    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        downloader.download(
            "https://example.invalid/test.tar.gz",
            filename="test.tar.gz",
            expected_sha256=_sha256_bytes(b"expected-data"),
            show_progress=False,
        )

    assert archive.exists()


class TestNetworkIntegrityPolicy:
    """H1: never fetch an unverified archive from the network."""

    def test_network_download_without_sha256_is_refused(self, tmp_path: Path):
        downloader = Downloader(
            tmp_path, allow_network_downloads=True, require_sha256_for_network=True
        )
        # No local archive present, so this would go to the network.
        with pytest.raises(RuntimeError, match="without a sha256"):
            downloader.download(
                "https://example.invalid/missing.tar.gz",
                filename="missing.tar.gz",
                expected_sha256=None,
                show_progress=False,
            )

    def test_network_download_with_sha256_is_allowed(self, tmp_path: Path, monkeypatch):
        data = b"network-archive-bytes"
        downloader = Downloader(
            tmp_path, allow_network_downloads=True, require_sha256_for_network=True
        )

        def fake_download_file(url, target_path, show_progress, progress_cb=None):
            target_path.write_bytes(data)

        monkeypatch.setattr(downloader, "_download_file", fake_download_file)

        out = downloader.download(
            "https://example.invalid/net.tar.gz",
            filename="net.tar.gz",
            expected_sha256=_sha256_bytes(data),
            show_progress=False,
        )
        assert out == tmp_path / "net.tar.gz"
        assert out.read_bytes() == data

    def test_policy_can_be_disabled_to_allow_unverified_network(self, tmp_path: Path, monkeypatch):
        data = b"unverified-bytes"
        downloader = Downloader(
            tmp_path, allow_network_downloads=True, require_sha256_for_network=False
        )

        def fake_download_file(url, target_path, show_progress, progress_cb=None):
            target_path.write_bytes(data)

        monkeypatch.setattr(downloader, "_download_file", fake_download_file)

        out = downloader.download(
            "https://example.invalid/unverified.tar.gz",
            filename="unverified.tar.gz",
            expected_sha256=None,
            show_progress=False,
        )
        assert out.read_bytes() == data

    def test_local_archive_without_sha256_still_accepted(self, tmp_path: Path, caplog):
        """Local mirror archives are trusted and used even without a checksum."""
        import logging

        archive = tmp_path / "local.tar.gz"
        archive.write_bytes(b"local-bytes")
        downloader = Downloader(tmp_path, allow_network_downloads=False)

        with caplog.at_level(logging.WARNING, logger="ffmpeg_builder.downloader"):
            out = downloader.download(
                "https://example.invalid/local.tar.gz",
                filename="local.tar.gz",
                expected_sha256=None,
                show_progress=False,
            )
        assert out == archive
        assert any("without sha256 verification" in r.message for r in caplog.records)


class TestRegistryChecksums:
    """H1: every registry component ships a verifiable sha256."""

    def test_all_components_have_well_formed_sha256(self):
        from ffmpeg_builder.components import ComponentRegistry

        reg = ComponentRegistry()
        bad = [
            c.name
            for c in reg.get_all()
            if not (
                c.sha256
                and len(c.sha256) == 64
                and all(ch in "0123456789abcdef" for ch in c.sha256)
            )
        ]
        assert bad == [], f"components missing/invalid sha256: {bad}"

    def test_registry_sha256_matches_local_mirror(self, tmp_path: Path):
        """If the local source mirror is present, every stored hash must match it."""
        import hashlib
        from ffmpeg_builder.components import ComponentRegistry

        mirror = Path(__file__).resolve().parent.parent / "third_party" / "sources"
        if not mirror.is_dir():
            pytest.skip("local source mirror not present in this checkout")

        reg = ComponentRegistry()
        mismatches = []
        for comp in reg.get_all():
            archive = mirror / comp.get_archive_filename()
            if not archive.exists():
                continue
            # Un-pulled Git LFS pointer (e.g. CI checkouts without `git lfs pull`):
            # nothing real to verify against.
            if b"git-lfs.github.com" in archive.read_bytes()[:200]:
                continue
            h = hashlib.sha256()
            with open(archive, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != comp.sha256:
                mismatches.append(comp.name)
        assert mismatches == [], f"sha256 does not match mirror for: {mismatches}"

    def test_component_version_profile_checksums_match_local_mirror(self):
        """Declared optional source versions must retain independently verified hashes."""
        import hashlib
        from ffmpeg_builder.components import ComponentRegistry

        mirror = Path(__file__).resolve().parent.parent / "third_party" / "sources"
        if not mirror.is_dir():
            pytest.skip("local source mirror not present in this checkout")

        registry = ComponentRegistry()
        mismatches = []
        for component in registry.get_all():
            for version in component.versions:
                resolved = component.with_version(version)
                archive = mirror / resolved.get_archive_filename()
                if not archive.exists() or b"git-lfs.github.com" in archive.read_bytes()[:200]:
                    continue
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                if digest != resolved.sha256:
                    mismatches.append(f"{component.name} {version}")

        assert mismatches == [], f"profile sha256 does not match mirror for: {mismatches}"
