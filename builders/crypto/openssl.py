"""OpenSSL component builder."""

from pathlib import Path


def build_openssl(builder, component, source_dir: Path) -> None:
    builder.build_openssl(component, source_dir)
