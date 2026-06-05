#!/bin/bash
# tapeman2 macOS installer
# Run as: sudo ./install-mac.sh
#
# Works from a cloned repo (files in lib/ and conf/) or a flat directory.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "  TapeMan 2 — macOS Installer"
echo "  ============================"
echo ""

if [[ $EUID -ne 0 ]]; then
    echo "  Run as root: sudo ./install-mac.sh"
    exit 1
fi

# Locate source files (repo layout vs flat)
if [ -d "$SCRIPT_DIR/lib" ]; then LIBDIR="$SCRIPT_DIR/lib"; else LIBDIR="$SCRIPT_DIR"; fi
if [ -d "$SCRIPT_DIR/conf" ]; then CONFDIR="$SCRIPT_DIR/conf"; else CONFDIR="$SCRIPT_DIR"; fi

echo "  Creating directories..."
mkdir -p /usr/local/lib/tapeman2
mkdir -p /usr/local/etc/tapeman2
mkdir -p /usr/local/var/tapeman2
mkdir -p /usr/local/var/tapeman2/jobs
mkdir -p /usr/local/var/tapeman2/staging
mkdir -p /usr/local/var/tapeman2/restore
mkdir -p /usr/local/var/log/tapeman2
mkdir -p /Volumes/tape 2>/dev/null || true

chmod 1777 /usr/local/var/tapeman2 /usr/local/var/tapeman2/jobs \
           /usr/local/var/tapeman2/staging /usr/local/var/tapeman2/restore \
           /usr/local/var/log/tapeman2 2>/dev/null || true

echo "  Checking Python..."
PYTHON=$(which python3 || which python)
if [ -z "$PYTHON" ]; then
    echo "  Python 3 not found. Install via: brew install python"
    exit 1
fi
echo "  Using: $($PYTHON --version 2>&1)"

echo "  Installing Python dependencies..."
$PYTHON -m pip install rich --quiet 2>/dev/null || \
    echo "  (rich unavailable — TUI will use plain text)"

if command -v brew &>/dev/null; then
    echo "  Checking optional tools (mtx, sg3_utils)..."
    brew list mtx &>/dev/null       || brew install mtx 2>/dev/null || echo "  (mtx skipped)"
    brew list sg3_utils &>/dev/null || brew install sg3_utils 2>/dev/null || echo "  (sg3_utils skipped — tape health/progress limited)"
else
    echo "  brew not found — skipping optional tools."
fi

# macFUSE allow_other for multi-user mount access
if [ -f /etc/fuse.conf ]; then
    grep -q '^user_allow_other' /etc/fuse.conf || echo "user_allow_other" >> /etc/fuse.conf
else
    echo "user_allow_other" > /etc/fuse.conf 2>/dev/null || true
fi

echo "  Installing files..."
cp "$LIBDIR/tapeman2_core.py"           /usr/local/lib/tapeman2/
cp "$LIBDIR/tapeman2_changer.py"        /usr/local/lib/tapeman2/
cp "$LIBDIR/tapeman2_platform_mac.py"   /usr/local/lib/tapeman2/
cp "$LIBDIR/tapeman2_platform_linux.py" /usr/local/lib/tapeman2/
cp "$SCRIPT_DIR/tapeman2"               /usr/local/bin/tapeman2
cp "$SCRIPT_DIR/tapeman2-gui"           /usr/local/bin/tapeman2-gui
cp "$SCRIPT_DIR/tapeman2-update"        /usr/local/bin/tapeman2-update
if [ -f "$SCRIPT_DIR/tapeman2-setup-mac" ]; then
    cp "$SCRIPT_DIR/tapeman2-setup-mac" /usr/local/bin/tapeman2-setup
else
    cp "$SCRIPT_DIR/tapeman2-setup"     /usr/local/bin/tapeman2-setup
fi

chmod 755 /usr/local/bin/tapeman2 /usr/local/bin/tapeman2-gui \
          /usr/local/bin/tapeman2-setup /usr/local/bin/tapeman2-update
chmod 644 /usr/local/lib/tapeman2/*.py

if [ ! -f /usr/local/etc/tapeman2/tapeman2.conf ]; then
    if [ -f "$CONFDIR/tapeman2-mac.conf" ]; then
        cp "$CONFDIR/tapeman2-mac.conf" /usr/local/etc/tapeman2/tapeman2.conf
    else
        cp "$CONFDIR/tapeman2.conf" /usr/local/etc/tapeman2/tapeman2.conf
    fi
    echo "  Config installed at /usr/local/etc/tapeman2/tapeman2.conf"
else
    echo "  Config already exists — not overwriting."
fi

echo "  Initializing database..."
$PYTHON -c "
import sys
sys.path.insert(0, '/usr/local/lib/tapeman2')
import tapeman2_core as c
c.initialize('/usr/local/etc/tapeman2/tapeman2.conf')
print('  Database ready.')
"
[ -f /usr/local/var/tapeman2/archives.db ] && chmod 666 /usr/local/var/tapeman2/archives.db

echo ""
echo "  ✔ tapeman2 installed."
echo ""
echo "  Next step:  sudo tapeman2-setup"
echo "  TUI:        tapeman2"
echo "  GUI:        tapeman2-gui"
echo "  Update:     sudo tapeman2-update"
echo ""
echo "  NOTE: LTFS and macFUSE must be installed separately (see tapeman2-setup)."
echo ""
