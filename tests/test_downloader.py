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
