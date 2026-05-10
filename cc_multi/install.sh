#!/usr/bin/env bash
# install.sh — CPSM installer for Linux.
#
# Usage:
#   ./install.sh <path-to-CPSM-x.y.z-x86_64.AppImage>
#
# Installs runtime dependencies (tmux, sshpass, openssh-client/clients,
# terminal emulator) via the detected package manager, places the AppImage
# under /opt/cpsm (system) or ~/.local/opt/cpsm (per-user), symlinks a
# `cpsm` command into PATH, and registers a freedesktop .desktop launcher.
#
# Re-running is safe; already-installed components are skipped.

set -euo pipefail

# ── Argument & environment validation ──────────────────────────────────────

APPIMAGE_PATH="${1:-}"
if [[ -z "$APPIMAGE_PATH" || "$APPIMAGE_PATH" == "-h" || "$APPIMAGE_PATH" == "--help" ]]; then
    cat <<EOF
Usage: $0 <path-to-CPSM-x.y.z-x86_64.AppImage>

Installs CPSM and its runtime dependencies on this Linux system.

Behavior:
  - If run as root:      installs to /opt/cpsm, symlinks /usr/local/bin/cpsm
  - If run as a user:    installs to ~/.local/opt/cpsm, symlinks ~/.local/bin/cpsm
                         (requires sudo for package installation only)

Supports: Debian/Ubuntu/Mint/Pop!_OS, Fedora/RHEL/Rocky/Alma, Arch/Manjaro,
openSUSE, Alpine.  Other distros will get a manual-install message listing
the required packages.

Examples:
  ./install.sh CPSM-0.1.0-x86_64.AppImage             # per-user install
  sudo ./install.sh CPSM-0.1.0-x86_64.AppImage        # system-wide install
EOF
    exit 1
fi

if [[ ! -f "$APPIMAGE_PATH" ]]; then
    echo "Error: AppImage not found at '$APPIMAGE_PATH'" >&2
    exit 2
fi
APPIMAGE_PATH="$(readlink -f "$APPIMAGE_PATH")"

# Color output when stdout is a TTY.
if [[ -t 1 ]]; then
    RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; BLU=$'\e[34m'; RST=$'\e[0m'
else
    RED=''; YEL=''; GRN=''; BLU=''; RST=''
fi
info()  { echo "${BLU}info${RST}   $*"; }
warn()  { echo "${YEL}warn${RST}   $*" >&2; }
error() { echo "${RED}error${RST}  $*" >&2; }
ok()    { echo "${GRN}ok${RST}     $*"; }

# ── Distro / package manager / GUI detection ───────────────────────────────

DISTRO_ID="unknown"
DISTRO_LIKE=""
DISTRO_NAME=""
PM=""
HAS_GUI=0

detect_distro() {
    if [[ ! -f /etc/os-release ]]; then
        error "/etc/os-release missing — cannot detect distro"
        exit 3
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-unknown}"
    DISTRO_LIKE="${ID_LIKE:-}"
    DISTRO_NAME="${PRETTY_NAME:-$DISTRO_ID}"
    info "Distro: $DISTRO_NAME (id=$DISTRO_ID)"
}

detect_pm() {
    for candidate in apt-get dnf yum pacman zypper apk; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PM="$candidate"
            info "Package manager: $PM"
            return 0
        fi
    done
    error "No supported package manager found (apt-get/dnf/yum/pacman/zypper/apk)"
    error "Install tmux + sshpass + openssh-client + a terminal emulator manually"
    exit 4
}

detect_gui() {
    if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
        HAS_GUI=1
    else
        HAS_GUI=0
        info "Headless session — terminal emulator + desktop entry will be skipped"
    fi
}

# ── Privilege model ────────────────────────────────────────────────────────

SUDO=""
INSTALL_TYPE=""
INSTALL_DIR=""
BIN_DIR=""

decide_install_target() {
    if [[ "$EUID" -eq 0 ]]; then
        SUDO=""
        INSTALL_TYPE="system"
        INSTALL_DIR="/opt/cpsm"
        BIN_DIR="/usr/local/bin"
    elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
        INSTALL_TYPE="user"
        INSTALL_DIR="$HOME/.local/opt/cpsm"
        BIN_DIR="$HOME/.local/bin"
    else
        error "Not running as root and 'sudo' is not available"
        error "Re-run with sudo, or install sudo first"
        exit 5
    fi
    info "Install mode: $INSTALL_TYPE  (binary: $INSTALL_DIR  symlink: $BIN_DIR)"
}

