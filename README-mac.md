# TapeMan 2 — macOS Edition

Tape archival management for macOS with locally connected SAS tape drive or library.

---

## Prerequisites

### 1. macFUSE
Required for LTFS filesystem support.

```bash
brew install --cask macfuse
```

After installing: **System Preferences → Security & Privacy → Allow** the macFUSE kernel extension. Reboot required.

### 2. LTFS Package (choose one)

**IBM Spectrum Archive SDE for Mac (recommended):**
https://www.ibm.com/support/pages/ibm-spectrum-archive-single-drive-edition

**HPE StoreOpen for Mac:**
https://h20392.www2.hp.com/portal/swdepot/displayProductInfo.do?productNumber=HPLTFS

Both install `ltfs`, `mkltfs`, and `ltfsck` to `/usr/local/bin`.

### 3. SAS HBA Driver
Install the driver for your HBA **before** connecting the tape drive:
- **ATTO**: https://www.atto.com/software/
- **Areca**: https://www.areca.com.tw/support/
- **LSI/Broadcom**: https://www.broadcom.com/support/

Reboot after driver install.

### 4. Optional: mtx (library/changer support)
```bash
brew install mtx
```

### 5. Optional: mt (tape control)
```bash
brew install cdrtools
```

---

## Install

```bash
sudo ./install-mac.sh
sudo tapeman2-setup
```

---

## Usage

```bash
# TUI (terminal)
tapeman2

# GUI (native window)
tapeman2-gui

# Reconfigure
sudo tapeman2-setup
```

---

## Install Paths

| What | Where |
|------|-------|
| Core library | `/usr/local/lib/tapeman2/` |
| Binaries | `/usr/local/bin/tapeman2` etc. |
| Config | `/usr/local/etc/tapeman2/tapeman2.conf` |
| Database | `/usr/local/var/tapeman2/archives.db` |
| Log | `/usr/local/var/log/tapeman2/tapeman2.log` |
| Staging | `/usr/local/var/tapeman2/staging/` |
| Restore | `/usr/local/var/tapeman2/restore/` |

---

## Tape Device Paths on macOS

| Device | Description |
|--------|-------------|
| `/dev/rmt0` | Tape drive (rewind on close) |
| `/dev/nrmt0` | Tape drive (no rewind) — use this one |
| `/dev/ch0` | Media changer (robotic library) |
| `/dev/sg0` | SCSI generic (for LTFS) |

---

## Differences from Linux Version

| Feature | Linux | macOS |
|---------|-------|-------|
| Tape device | `/dev/nst0` | `/dev/nrmt0` |
| Changer device | `/dev/sg1` | `/dev/ch0` |
| Mount point | `/mnt/tape` | `/Volumes/tape` |
| Unmount | `fusermount -u` | `umount` |
| FUSE | kernel module | macFUSE kext |
| Config path | `/etc/tapeman2/` | `/usr/local/etc/tapeman2/` |
| State/DB | `/var/lib/tapeman2/` | `/usr/local/var/tapeman2/` |

---

## Troubleshooting

**"no display name and no $DISPLAY"** when running tapeman2-gui:
The GUI runs natively on Mac — just run `tapeman2-gui` from Terminal, no X11 needed.

**Drive not detected:**
```bash
system_profiler SPSASDataType
```
Check HBA driver is loaded and drive is powered on.

**LTFS won't mount:**
Check macFUSE is installed and allowed in System Preferences → Security.

**"ltfs: command not found":**
Install IBM Spectrum Archive SDE or HPE StoreOpen — see Prerequisites above.
