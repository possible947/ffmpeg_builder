"""G2.3: Rust target std as an additive artifact of the project-managed Rust toolchain.

Archives are small fixtures with real SHA-256 digests (the production integrity
path runs unmodified). Their ``install.sh`` scripts emulate the rust-installer
and install a stub ``rustc`` so the production Rust verifier runs for real.
"""

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

from ffmpeg_builder import toolchain_builders
from ffmpeg_builder.build_types import BuildError
from ffmpeg_builder.toolchain_registry import ToolchainRegistry
from ffmpeg_builder.toolchain_store import ToolchainStore

TARGET = "x86_64-pc-windows-gnullvm"
TARGET2 = "i686-pc-windows-gnullvm"
BACKEND = "linux-cross-mingw"

_BASE_INSTALL = r"""#!/bin/sh
for a in "$@"; do case "$a" in --prefix=*) P="${a#--prefix=}";; esac; done
mkdir -p "$P/bin" "$P/lib/rustlib"
cat > "$P/bin/rustc" <<'EOS'
#!/bin/sh
here=$(cd "$(dirname "$0")/.." && pwd -P)
case "$*" in
  "--version") echo "rustc @RELEASE@ (fake)";;
  "-vV") printf 'rustc @RELEASE@ (fake)\nrelease: @RELEASE@\n';;
  "--print sysroot") echo "$here";;
  *"--print target-libdir"*)
    t=""; prev=""
    for a in "$@"; do [ "$prev" = "--target" ] && t="$a"; prev="$a"; done
    echo "$here/lib/rustlib/$t/lib";;
  *)
    out=""; prev=""
    for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
    if ls "$here"/lib/rustlib/*/lib/BROKEN >/dev/null 2>&1; then echo "error: std broken" >&2; exit 1; fi
    printf 'd\206' > "$out";;
esac
EOS
printf '#!/bin/sh\necho "cargo @RELEASE@ (fake)"\n' > "$P/bin/cargo"
chmod +x "$P/bin/rustc" "$P/bin/cargo"
echo rustc >> "$P/lib/rustlib/components"
"""

_EXTRA_INSTALL = r"""#!/bin/sh
for a in "$@"; do case "$a" in --prefix=*) P="${a#--prefix=}";; --components=*) C="${a#--components=}";; esac; done
@BODY@
"""

_EXTRA_OK = r"""L="$P/lib/rustlib/@TARGET@/lib"
mkdir -p "$L"
echo core > "$L/libcore-1234.rlib"
echo std > "$L/libstd-1234.rlib"
@EXTRA_FILES@
echo "$C" >> "$P/lib/rustlib/components"
"""


