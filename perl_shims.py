"""Vendored Perl module shims for distros with an unbundled core Perl.

Fedora 41+ ships a minimal Perl distribution that requires several
historically-core modules (``FindBin``, ``IPC::Cmd``, ``Time::Piece``, ...)
to be installed as separate RPMs (``perl-FindBin``, ``perl-IPC-Cmd``,
``perl-Time-Piece``). OpenSSL's ``Configure``/``Makefile.in`` machinery uses
all three and assumes a "batteries included" Perl install. Rather than
requiring every contributor to install these system packages, this module
writes small, functionally-equivalent, clean-room shims that cover only the
narrow subset of each module's documented public interface that OpenSSL's
build actually uses, and exposes them via a workspace-local ``PERL5LIB``
directory -- only for the modules genuinely missing on the host.

These are fresh, minimal reimplementations of well-known public APIs
(``$FindBin::Bin``, ``IPC::Cmd::can_run``, ``Time::Piece::strftime``/
``strptime``); they are not derived from the upstream modules' source.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

_FINDBIN_PM = """\
package FindBin;

# Minimal drop-in for environments where perl-FindBin has been unbundled
# from the base Perl install. Provides only $Bin/$Script/$RealBin/
# $RealScript, computed from $0, which is all OpenSSL's Configure needs.

use strict;
use warnings;
use Cwd ();
use File::Basename ();

our $Bin;
our $Script;
our $RealBin;
our $RealScript;
our $Dir;
our $RealDir;

my $abs = Cwd::abs_path($0);
$abs = $0 unless defined $abs;

$RealBin    = File::Basename::dirname($abs);
$RealScript = File::Basename::basename($abs);
$Bin        = $RealBin;
$Script     = $RealScript;
$Dir        = $RealBin;
$RealDir    = $RealBin;

1;
"""

_IPC_CMD_PM = """\
package IPC::Cmd;

# Minimal drop-in for environments where perl-IPC-Cmd has been unbundled
# from the base Perl install. Implements only can_run($cmd), which is the
# sole entry point OpenSSL's Configure/OpenSSL::config use.

use strict;
use warnings;
use File::Spec ();

our $VERSION = '0.01';

sub can_run {
    my ($cmd) = @_;
    return undef unless defined $cmd && length $cmd;

    if (File::Spec->file_name_is_absolute($cmd)) {
        return (-x $cmd && !-d $cmd) ? $cmd : undef;
    }

    for my $dir (split /:/, $ENV{PATH} // '') {
        next unless length $dir;
        my $candidate = File::Spec->catfile($dir, $cmd);
        return $candidate if -x $candidate && !-d $candidate;
    }
    return undef;
}

1;
"""

_TIME_PIECE_PM = """\
package Time::Piece;

# Minimal drop-in for environments where perl-Time-Piece has been unbundled
# from the base Perl install. Implements only the subset OpenSSL's
# Makefile.in template uses: scalar-context localtime/gmtime returning an
# object with ->strftime, and Time::Piece->strptime for the "%d %b %Y"
# release-date format in VERSION.dat.

use strict;
use warnings;
use POSIX ();
use Exporter ();

our @ISA = ('Exporter');
our @EXPORT = qw(localtime gmtime);

my @MON_ABBR = qw(Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec);
my %MON_INDEX;
@MON_INDEX{@MON_ABBR} = (0 .. 11);

sub _from_epoch {
    my ($class, $epoch) = @_;
    return bless { epoch => $epoch }, $class;
}

sub localtime {
    my $epoch = @_ ? shift : time;
    return __PACKAGE__->_from_epoch($epoch);
}

sub gmtime {
    my $epoch = @_ ? shift : time;
    return __PACKAGE__->_from_epoch($epoch);
}

sub strftime {
    my ($self, $fmt) = @_;
    return POSIX::strftime($fmt, CORE::localtime($self->{epoch}));
}

