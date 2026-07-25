#!/usr/bin/env bash
#
# check_python_env.sh — Check Python environment and dependencies for FFmpeg Builder
#
# Usage: ./scripts/check_python_env.sh
#
# Checks:
#   1. Python 3 interpreter availability
#   2. All required Python packages (from pyproject.toml)
#   3. Version compliance (>= minimum)
#   4. Virtual environment status
#   5. Installation commands for missing packages (pip / apt / dnf / port / pacman / zypper)
#   6. MSYS2 detection and Windows bootstrap hint

set -o pipefail

# ── Colors ────────────────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
    R='\033[0;31m'; G='\033[0;32m'; Y='\033[0;33m'
    B='\033[0;34m'; C='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
    R=''; G=''; Y=''; B=''; C=''; BOLD=''; DIM=''; NC=''
fi

# ── Dependency table ──────────────────────────────────────────────────────────
#    pip_name       min_version   import_name     apt_name           dnf_name            pacman_name         zypper_name
#                                                                                                      

DEPS=(
    "rich           13.0.0        rich            python3-rich        python3-rich         python-rich         python3-rich"
    "tqdm           4.65.0        tqdm            python3-tqdm        python3-tqdm         python-tqdm         python3-tqdm"
    "pyyaml         6.0           yaml            python3-yaml        python3-pyyaml       python-yaml         python3-PyYAML"
    "requests       2.31.0        requests        python3-requests    python3-requests     python-requests     python3-requests"
    "packaging      23.0          packaging       python3-packaging   python3-packaging    python-packaging    python3-packaging"
    "psutil         5.9.0         psutil          python3-psutil      python3-psutil       python-psutil       python3-psutil"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

# Compare two version strings: returns 0 if $1 >= $2
version_gte() {
    local installed="$1" required="$2"
    if [[ "$installed" == "$required" ]]; then return 0; fi

    local i IFS='.'
    local -a a=(${installed%%[a-zA-Z]*}) b=(${required%%[a-zA-Z]*})

    for i in 0 1 2; do
        local x="${a[$i]:-0}" y="${b[$i]:-0}"
        if (( 10#$x > 10#$y )); then return 0; fi
        if (( 10#$x < 10#$y )); then return 1; fi
    done
    return 0
}

# Check if a Python module is importable; sets MOD_VERSION on success
check_module() {
    local import_name="$1" pip_name="$2" sys_pkg="$3"
    MOD_VERSION=$(python3 -c "
import sys
try:
    m = __import__('${import_name}')
    if sys.modules.get('${import_name}') is None:
        raise ImportError
    v = getattr(m, '__version__', None) or getattr(m, 'VERSION', None) or ''
    if isinstance(v, tuple): v = '.'.join(str(x) for x in v)
    if v and v not in ('UNKNOWN', '0.0.0'):
        print(v); sys.exit()
    from importlib.metadata import version
    v = version('${pip_name}')
    if v and v != '0.0.0':
        print(v); sys.exit()
    print('')
except (ImportError, Exception):
    print('__MISSING__')
" 2>/dev/null) || MOD_VERSION="__MISSING__"

    if [[ "$MOD_VERSION" == "__MISSING__" ]]; then return 1; fi

    # Fallback: dpkg for Debian/Ubuntu system packages
    if [[ -z "$MOD_VERSION" || "$MOD_VERSION" == "0.0.0" || "$MOD_VERSION" == "UNKNOWN" ]]; then
        if command -v dpkg &>/dev/null && [[ -n "$sys_pkg" ]]; then
            local dpkg_ver
            dpkg_ver=$(dpkg -s "$sys_pkg" 2>/dev/null | awk '/^Version:/{print $2}')
            if [[ -n "$dpkg_ver" ]]; then
                MOD_VERSION="${dpkg_ver%%-*}"
            fi
        fi
    fi

    return 0
}

pad_right() { printf "%-${2}s" "$1"; }
pad_left () { printf "%${2}s" "$1"; }

# ── 1. Detect Python 3 ───────────────────────────────────────────────────────

PYTHON_BIN=$(command -v python3 2>/dev/null)

if [[ -z "$PYTHON_BIN" ]]; then
    printf "${R}${BOLD}ERROR: python3 not found in PATH${NC}\n"
    echo ""
    echo "Install Python 3 first:"
    echo "  Debian/Ubuntu : sudo apt install python3"
    echo "  Fedora        : sudo dnf install python3"
    echo "  macOS/Ports   : sudo port install python312"
    echo "  Arch          : sudo pacman -S python"
    exit 1
fi

PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

# ── 2. Detect platform ───────────────────────────────────────────────────────

OS=$(uname -s)
DISTRO="unknown"; PKG_MGR="unknown"
IS_MSYS2="no"
MSYS2_MINGW_PREFIX=""

if [[ "$OS" == "Darwin" ]]; then
    DISTRO="macos"
    if command -v port &>/dev/null;   then PKG_MGR="port"; fi
elif [[ "$OS" == "Linux" ]]; then
    if [[ -f /etc/os-release ]]; then
        DISTRO=$(. /etc/os-release && echo "${ID:-unknown}")
    fi
    if command -v apt   &>/dev/null; then PKG_MGR="apt"
    elif command -v dnf &>/dev/null; then PKG_MGR="dnf"
    elif command -v pacman &>/dev/null; then PKG_MGR="pacman"
    elif command -v zypper &>/dev/null; then PKG_MGR="zypper"
    fi
elif [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* || -n "${MSYSTEM:-}" ]]; then
    DISTRO="msys2"
    PKG_MGR="pacman"
    IS_MSYS2="yes"

    case "${MSYSTEM:-UCRT64}" in
        UCRT64)  MSYS2_MINGW_PREFIX="mingw-w64-ucrt-x86_64" ;;
        CLANG64) MSYS2_MINGW_PREFIX="mingw-w64-clang-x86_64" ;;
        MINGW64) MSYS2_MINGW_PREFIX="mingw-w64-x86_64" ;;
        *)
            MSYS2_MINGW_PREFIX="mingw-w64-ucrt-x86_64"
            ;;
    esac
fi

# ── 3. Detect extras ─────────────────────────────────────────────────────────

PIP_VER=""
if python3 -m pip --version &>/dev/null; then
    PIP_VER=$(python3 -m pip --version 2>/dev/null | awk '{print $2}')
fi

VENV_ACTIVE="no"
if [[ -n "${VIRTUAL_ENV:-}" ]]; then VENV_ACTIVE="yes"; fi

HAS_VENV_MOD="no"
if python3 -c "import venv" &>/dev/null; then HAS_VENV_MOD="yes"; fi

# ── 4. Print report ─────────────────────────────────────────────────────────

W=17   # column width

echo ""
printf "${B}${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}\n"
printf "${B}${BOLD}║         FFmpeg Builder — Python Environment Check          ║${NC}\n"
printf "${B}${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}\n"
echo ""

# ── Python interpreter ──
printf "  ${BOLD}Python Interpreter${NC}\n"
printf "    %-14s ${G}%s${NC}  (%s)\n" "python3:" "$PYTHON_VER" "$PYTHON_BIN"

if (( PYTHON_MAJOR < 3 || (PYTHON_MAJOR == 3 && PYTHON_MINOR < 8) )); then
    printf "    ${R}⚠ Python >= 3.8 required (found %s)${NC}\n" "$PYTHON_VER"
fi

# ── Platform ──
printf "\n  ${BOLD}Platform${NC}\n"
printf "    %-14s %s\n" "OS:" "$OS"
printf "    %-14s %s\n" "Distribution:" "$DISTRO"
printf "    %-14s %s\n" "Package mgr:" "$PKG_MGR"
if [[ "$IS_MSYS2" == "yes" ]]; then
    printf "    %-14s %s\n" "MSYSTEM:" "${MSYSTEM:-unknown}"
fi

# ── Environment ──
printf "\n  ${BOLD}Environment${NC}\n"
printf "    %-14s " "pip:"
if [[ -n "$PIP_VER" ]]; then
    printf "${G}%s${NC}\n" "$PIP_VER"
else
    printf "${R}not found${NC}\n"
fi

printf "    %-14s " "venv active:"
if [[ "$VENV_ACTIVE" == "yes" ]]; then
    printf "${G}yes${NC}  (%s)\n" "$VIRTUAL_ENV"
else
    printf "${Y}no${NC}\n"
fi

printf "    %-14s " "venv module:"
if [[ "$HAS_VENV_MOD" == "yes" ]]; then
    printf "${G}available${NC}\n"
else
    printf "${Y}not available${NC}\n"
fi

# ── 5. Check dependencies ────────────────────────────────────────────────────

echo ""
printf "  ${BOLD}Dependencies${NC}\n"
echo "    ┌─────────────────┬──────────┬──────────────┬──────────┐"
printf "    │ %-15s │ %-8s │ %-12s │ %-8s │\n" "Package" "Required" "Installed" "Status"
echo "    ├─────────────────┼──────────┼──────────────┼──────────┤"

MISSING=()
MET=()
UNMET=()

for entry in "${DEPS[@]}"; do
    read -r pip_name min_ver import_name apt_name dnf_name pacman_name zypper_name <<< "$entry"

    label=$(pad_right "$pip_name" 15)

    if check_module "$import_name" "$pip_name" "$apt_name"; then
        installed="$MOD_VERSION"
        inst_display=$(pad_right "${installed:-?}" 10)

        if [[ -z "$installed" ]]; then
            printf "    │ ${C}%s${NC} │ %-8s │ %-12s │ ${Y}%-8s${NC} │\n" \
                "$label" "$min_ver" "$inst_display" "no ver"
            MET+=("$pip_name")
        elif version_gte "$installed" "$min_ver"; then
            printf "    │ ${C}%s${NC} │ %-8s │ %-12s │ ${G}%-8s${NC} │\n" \
                "$label" "$min_ver" "$inst_display" "ok"
            MET+=("$pip_name")
        else
            printf "    │ ${C}%s${NC} │ %-8s │ %-12s │ ${R}%-8s${NC} │\n" \
                "$label" "$min_ver" "$inst_display" "< min"
            UNMET+=("$pip_name")
            MISSING+=("$pip_name|$apt_name|$dnf_name|$pacman_name|$zypper_name")
        fi
    else
        printf "    │ ${C}%s${NC} │ %-8s │ %-12s │ ${R}%-8s${NC} │\n" \
            "$label" "$min_ver" $(pad_right "—" 10) "missing"
        MISSING+=("$pip_name|$apt_name|$dnf_name|$pacman_name|$zypper_name")
    fi
done

echo "    └─────────────────┴──────────┴──────────────┴──────────┘"

# ── Summary ──
echo ""
printf "    ${G}✓ met: %d${NC}" "${#MET[@]}"
if (( ${#UNMET[@]} > 0 )); then
    printf "  ${R}✗ version too low: %d${NC}" "${#UNMET[@]}"
fi
if (( ${#MISSING[@]} == 0 && ${#UNMET[@]} == 0 )); then
    printf "  — ${G}${BOLD}all dependencies satisfied${NC}"
fi
echo ""

# ── 6. Installation commands ─────────────────────────────────────────────────

if (( ${#MISSING[@]} > 0 || ${#UNMET[@]} > 0 )); then
    echo ""
    printf "  ${BOLD}Installation commands${NC}\n"

    # Build per-manager command lists
    PIP_NAMES=()
    APT_NAMES=()
    DNF_NAMES=()
    PORT_NAMES=()
    PACMAN_NAMES=()
    ZYPPER_NAMES=()

    for item in "${MISSING[@]}"; do
        IFS='|' read -r pip_name apt_name dnf_name pacman_name zypper_name <<< "$item"
        PIP_NAMES+=("$pip_name")
        APT_NAMES+=("$apt_name")
        DNF_NAMES+=("$dnf_name")
        PACMAN_NAMES+=("$pacman_name")
        ZYPPER_NAMES+=("$zypper_name")

        # MacPorts: py3XX-<name>
        if [[ "$OS" == "Darwin" ]]; then
            PORT_NAMES+=("py${PYTHON_MAJOR}${PYTHON_MINOR}-${pip_name}")
        fi
    done

    # Also include version-unmet packages for upgrade
    for name in "${UNMET[@]}"; do
        already=false
        for item in "${MISSING[@]}"; do
            IFS='|' read -r pn _ <<< "$item"
            [[ "$pn" == "$name" ]] && already=true && break
        done
        if ! $already; then
            PIP_NAMES+=("$name")
        fi
    done

    col="    "

    # pip (universal)
    printf "\n${col}${DIM}# pip (universal)${NC}\n"
    if [[ "$VENV_ACTIVE" == "yes" ]]; then
        printf "${col}${G}pip install %s${NC}\n" "${PIP_NAMES[*]}"
    else
        printf "${col}${G}pip3 install --user %s${NC}\n" "${PIP_NAMES[*]}"
    fi

    # System package managers
    if [[ "$PKG_MGR" == "apt" ]]; then
        printf "\n${col}${DIM}# apt (Debian/Ubuntu)${NC}\n"
        printf "${col}${G}sudo apt install %s${NC}\n" "${APT_NAMES[*]}"
    fi

    if [[ "$PKG_MGR" == "dnf" ]]; then
        printf "\n${col}${DIM}# dnf (Fedora)${NC}\n"
        printf "${col}${G}sudo dnf install %s${NC}\n" "${DNF_NAMES[*]}"
    fi

    if [[ "$PKG_MGR" == "pacman" && "$IS_MSYS2" != "yes" ]]; then
        printf "\n${col}${DIM}# pacman (Arch)${NC}\n"
        printf "${col}${G}sudo pacman -S %s${NC}\n" "${PACMAN_NAMES[*]}"
    fi

    if [[ "$IS_MSYS2" == "yes" ]]; then
        MSYS2_PY_PKGS=()
        for pkg in "${PACMAN_NAMES[@]}"; do
            MSYS2_PY_PKGS+=("${MSYS2_MINGW_PREFIX}-${pkg}")
        done

        printf "\n${col}${DIM}# pacman (MSYS2 ${MSYSTEM:-UCRT64})${NC}\n"
        printf "${col}${G}pacman -S --needed %s${NC}\n" "${MSYS2_PY_PKGS[*]}"

        printf "\n${col}${DIM}# Full Windows + MSYS2 UCRT64 environment bootstrap${NC}\n"
        printf "${col}${G}powershell -ExecutionPolicy Bypass -File scripts\\setup_windows_msys2_ucrt64.ps1${NC}\n"
    fi

    if [[ "$PKG_MGR" == "zypper" ]]; then
        printf "\n${col}${DIM}# zypper (openSUSE)${NC}\n"
        printf "${col}${G}sudo zypper install %s${NC}\n" "${ZYPPER_NAMES[*]}"
    fi

    if [[ "$OS" == "Darwin" ]]; then
        printf "\n${col}${DIM}# MacPorts${NC}\n"
        printf "${col}${G}sudo port install %s${NC}\n" "${PORT_NAMES[*]}"
    fi

    # venv recommendation
    if [[ "$VENV_ACTIVE" == "no" ]]; then
        echo ""
        printf "${col}${Y}Tip: create a virtual environment first:${NC}\n"
        printf "${col}  python3 -m venv .venv && source .venv/bin/activate\n"
        printf "${col}  pip install -e .\n"
    fi
fi

echo ""

# ── Exit code ──

if (( ${#MISSING[@]} > 0 || ${#UNMET[@]} > 0 )); then
    exit 1
else
    exit 0
fi