def _tar(path: Path, members: Dict[str, str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:xz") as tar:
        for name, text in members.items():
            payload = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    """A mirror with a fake base Rust and fake target std archives."""

    def __init__(self, tmp_path: Path, version: str = "1.99.0") -> None:
        self.tmp_path = tmp_path
        self.version = version
        self.mirror = tmp_path / "sources"
        self.base_name = f"rust-{version}-x86_64-unknown-linux-gnu.tar.gz"
        self.base_sha = ""
        self.extras: List[dict] = []

    def base(self, compiler_release: Optional[str] = None) -> "Fixture":
        release = compiler_release or self.version
        top = f"rust-{self.version}-x86_64-unknown-linux-gnu"
        script = _BASE_INSTALL.replace("@RELEASE@", release)
        self.base_sha = _tar(
            self.mirror / self.base_name,
            {f"{top}/install.sh": script, f"{top}/components": "rustc\n"},
        )
        return self

    def extra(
        self,
        target: str = TARGET,
        *,
        broken_install: bool = False,
        broken_std: bool = False,
        top_release: Optional[str] = None,
        backends: Optional[List[str]] = None,
        suffix: str = "",
    ) -> "Fixture":
        release = self.version
        top = f"rust-std-{top_release or release}-{target}"
        name = f"rust-std-{release}-{target}.tar.xz"
        files = f'echo x > "$L/BROKEN"' if broken_std else ""
        body = "exit 3" if broken_install else _EXTRA_OK.replace("@TARGET@", target)
        body = body.replace("@EXTRA_FILES@", files)
        script = _EXTRA_INSTALL.replace("@BODY@", body)
        sha = _tar(
            self.mirror / name,
            {
                f"{top}/install.sh": script + suffix,
                f"{top}/components": f"rust-std-{target}\n",
            },
        )
        self.extras.append(
            {
                "kind": "rust-std",
                "target": target,
                "component": f"rust-std-{target}",
                "release": release,
                "backends": backends if backends is not None else [BACKEND],
                "archive_filename": name,
                "sha256": sha,
            }
        )
        return self

    def registry(self, **overrides) -> ToolchainRegistry:
        profile = {
            "kind": "rust",
            "platforms": ["linux"],
            "version": self.version,
            "provides": ["cargo", "rustc"],
            "materialize": {"bin_subdir": "bin"},
            "source": {
                "default": "build",
                "build": {
                    "type": "build",
                    "recipe": "rust_standalone",
                    "url": f"https://example.org/{self.base_name}",
                    "archive_filename": self.base_name,
                    "sha256": self.base_sha,
                },
            },
            "extras": [dict(item) for item in self.extras],
        }
        profile.update(overrides)
        path = self.tmp_path / "toolchains.yaml"
        path.write_text(yaml.safe_dump({"toolchains": {"rust": profile}}), encoding="utf-8")
        return ToolchainRegistry(path)

    def store(self, backend: Optional[str] = BACKEND) -> ToolchainStore:
        return ToolchainStore(
            self.tmp_path / "store",
            workspace=self.tmp_path / "workspace",
            source_archives_dir=self.mirror,
            backend=backend,
        )


def _fingerprint(prefix: Path) -> Dict[str, str]:
    return {
        str(p.relative_to(prefix)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(prefix.rglob("*"))
        if p.is_file()
    }


def _error(fn) -> BuildError:
    with pytest.raises(BuildError) as info:
        fn()
    return info.value


@pytest.fixture
def fx(tmp_path) -> Fixture:
    return Fixture(tmp_path).base().extra()


class TestProductionRegistry:
    def test_rust_declares_windows_gnullvm_std_for_wsl_mingw_only(self):
        registry = ToolchainRegistry()
        profile = registry.get_profile("rust")
        assert profile is not None and profile.version == "1.99.0"
        assert [(e.kind, e.target) for e in profile.extras] == [("rust-std", TARGET)]
        extra = profile.extras[0]
        assert extra.release == profile.version
        assert extra.component == f"rust-std-{TARGET}"
        assert extra.sha256 == "dcf636d91ef0639dd31a67169c7d7e27e1f013632490e1af97753b31728e85c0"
        assert profile.required_extras(BACKEND) == [extra]
        assert profile.required_extras(None) == []

    def test_wsl_mingw_recommends_rust_and_native_is_unchanged(self):
        registry = ToolchainRegistry()
        wsl = registry.recommend_profiles(
            "8.1", "linux", {}, components=[], installed=[], backend=BACKEND
        )
        assert [name for name, _ in wsl] == ["llvm-mingw", "rust"]
        native = registry.recommend_profiles("8.1", "linux", {}, components=[], installed=[])
        assert "rust" not in [name for name, _ in native]


class TestRegistryValidation:
    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda e: e.pop("archive_filename"), "archive_filename"),
            (lambda e: e.pop("sha256"), "requires a sha256"),
            (lambda e: e.update(sha256="xyz"), "64-digit hex"),
            (lambda e: e.pop("target"), "target"),
            (lambda e: e.update(kind="rust-docs"), "unsupported kind"),
            (lambda e: e.update(release="1.99.1"), "does not match"),
            (
                lambda e: e.update(archive_filename="rust-std-1.99.1-" + TARGET + ".tar.xz"),
                "inconsistent",
            ),
            (lambda e: e.update(backends=["windows-msys2-ucrt64"]), "invalid backend"),
            (lambda e: e.update(target="not a triple"), "valid target triple"),
        ],
    )
    def test_invalid_extra_rejected_at_load(self, fx, mutate, message):
        mutate(fx.extras[0])
        with pytest.raises(ValueError, match=message):
            fx.registry()

    def test_duplicate_target_rejected(self, fx):
        fx.extras.append(dict(fx.extras[0]))
        with pytest.raises(ValueError, match="duplicates"):
            fx.registry()

    def test_extras_require_rust_profile_kind(self, fx):
        with pytest.raises(ValueError, match="only valid on 'rust'"):
            fx.registry(kind="compiler")

    def test_extras_require_rust_standalone_source(self, fx):
        registry_source = {
            "default": "binary",
            "binary": {
                "type": "binary",
                "url": "https://example.org/x.tar.gz",
                "archive_filename": "x.tar.gz",
                "sha256": "a" * 64,
            },
        }
        with pytest.raises(ValueError, match="rust_standalone"):
            fx.registry(source=registry_source)


