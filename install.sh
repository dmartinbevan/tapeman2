#!/bin/bash
# tapeman2 install script
# Rocky Linux 8 / RHEL 8
# Run as root

set -e

echo ""
echo "  TapeMan 2 Installer"
echo "  ==================="
echo ""

# ── Check root ────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "  Run as root: sudo ./install.sh"
    exit 1
fi

# ── Directories ───────────────────────────────────────────────────────────────
echo "Creating directories..."
mkdir -p /usr/local/lib/tapeman2
mkdir -p /etc/tapeman2
mkdir -p /var/lib/tapeman2
mkdir -p /var/log/tapeman2
mkdir -p /data/staging
mkdir -p /data/restore

# Ensure tape group exists
if ! getent group tape &>/dev/null; then
    groupadd tape
    echo "  Created 'tape' group."
fi

# Config dir readable by all
chown root:root /etc/tapeman2
chmod 755       /etc/tapeman2

# State/log/staging dirs writable by tape group
chown root:tape /var/log/tapeman2 /var/lib/tapeman2 /data/staging /data/restore
chmod 775       /var/log/tapeman2 /var/lib/tapeman2 /data/staging /data/restore

# Pre-create log file with correct permissions so non-root users can write to it
touch /var/log/tapeman2/tapeman2.log
chown root:tape /var/log/tapeman2/tapeman2.log
chmod 664       /var/log/tapeman2/tapeman2.log

# ── System packages ───────────────────────────────────────────────────────────
echo "Installing system packages..."

# LTFS build dependencies (idempotent — safe to run even if LTFS already built)
dnf install -y \
    gcc gcc-c++ make automake autoconf libtool \
    fuse fuse-devel fuse-libs \
    libxml2 libxml2-devel \
    libicu libicu-devel \
    icu \
    libuuid-devel \
    zlib-devel \
    git perl \
    mt-st \
    lsscsi \
    sg3_utils \
    mtx \
    rsync \
    python3-tkinter \
    2>/dev/null || true

# mt-st may be called 'mt' on some systems — check either
if ! command -v mt &>/dev/null && ! command -v mt-st &>/dev/null; then
    echo "  WARNING: mt/mt-st not found — tape control may not work"
fi

echo "  System packages done."

# ── LTFS — build from source if not already installed ─────────────────────────
if ! command -v ltfs &>/dev/null || ! command -v mkltfs &>/dev/null; then
    echo ""
    echo "LTFS not found — building from source (this takes a few minutes)..."
    echo ""

    cd /usr/local/src

    if [ ! -d ltfs ]; then
        git clone https://github.com/LinearTapeFileSystem/ltfs.git
    else
        echo "  LTFS source already present — skipping clone."
    fi

    cd ltfs
    git submodule update --init --recursive

    ./autogen.sh
    ./configure --disable-snmp LIBS="-ldl"
    make -j$(nproc)
    make install
    ldconfig

    echo ""
    echo "  LTFS built and installed."
    cd /
else
    echo "  LTFS already installed — skipping build."
fi

# ── Python dependencies ───────────────────────────────────────────────────────
echo "Installing Python dependencies..."
pip3 install rich 2>/dev/null || \
    pip3 install --break-system-packages rich 2>/dev/null || \
    echo "  (rich not available — TUI will use plain text fallback)"

# ── Copy tapeman2 files ───────────────────────────────────────────────────────
echo "Copying tapeman2 files..."

# Determine script directory so we can find our files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp "$SCRIPT_DIR/tapeman2_core.py"            /usr/local/lib/tapeman2/tapeman2_core.py
cp "$SCRIPT_DIR/tapeman2_changer.py"         /usr/local/lib/tapeman2/tapeman2_changer.py
cp "$SCRIPT_DIR/tapeman2_platform_linux.py"  /usr/local/lib/tapeman2/tapeman2_platform_linux.py
cp "$SCRIPT_DIR/tapeman2_platform_mac.py"    /usr/local/lib/tapeman2/tapeman2_platform_mac.py
cp "$SCRIPT_DIR/tapeman2"                    /usr/local/bin/tapeman2
cp "$SCRIPT_DIR/tapeman2-gui"               /usr/local/bin/tapeman2-gui
cp "$SCRIPT_DIR/tapeman2-setup"             /usr/local/bin/tapeman2-setup
cp "$SCRIPT_DIR/tapeman2-update"            /usr/local/bin/tapeman2-update

# ── Permissions ───────────────────────────────────────────────────────────────
echo "Setting permissions..."
chmod 755 /usr/local/bin/tapeman2
chmod 755 /usr/local/bin/tapeman2-gui
chmod 755 /usr/local/bin/tapeman2-setup
chmod 755 /usr/local/bin/tapeman2-update
chmod 644 /usr/local/lib/tapeman2/tapeman2_core.py
chmod 644 /usr/local/lib/tapeman2/tapeman2_changer.py
chmod 644 /usr/local/lib/tapeman2/tapeman2_platform_linux.py
chmod 644 /usr/local/lib/tapeman2/tapeman2_platform_mac.py

# ── Tape device permissions ───────────────────────────────────────────────────
echo "Configuring tape device permissions..."

# Set current device permissions (world accessible — no per-user config needed)
for dev in /dev/sg0 /dev/sg1 /dev/sg2 /dev/sg3 \
           /dev/sg4 /dev/sg5 /dev/sg6 /dev/sg7 \
           /dev/st0 /dev/st1 /dev/nst0 /dev/nst1; do
    if [ -e "$dev" ]; then
        chmod 666 "$dev"
        echo "  Set $dev world-accessible."
    fi
done

# Persistent udev rules — survive reboot
cat > /etc/udev/rules.d/99-tape.rules << 'EOF'
SUBSYSTEM=="scsi_generic", ATTRS{type}=="1", MODE="0666"
SUBSYSTEM=="scsi_tape", MODE="0666"
EOF
udevadm control --reload-rules
udevadm trigger
echo "  udev rules installed."

# ── Config ────────────────────────────────────────────────────────────────────
if [ ! -f /etc/tapeman2/tapeman2.conf ]; then
    cp "$SCRIPT_DIR/tapeman2.conf" /etc/tapeman2/tapeman2.conf
    echo "  Config installed at /etc/tapeman2/tapeman2.conf"
else
    echo "  Config already exists — not overwriting."
fi
chmod 644 /etc/tapeman2/tapeman2.conf

# ── Initialize DB ─────────────────────────────────────────────────────────────
echo "Initializing database..."
python3 -c "
import sys
sys.path.insert(0, '/usr/local/lib/tapeman2')
import tapeman2_core as c
c.initialize()
print('  Database ready.')
" || echo "  (DB will initialize on first run of tapeman2-setup)"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ✔ tapeman2 installed successfully."
echo ""
echo "  Next step:  tapeman2-setup"
echo ""
echo "  TUI:        tapeman2"
echo "  GUI:        tapeman2-gui  (requires SSH -Y or local display)"
echo "  Update:     sudo tapeman2-update"
echo ""
echo "  Config:     /etc/tapeman2/tapeman2.conf"
echo "  Database:   /var/lib/tapeman2/archives.db"
echo "  Log:        /var/log/tapeman2/tapeman2.log"
echo "  Staging:    /data/staging/"
echo "  Restore:    /data/restore/"
echo ""