# ── Package name mapping per distro ────────────────────────────────────────
# Empty values mean "this distro doesn't ship the package in default repos —
# skip and warn".  PKG_TERM is the terminal emulator we install when none is
# already present; xterm is the universal cheapest option.

PKG_KEYGEN=""
PKG_TMUX=""
PKG_SSHPASS=""
PKG_TERM=""
NEEDS_EPEL=0

map_packages() {
    case "$DISTRO_ID" in
        debian|ubuntu|linuxmint|raspbian|pop|elementary|kali)
            PKG_KEYGEN="openssh-client"
            PKG_TMUX="tmux"
            PKG_SSHPASS="sshpass"
            PKG_TERM="xterm"
            ;;
        fedora|rhel|centos|rocky|almalinux|ol)
            PKG_KEYGEN="openssh-clients"
            PKG_TMUX="tmux"
            PKG_SSHPASS="sshpass"
            PKG_TERM="xterm"
            NEEDS_EPEL=1
            ;;
        arch|manjaro|endeavouros|garuda|artix)
            PKG_KEYGEN="openssh"
            PKG_TMUX="tmux"
            PKG_SSHPASS=""  # AUR-only
            PKG_TERM="xterm"
            ;;
        opensuse*|suse|sles)
            PKG_KEYGEN="openssh-clients"
            PKG_TMUX="tmux"
            PKG_SSHPASS="sshpass"
            PKG_TERM="xterm"
            ;;
        alpine)
            PKG_KEYGEN="openssh-keygen"
            PKG_TMUX="tmux"
            PKG_SSHPASS="sshpass"
            PKG_TERM="xterm"
            ;;
        *)
            # Try ID_LIKE for derivatives we don't recognize directly.
            for like in $DISTRO_LIKE; do
                case "$like" in
                    debian|ubuntu)
                        warn "Unknown distro '$DISTRO_ID' — treating as Debian/Ubuntu"
                        DISTRO_ID="$like"; map_packages; return ;;
                    fedora|rhel)
                        warn "Unknown distro '$DISTRO_ID' — treating as Fedora/RHEL"
                        DISTRO_ID="$like"; map_packages; return ;;
                    arch)
                        warn "Unknown distro '$DISTRO_ID' — treating as Arch"
                        DISTRO_ID="$like"; map_packages; return ;;
                    suse)
                        warn "Unknown distro '$DISTRO_ID' — treating as openSUSE"
                        DISTRO_ID="opensuse"; map_packages; return ;;
                esac
            done
            error "Unsupported distro '$DISTRO_ID' — install these manually:"
            error "  - tmux"
            error "  - sshpass         (for password-based SSH key deploy)"
            error "  - openssh-client  (for ssh-keygen)"
            error "  - a terminal emulator (xterm, konsole, alacritty, …)"
            exit 6
            ;;
    esac
}

# ── Dependency check ───────────────────────────────────────────────────────

NEED_KEYGEN=0
NEED_TMUX=0
NEED_SSHPASS=0
NEED_TERM=0
FOUND_TERM=""