class TestCompositeInstall:
    def test_fresh_install_materialises_base_and_target_std_in_one_prefix(self, fx):
        registry, store = fx.registry(), fx.store()
        entry = store.acquire(registry, "rust")
        prefix = Path(entry.path)
        assert (prefix / "bin" / "rustc").exists()
        assert (prefix / "lib" / "rustlib" / TARGET / "lib" / "libcore-1234.rlib").exists()
        components = (prefix / "lib" / "rustlib" / "components").read_text().split()
        assert components == ["rustc", f"rust-std-{TARGET}"]
        assert store.installed_status(registry, "rust")[0] == "compatible"
        assert not list(store.installed_dir.glob(".staging-*"))

        provenance = yaml.safe_load((prefix / ".toolchain-install.json").read_text())
        assert provenance["source_sha256"] == fx.base_sha
        assert provenance["extras"] == [
            {
                "kind": "rust-std",
                "target": TARGET,
                "component": f"rust-std-{TARGET}",
                "release": "1.99.0",
                "archive_filename": f"rust-std-1.99.0-{TARGET}.tar.xz",
                "sha256": fx.extras[0]["sha256"],
            }
        ]
        info = store.describe(registry, "rust")
        assert info is not None
        assert info["version"] == "1.99.0" and info["base_sha256"] == fx.base_sha
        assert info["targets"] == [TARGET] and info["extras"][0]["sha256"] == fx.extras[0]["sha256"]
        assert info["verified"] is True and info["prefix"] == str(prefix)

    def test_reuse_does_not_reinstall(self, fx):
        registry, store = fx.registry(), fx.store()
        first = store.acquire(registry, "rust")
        marker = Path(first.path) / "marker"
        marker.write_text("kept")
        again = store.acquire(registry, "rust")
        assert again.installed_at == first.installed_at and marker.exists()

    def test_native_install_has_no_windows_target_and_stays_valid(self, fx):
        registry = fx.registry()
        native = fx.store(backend=None)
        entry = native.acquire(registry, "rust")
        assert entry.extras == []
        assert not (Path(entry.path) / "lib" / "rustlib" / TARGET).exists()
        assert native.installed_status(registry, "rust")[0] == "compatible"

    def test_native_install_is_incomplete_for_wsl_mingw_and_upgrades_atomically(self, fx):
        registry = fx.registry()
        fx.store(backend=None).acquire(registry, "rust")
        wsl = fx.store()
        status, reason = wsl.installed_status(registry, "rust")
        assert status == "stale" and TARGET in reason
        assert wsl.installed("rust", registry) is None
        entry = wsl.acquire(registry, "rust")
        assert (Path(entry.path) / "lib" / "rustlib" / TARGET).is_dir()
        assert wsl.installed_status(registry, "rust")[0] == "compatible"
        # a native consumer of the same store still sees a compatible install
        assert fx.store(backend=None).installed_status(registry, "rust")[0] == "compatible"


class TestSourceIntegrity:
    def test_missing_extra_archive_is_a_project_source_error(self, fx):
        registry, store = fx.registry(), fx.store()
        (fx.mirror / fx.extras[0]["archive_filename"]).unlink()
        error = _error(lambda: store.acquire(registry, "rust"))
        assert (error.category, error.code) == ("project_source", "source_missing")
        assert TARGET in str(error) and "rust 1.99.0" in str(error)
        assert store.installed("rust") is None
        assert not (store.installed_dir / "rust-1.99.0").exists()
        assert not list(store.installed_dir.glob(".staging-*"))

    def test_corrupt_extra_archive_rejected_before_staging(self, fx):
        registry, store = fx.registry(), fx.store()
        archive = fx.mirror / fx.extras[0]["archive_filename"]
        archive.write_bytes(archive.read_bytes() + b"corrupt")
        error = _error(lambda: store.acquire(registry, "rust"))
        assert (error.category, error.code) == ("project_source", "checksum_mismatch")
        assert fx.extras[0]["sha256"] in str(error) and "rust-std" in str(error)
        assert not (store.installed_dir / "rust-1.99.0").exists()
        assert not (store.installed_dir / ".staging-rust-1.99.0").exists()
        assert store.installed("rust") is None

    def test_release_mismatched_archive_rejected_before_installation(self, tmp_path):
        fx = Fixture(tmp_path).base().extra(top_release="1.99.1")
        registry, store = fx.registry(), fx.store()
        error = _error(lambda: store.acquire(registry, "rust"))
        assert (error.category, error.code) == ("toolchain_install", "extra_release_mismatch")
        assert "1.99.1" in str(error)
        assert store.installed("rust") is None
        assert not (store.installed_dir / "rust-1.99.0").exists()

    def test_compiler_release_mismatch_rejected_by_verifier(self, tmp_path):
        fx = Fixture(tmp_path).base(compiler_release="1.99.1").extra()
        registry, store = fx.registry(), fx.store()
        error = _error(lambda: store.acquire(registry, "rust"))
        assert (error.category, error.code) == ("toolchain_install", "verification_failed")
        assert "does not match" in str(error) and "1.99.1" in str(error)
        assert store.installed("rust") is None