sub strptime {
    my ($class, $str, $fmt) = @_;
    if ($fmt eq '%d %b %Y' && $str =~ /^\\s*(\\d{1,2})\\s+(\\w{3})\\s+(\\d{4})\\s*$/) {
        my ($day, $mon_str, $year) = ($1, $2, $3);
        my $mon = $MON_INDEX{$mon_str};
        die "Time::Piece shim: unrecognised month '$mon_str'\\n" unless defined $mon;
        my $epoch = POSIX::mktime(0, 0, 12, $day, $mon, $year - 1900);
        die "Time::Piece shim: mktime failed for '$str'\\n" unless defined $epoch;
        return $class->_from_epoch($epoch);
    }
    die "Time::Piece shim: unsupported strptime format '$fmt'\\n";
}

1;
"""

# Maps each shim's relative install path (under the shim root) to its
# source and the module name used to probe real availability via `perl -M`.
_SHIM_SOURCES: Dict[str, str] = {
    "FindBin.pm": _FINDBIN_PM,
    "IPC/Cmd.pm": _IPC_CMD_PM,
    "Time/Piece.pm": _TIME_PIECE_PM,
}

_SHIM_MODULE_NAMES: Dict[str, str] = {
    "FindBin.pm": "FindBin",
    "IPC/Cmd.pm": "IPC::Cmd",
    "Time/Piece.pm": "Time::Piece",
}


def _perl_module_available(module: str, env: Optional[Dict[str, str]] = None) -> bool:
    """Probe whether ``perl -M<module> -e1`` succeeds in the given environment."""
    try:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        result = subprocess.run(
            ["perl", f"-M{module}", "-e", "1"],
            capture_output=True,
            timeout=10,
            env=merged_env,
        )
        return result.returncode == 0
    except Exception:
        return False


def apply_perl_shims_to_env(
    workspace: Path,
    env: Dict[str, str],
    on_log: Optional[object] = None,
) -> None:
    """Inject PERL5LIB shims into ``env`` for any core Perl modules missing on this host.

    Probes each of ``FindBin``, ``IPC::Cmd``, and ``Time::Piece`` via ``perl
    -M<module> -e1`` using ``env``'s current PATH/PERL5LIB. For any module
    that is genuinely missing, writes (or refreshes) a minimal shim under
    ``<workspace>/perl_shims/`` and prepends that directory to ``env``'s
    ``PERL5LIB``, mutating ``env`` in place. Leaves ``env`` untouched if all
    three modules are already available (the common case on most distros).
    Stale shims for modules no longer missing are removed so a shim never
    shadows a genuinely-installed system module.

    Args:
        workspace: Build workspace root; shims are written under
            ``workspace/perl_shims``.
        env: Build environment dict to probe against and mutate in place.
        on_log: Optional single-argument callable (e.g. ``builder.on_log``)
            used to report which shims were injected.
    """
    missing = [
        rel_path
        for rel_path, module_name in _SHIM_MODULE_NAMES.items()
        if not _perl_module_available(module_name, env)
    ]

    shim_root = workspace / "perl_shims"

    for rel_path in _SHIM_SOURCES:
        target = shim_root / rel_path
        if rel_path in missing:
            content = _SHIM_SOURCES[rel_path]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8")
        elif target.exists():
            # Remove stale shims for modules that are no longer missing, so
            # a leftover shim never shadows a now-installed system module.
            target.unlink()

    if not missing:
        return

    existing = env.get("PERL5LIB") or os.environ.get("PERL5LIB", "")
    env["PERL5LIB"] = os.pathsep.join(part for part in (str(shim_root), existing) if part)

    if callable(on_log):
        module_names = ", ".join(_SHIM_MODULE_NAMES[rel_path] for rel_path in missing)
        on_log(
            f"Injected PERL5LIB shims for missing core Perl modules ({module_names}); "
            f"see {shim_root}"
        )
