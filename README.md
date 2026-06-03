# TapeMan 2

A tape archival management system for Linux and macOS with LTO tape drives.

## Features

- **Archive datasets** with dual SHA256 checksum verification
- **Restore by Archive ID** with post-restore verification
- **Background jobs** — archive/restore/verify run as daemon processes, survive SSH disconnect
- **SQLite database** — full archive inventory with search, filtering, CSV export
- **Per-lab/PI metadata** tagging
- **Tar bundling** for small-file datasets (cryo-EM movies etc.)
- **Drive cleaning detection** via TapeAlert (sg_logs) and mt status
- **Robotic library support** via mtx — auto load/unload by label
- **Multi-drive library** support
- **TUI** — rich terminal interface, works over SSH
- **GUI** — Tkinter interface, works locally or via SSH -Y

## Hardware Tested

- Quantum LTO-9 Half-Height tape drive
- LSI SAS3008 HBA (mpt3sas driver)
- Rocky Linux 8.10

## Requirements

### Linux
- Rocky Linux 8 / RHEL 8
- Python 3.6+
- LTFS (built from source — see below)
- mt-st or mt
- lsscsi, sg3_utils
- mtx (optional — library/changer support)
- python3-tkinter (GUI only)

### macOS
- macFUSE
- IBM Spectrum Archive SDE or HPE StoreOpen (LTFS)
- SAS HBA with vendor driver
- mtx (optional — `brew install mtx`)

## Quick Install (Linux)

```bash
git clone https://github.com/dmartinbevan/tapeman2.git
cd tapeman2
sudo ./install.sh
sudo tapeman2-setup
tapeman2
```

The installer will build LTFS from source automatically if not already installed.

## Quick Install (macOS)

```bash
git clone https://github.com/dmartinbevan/tapeman2.git
cd tapeman2
sudo ./install-mac.sh
sudo tapeman2-setup
tapeman2
```

See `docs/README-mac.md` for macOS prerequisites.

## Updating

```bash
cd /usr/local/src/tapeman2   # or wherever you cloned it
git pull
sudo ./install.sh
```

The installer is idempotent — safe to re-run. Your config and database are preserved.

## File Structure

```
tapeman2/
├── tapeman2              # TUI (terminal interface)
├── tapeman2-gui          # GUI (Tkinter)
├── tapeman2-setup        # Linux configuration wizard
├── tapeman2-setup-mac    # macOS configuration wizard
├── install.sh            # Linux installer
├── install-mac.sh        # macOS installer
├── lib/
│   ├── tapeman2_core.py          # Core logic, DB, job manager
│   ├── tapeman2_changer.py       # Robotic library support (mtx)
│   ├── tapeman2_platform_linux.py # Linux platform layer
│   └── tapeman2_platform_mac.py  # macOS platform layer
├── conf/
│   ├── tapeman2.conf             # Linux config template
│   └── tapeman2-mac.conf         # macOS config template
├── docs/
│   ├── tape-guide.md             # User guide for researchers
│   └── README-mac.md             # macOS setup guide
└── legacy/
    └── tape-manage               # Original bash tapeman (v1)
```

## Install Paths (Linux)

| What | Where |
|------|-------|
| Binaries | `/usr/local/bin/tapeman2*` |
| Library | `/usr/local/lib/tapeman2/` |
| Config | `/etc/tapeman2/tapeman2.conf` |
| Database | `/var/lib/tapeman2/archives.db` |
| Jobs | `/var/lib/tapeman2/jobs/` |
| Log | `/var/log/tapeman2/tapeman2.log` |
| Staging | `/data/staging/` |
| Restore | `/data/restore/` |

## Install Paths (macOS)

| What | Where |
|------|-------|
| Binaries | `/usr/local/bin/tapeman2*` |
| Library | `/usr/local/lib/tapeman2/` |
| Config | `/usr/local/etc/tapeman2/tapeman2.conf` |
| Database | `/usr/local/var/tapeman2/archives.db` |
| Jobs | `/usr/local/var/tapeman2/jobs/` |

## Background Jobs

Archive, restore, and verify operations run as background daemon processes.
Jobs survive SSH disconnection and terminal closure.

```
tapeman2 → Archive → submits JOB-0001 → returns immediately
                          ↓
                   child process runs independently
                          ↓
                   writes progress to /var/lib/tapeman2/jobs/JOB-0001.json
                          ↓
                   tapeman2 Jobs menu shows live status
```

Check job status anytime:
- TUI: Main menu → option 6 (Jobs)
- GUI: Jobs tab (auto-refreshes every 5 seconds)

## LTFS Build Notes (Linux)

LTFS is not in standard Rocky Linux repos and must be built from source:

```bash
dnf install -y gcc gcc-c++ make automake autoconf libtool \
    fuse fuse-devel libxml2 libxml2-devel libicu libicu-devel icu \
    libuuid-devel zlib-devel git perl

git clone https://github.com/LinearTapeFileSystem/ltfs.git
cd ltfs
git submodule update --init --recursive
./autogen.sh
./configure --disable-snmp LIBS="-ldl"
make -j$(nproc)
make install && ldconfig
```

The `install.sh` script handles this automatically.

## License

MIT License — see LICENSE file.

## Author

Doug Bevan