class TestRollbackPreservesActiveToolchain:
    def _replace_extra(self, fx, **kwargs):
        """New extra archive content under a new hash: forces a replacement install."""
        fx.extras.clear()
        fx.extra(suffix="# changed\n", **kwargs)
        return fx.registry()

    @pytest.mark.parametrize(
        "kwargs, code",
        [
            ({"broken_install": True}, "extra_install_failed"),
            ({"broken_std": True}, "verification_failed"),
        ],
    )
    def test_failed_replacement_keeps_previous_install(self, fx, kwargs, code):
        store = fx.store()
        entry = store.acquire(fx.registry(), "rust")
        before = _fingerprint(Path(entry.path))
        state_before = store.state_path.read_text()
        registry = self._replace_extra(fx, **kwargs)
        assert store.installed_status(registry, "rust")[0] == "stale"

        error = _error(lambda: store.acquire(registry, "rust"))
        assert (error.category, error.code) == ("toolchain_install", code)
        assert _fingerprint(Path(entry.path)) == before
        assert store.state_path.read_text() == state_before
        assert not list(store.installed_dir.glob(".staging-*"))
        assert not list(store.installed_dir.glob(".backup-*"))

    def test_failed_first_install_leaves_nothing_active(self, tmp_path):
        fx = Fixture(tmp_path).base().extra(broken_install=True)
        store = fx.store()
        _error(lambda: store.acquire(fx.registry(), "rust"))
        assert store.installed("rust") is None
        assert store.list_installed() == []
        assert not (store.installed_dir / "rust-1.99.0").exists()


class TestStaleDetection:
    def _installed(self, fx):
        store = fx.store()
        store.acquire(fx.registry(), "rust")
        return store

    def test_extra_hash_change_is_stale(self, fx):
        store = self._installed(fx)
        fx.extras[0]["sha256"] = "b" * 64
        status, reason = store.installed_status(fx.registry(), "rust")
        assert status == "stale" and "sha256" in reason

    def test_target_change_is_stale(self, fx):
        store = self._installed(fx)
        fx.extras[0]["target"] = TARGET2
        fx.extras[0]["component"] = f"rust-std-{TARGET2}"
        fx.extras[0]["archive_filename"] = f"rust-std-1.99.0-{TARGET2}.tar.xz"
        status, reason = store.installed_status(fx.registry(), "rust")
        assert status == "stale" and TARGET2 in reason

    def test_extra_removed_from_registry_is_stale(self, fx):
        store = self._installed(fx)
        fx.extras.clear()
        status, reason = store.installed_status(fx.registry(), "rust")
        assert status == "stale" and "no longer declared" in reason

    def test_extra_added_to_registry_is_stale(self, tmp_path):
        base_only = Fixture(tmp_path).base()
        store = base_only.store()
        store.acquire(base_only.registry(), "rust")
        base_only.extra()
        status, reason = store.installed_status(base_only.registry(), "rust")
        assert status == "stale" and "not installed" in reason

    def test_base_hash_change_is_stale(self, fx):
        store = self._installed(fx)
        fx.base_sha = "c" * 64
        assert store.installed_status(fx.registry(), "rust")[0] == "stale"

    def test_extra_order_is_not_semantic(self, tmp_path):
        fx = Fixture(tmp_path).base().extra(TARGET).extra(TARGET2)
        store = fx.store()
        store.acquire(fx.registry(), "rust")
        fx.extras.reverse()
        assert store.installed_status(fx.registry(), "rust")[0] == "compatible"

    def test_provenance_extras_are_sorted_independent_of_order(self, tmp_path):
        fx = Fixture(tmp_path).base().extra(TARGET).extra(TARGET2)
        store = fx.store()
        entry = store.acquire(fx.registry(), "rust")
        targets = [item["target"] for item in entry.provenance()["extras"]]
        assert targets == sorted(targets)

    def test_legacy_install_without_extras_is_incomplete_for_wsl_mingw_only(self, fx):
        registry = fx.registry()
        store = fx.store()
        entry = store.acquire(registry, "rust")
        state = store.load_state()
        state.profiles["rust"].extras = []  # state written before G2.3
        store.save_state(state)
        assert store.installed_status(registry, "rust")[0] == "stale"
        assert fx.store(backend=None).installed_status(registry, "rust")[0] == "compatible"
        # --verify-only reports it but neither marks it broken nor invents an extra
        assert store.reverify(registry) == ["rust"]
        after = store.load_state().profiles["rust"]
        assert after.verified is True and after.extras == []
        assert Path(entry.path).is_dir()

    def test_missing_directory_is_corrupt(self, fx):
        store = self._installed(fx)
        import shutil

        shutil.rmtree(store.installed_dir / "rust-1.99.0")
        assert store.installed_status(fx.registry(), "rust")[0] == "corrupt"