check_deps() {
    command -v ssh-keygen >/dev/null 2>&1 || NEED_KEYGEN=1
    command -v tmux       >/dev/null 2>&1 || NEED_TMUX=1
    command -v sshpass    >/dev/null 2>&1 || NEED_SSHPASS=1
    if [[ "$HAS_GUI" -eq 1 ]]; then
        for term in xterm konsole alacritty kitty wezterm gnome-terminal foot xfce4-terminal; do
            if command -v "$term" >/dev/null 2>&1; then
                FOUND_TERM="$term"; break
            fi
        done
        if [[ -z "$FOUND_TERM" ]]; then NEED_TERM=1; fi
    fi

    local present=()
    local missing=()
    [[ $NEED_KEYGEN  -eq 0 ]] && present+=("ssh-keygen") || missing+=("ssh-keygen")
    [[ $NEED_TMUX    -eq 0 ]] && present+=("tmux")       || missing+=("tmux")
    [[ $NEED_SSHPASS -eq 0 ]] && present+=("sshpass")    || missing+=("sshpass")
    if [[ "$HAS_GUI" -eq 1 ]]; then
        [[ -n "$FOUND_TERM" ]] && present+=("term=$FOUND_TERM") || missing+=("terminal-emulator")
    fi
    info "Already present: ${present[*]:-none}"
    if [[ ${#missing[@]} -gt 0 ]]; then
        info "Will install:    ${missing[*]}"
    fi
}

# ── Package installation ───────────────────────────────────────────────────

install_packages() {
    local to_install=()
    [[ $NEED_KEYGEN  -eq 1 && -n "$PKG_KEYGEN"  ]] && to_install+=("$PKG_KEYGEN")
    [[ $NEED_TMUX    -eq 1 && -n "$PKG_TMUX"    ]] && to_install+=("$PKG_TMUX")
    [[ $NEED_SSHPASS -eq 1 && -n "$PKG_SSHPASS" ]] && to_install+=("$PKG_SSHPASS")
    [[ $NEED_TERM    -eq 1 && -n "$PKG_TERM"    ]] && to_install+=("$PKG_TERM")

    if [[ ${#to_install[@]} -eq 0 ]]; then
        ok "All dependencies already installed"
        return 0
    fi

    info "Installing via $PM: ${to_install[*]}"

    case "$PM" in
        apt-get)
            $SUDO apt-get update -qq
            DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y "${to_install[@]}"
            ;;
        dnf|yum)
            if [[ "$NEEDS_EPEL" -eq 1 && $NEED_SSHPASS -eq 1 ]]; then
                if ! rpm -q epel-release >/dev/null 2>&1; then
                    info "Enabling EPEL repository (sshpass lives there on RHEL family)"
                    $SUDO "$PM" install -y epel-release \
                        || warn "EPEL enable failed — sshpass install may fail too"
                fi
            fi
            $SUDO "$PM" install -y "${to_install[@]}"
            ;;
        pacman)
            $SUDO pacman -Sy --noconfirm --needed "${to_install[@]}"
            ;;
        zypper)
            $SUDO zypper --non-interactive install "${to_install[@]}"
            ;;
        apk)
            $SUDO apk add "${to_install[@]}"
            ;;
        *)
            error "Internal error: unknown PM=$PM"
            exit 7
            ;;
    esac
    ok "Package install complete"
}

# ── AppImage placement ─────────────────────────────────────────────────────

place_appimage() {
    info "Installing AppImage to $INSTALL_DIR/cpsm.AppImage"
    $SUDO mkdir -p "$INSTALL_DIR" "$BIN_DIR"
    $SUDO cp -f "$APPIMAGE_PATH" "$INSTALL_DIR/cpsm.AppImage"
    $SUDO chmod 0755 "$INSTALL_DIR/cpsm.AppImage"
    $SUDO ln -sf "$INSTALL_DIR/cpsm.AppImage" "$BIN_DIR/cpsm"
    ok "Installed: $BIN_DIR/cpsm → $INSTALL_DIR/cpsm.AppImage"
}

# ── Desktop integration ────────────────────────────────────────────────────

register_desktop() {
    if [[ "$HAS_GUI" -eq 0 ]]; then
        info "Skipping desktop entry (headless system)"
        return 0
    fi
    info "Registering desktop launcher via 'cpsm install-desktop'"
    # cpsm install-desktop writes ~/.local/share/applications/cpsm.desktop
    # for the invoking user.  System-wide install can re-run it as each
    # user later if desired.  We pass --force so a stale entry from an
    # older install is overwritten.
    if "$BIN_DIR/cpsm" install-desktop --force; then
        ok "Desktop entry registered"
    else
        warn "cpsm install-desktop returned non-zero — launcher may not appear"
        warn "You can retry manually: $BIN_DIR/cpsm install-desktop --force"
    fi
}

# ── Final report ───────────────────────────────────────────────────────────

report() {
    echo ""
    echo "════════════════════════════════════════════════════════════════════════"
    ok  "CPSM installation complete"
    echo ""
    echo "  Run from terminal:  ${BLU}cpsm gui${RST}"
    if [[ "$HAS_GUI" -eq 1 ]]; then
        echo "  From app menu:      ${BLU}CPSM${RST} (Development / System category)"
    fi
    echo ""
    if [[ $NEED_SSHPASS -eq 1 && -z "$PKG_SSHPASS" ]]; then
        warn "sshpass was not installed automatically on this distro."
        case "$DISTRO_ID" in
            arch|manjaro|endeavouros|garuda|artix)
                warn "Arch ships sshpass via the AUR.  Install with an AUR helper, e.g.:"
                warn "    yay -S sshpass        # or paru -S sshpass"
                ;;
            *)
                warn "Install sshpass manually if you need password-based key deployment."
                ;;
        esac
    fi
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        warn "$BIN_DIR is not in your \$PATH"
        warn "Add it to your shell profile, e.g.:"
        warn "    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc"
    fi
    echo "════════════════════════════════════════════════════════════════════════"
}

# ── Main ───────────────────────────────────────────────────────────────────

main() {
    detect_distro
    detect_pm
    detect_gui
    decide_install_target
    map_packages
    check_deps
    install_packages
    place_appimage
    register_desktop
    report
}

main "$@"