class TestVerifyOnly:
    def test_reverify_passes_for_full_composite(self, fx):
        registry, store = fx.registry(), fx.store()
        store.acquire(registry, "rust")
        assert store.reverify(registry) == []

    def test_reverify_flags_removed_target_std(self, fx):
        import shutil

        registry, store = fx.registry(), fx.store()
        entry = store.acquire(registry, "rust")
        shutil.rmtree(Path(entry.path) / "lib" / "rustlib" / TARGET)
        assert store.reverify(registry) == ["rust"]
        assert store.installed("rust") is None  # marked unverified


class TestRustVerifier:
    def _prefix(self, fx, tmp_path, name="p"):
        registry, store = fx.registry(), fx.store()
        entry = store.acquire(registry, "rust")
        return registry.get_profile("rust"), Path(entry.path)

    def test_target_layout_absent_fails(self, fx, tmp_path):
        import shutil

        profile, prefix = self._prefix(fx, tmp_path)
        shutil.rmtree(prefix / "lib" / "rustlib" / TARGET)
        failures = toolchain_builders._verify_rust(prefix, profile, profile.extras)
        assert failures and "target library directory missing" in failures[0]

    def test_missing_libcore_fails(self, fx, tmp_path):
        profile, prefix = self._prefix(fx, tmp_path)
        (prefix / "lib" / "rustlib" / TARGET / "lib" / "libcore-1234.rlib").unlink()
        failures = toolchain_builders._verify_rust(prefix, profile, profile.extras)
        assert failures and "libcore" in failures[0]

    def test_full_verification_passes(self, fx, tmp_path):
        profile, prefix = self._prefix(fx, tmp_path)
        assert toolchain_builders._verify_rust(prefix, profile, profile.extras) == []

    def test_compile_probe_failure_is_reported(self, fx, tmp_path):
        profile, prefix = self._prefix(fx, tmp_path)
        (prefix / "lib" / "rustlib" / TARGET / "lib" / "BROKEN").write_text("x")
        failures = toolchain_builders._verify_rust(prefix, profile, profile.extras)
        assert failures and "compile probe failed" in failures[0]

    def test_probe_output_for_wrong_architecture_is_rejected(self, fx, tmp_path):
        profile, prefix = self._prefix(fx, tmp_path)
        rustc = prefix / "bin" / "rustc"
        rustc.write_text(rustc.read_text().replace("'d\\206'", "'MZ'"))
        failures = toolchain_builders._verify_rust(prefix, profile, profile.extras)
        assert failures and "not COFF" in failures[0]

    def test_candidate_rustc_is_used_not_an_ambient_one(self, fx, tmp_path, monkeypatch):
        profile, prefix = self._prefix(fx, tmp_path)
        ambient = tmp_path / "ambient" / "bin"
        ambient.mkdir(parents=True)
        (ambient / "rustc").write_text("#!/bin/sh\necho ambient; exit 9\n")
        (ambient / "rustc").chmod(0o755)
        monkeypatch.setenv("PATH", f"{ambient}:/usr/bin:/bin")
        monkeypatch.setenv("RUSTUP_TOOLCHAIN", "stable")
        assert toolchain_builders._verify_rust(prefix, profile, profile.extras) == []

    def test_sysroot_outside_the_candidate_prefix_is_rejected(self, fx, tmp_path):
        profile, prefix = self._prefix(fx, tmp_path)
        rustc = prefix / "bin" / "rustc"
        rustc.write_text(rustc.read_text().replace('echo "$here"', 'echo "/usr"'))
        failures = toolchain_builders._verify_rust(prefix, profile, profile.extras)
        assert failures and "not the candidate prefix" in failures[0]
