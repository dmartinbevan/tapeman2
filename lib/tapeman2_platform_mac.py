#!/usr/bin/env python3
"""
tapeman2_platform_mac.py — macOS platform layer for tapeman2
Includes TapeAlert cleaning detection via sg_logs / mt status.
"""

import os
import re
import subprocess
import logging
from typing import List, Dict, Tuple

log = logging.getLogger("tapeman2")

PLATFORM = "darwin"

# ── Default Device Paths ──────────────────────────────────────────────────────

DEFAULT_SG_DEVICE      = "/dev/sg0"
DEFAULT_ST_DEVICE      = "/dev/nrmt0"
DEFAULT_CHANGER_DEVICE = "/dev/ch0"
DEFAULT_MOUNT_POINT    = "/Volumes/tape"
DEFAULT_STAGING_DIR    = "/usr/local/var/tapeman2/staging"
DEFAULT_RESTORE_DIR    = "/usr/local/var/tapeman2/restore"
DEFAULT_STATE_DIR      = "/usr/local/var/tapeman2"
DEFAULT_LOG_FILE       = "/usr/local/var/log/tapeman2/tapeman2.log"
DEFAULT_DB_PATH        = "/usr/local/var/tapeman2/archives.db"
DEFAULT_CONFIG_PATH    = "/usr/local/etc/tapeman2/tapeman2.conf"

# ── Shell Commands ────────────────────────────────────────────────────────────

def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout
        )
        return result
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "timeout")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 1, "", "command not found")

# ── FUSE / Mount ──────────────────────────────────────────────────────────────

def load_fuse():
    """macFUSE loads automatically on Mac — nothing to do."""
    pass

def unmount_fuse(mount_point):
    return run_cmd(["umount", mount_point], timeout=300)

def is_mounted(mount_point):
    result = run_cmd(["mountpoint", "-q", mount_point])
    if result.returncode == 0:
        return True
    out = run_cmd(["mount"]).stdout
    return mount_point in out

def mount_tape(sg_device, mount_point, progress_cb=None):
    from pathlib import Path
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    if is_mounted(mount_point):
        return
    if progress_cb:
        progress_cb("Mounting tape...")
    result = run_cmd(
        ["ltfs", "-o", "devname={}".format(sg_device), mount_point],
        timeout=120
    )
    if not is_mounted(mount_point):
        raise RuntimeError("Failed to mount tape:\n{}\n{}".format(
            result.stderr, result.stdout))

def unmount_tape(mount_point, progress_cb=None):
    if not is_mounted(mount_point):
        return
    if progress_cb:
        progress_cb("Flushing LTFS index to tape (please wait)...")
    result = unmount_fuse(mount_point)
    if is_mounted(mount_point):
        raise RuntimeError("Failed to unmount tape:\n{}".format(result.stderr))

def eject_tape(st_device):
    run_cmd(["mt", "-f", st_device, "offline"], timeout=30)

def tape_status(st_device):
    result = run_cmd(["mt", "-f", st_device, "status"], timeout=10)
    return result.stdout

# ── Cleaning Detection ────────────────────────────────────────────────────────

def check_cleaning_needed(sg_device, st_device=None) -> Tuple[bool, str]:
    """
    Check if the tape drive needs cleaning.

    Returns (needs_cleaning: bool, message: str)

    Tries three methods in order:
    1. sg_logs TapeAlert page 0x2e  (requires sg3_utils: brew install sg3_utils)
    2. mt status NEED_CLEANING bit
    3. High write error count heuristic

    Returns (False, "ok") if drive does not need cleaning or cannot be determined.
    Returns (True, reason) if cleaning is needed.
    """

    # ── Method 1: sg_logs TapeAlert (0x2e) ───────────────────────────────────
    result = run_cmd(["sg_logs", "-p", "0x2e", sg_device], timeout=10)
    if result.returncode == 0 and result.stdout:
        out = result.stdout
        for line in out.splitlines():
            line_lower = line.lower()
            flag_set = "[0x1]" in line or "= 1" in line.lower()
            if flag_set:
                if "clean now" in line_lower:
                    log.warning("TapeAlert: Clean Now flag set on %s", sg_device)
                    return True, "Clean Now (TapeAlert flag 0x20)"
                if "clean periodic" in line_lower:
                    log.warning("TapeAlert: Clean Periodic flag set on %s", sg_device)
                    return True, "Periodic cleaning recommended (TapeAlert flag 0x21)"
        return False, "ok (TapeAlert)"

    # ── Method 2: mt status NEED_CLEANING ────────────────────────────────────
    if st_device:
        result = run_cmd(["mt", "-f", st_device, "status"], timeout=10)
        if result.returncode == 0:
            out = result.stdout.upper()
            if "NEED_CLEANING" in out:
                log.warning("mt status: NEED_CLEANING on %s", st_device)
                return True, "Clean Now (mt status)"
            if "CLN" in out:
                return True, "Cleaning required (mt status)"

    # ── Method 3: write error counter heuristic ───────────────────────────────
    result = run_cmd(["sg_logs", "-p", "0x08", sg_device], timeout=10)
    if result.returncode == 0 and result.stdout:
        for line in result.stdout.splitlines():
            if "errors corrected" in line.lower():
                m = re.search(r"(\d+)", line)
                if m and int(m.group(1)) > 1000:
                    return True, "High write error count — consider cleaning"

    return False, "unknown (install sg3_utils: brew install sg3_utils)"

def cleaning_status_str(sg_device, st_device=None) -> Tuple[bool, str]:
    try:
        return check_cleaning_needed(sg_device, st_device)
    except Exception as e:
        log.debug("cleaning check error: %s", e)
        return False, "unknown"

# ── Device Detection ──────────────────────────────────────────────────────────

def detect_tape_drives() -> List[Dict]:
    drives = []

    # Method 1: system_profiler
    drives = _detect_via_system_profiler()
    if drives:
        return drives

    # Method 2: ioreg
    drives = _detect_via_ioreg()
    if drives:
        return drives

    # Method 3: /dev scan
    return _detect_via_dev_scan()

def _detect_via_system_profiler() -> List[Dict]:
    drives = []
    result = run_cmd(["system_profiler", "SPSASDataType", "-json"], timeout=30)
    if result.returncode != 0:
        return drives
    try:
        import json
        data = json.loads(result.stdout)
        items = data.get("SPSASDataType", [])
        idx = 0
        for item in items:
            for key, val in item.items():
                if isinstance(val, dict):
                    dev_type = val.get("sas_device_type", "")
                    if "tape" in dev_type.lower() or "sequential" in dev_type.lower():
                        model = val.get("sas_product_name", "Unknown")
                        drives.append({
                            "sg":    "/dev/sg{}".format(idx),
                            "st":    "/dev/rmt{}".format(idx),
                            "nst":   "/dev/nrmt{}".format(idx),
                            "model": model,
                            "index": idx,
                        })
                        idx += 1
    except Exception:
        pass
    return drives

def _detect_via_ioreg() -> List[Dict]:
    drives = []
    result = run_cmd(
        ["ioreg", "-c", "IOSCSIPeripheralDeviceType01", "-r", "-l"],
        timeout=30
    )
    if result.returncode != 0:
        return drives
    idx = 0
    current = {}
    for line in result.stdout.splitlines():
        if "Product Identification" in line or "kIOPropertyProductNameKey" in line:
            m = re.search(r'"([^"]+)"$', line.strip())
            if m:
                current["model"] = m.group(1).strip()
        if "Vendor Identification" in line:
            m = re.search(r'"([^"]+)"$', line.strip())
            if m:
                current["vendor"] = m.group(1).strip()
        if current.get("model"):
            model = "{} {}".format(
                current.get("vendor", ""), current.get("model", "")).strip()
            drives.append({
                "sg":    "/dev/sg{}".format(idx),
                "st":    "/dev/rmt{}".format(idx),
                "nst":   "/dev/nrmt{}".format(idx),
                "model": model,
                "index": idx,
            })
            idx += 1
            current = {}
    return drives

def _detect_via_dev_scan() -> List[Dict]:
    drives = []
    idx = 0
    while os.path.exists("/dev/rmt{}".format(idx)):
        drives.append({
            "sg":    "/dev/sg{}".format(idx),
            "st":    "/dev/rmt{}".format(idx),
            "nst":   "/dev/nrmt{}".format(idx),
            "model": "Unknown (detected via /dev scan)",
            "index": idx,
        })
        idx += 1
    return drives

def detect_changers() -> List[Dict]:
    changers = []
    for i in range(4):
        dev = "/dev/ch{}".format(i)
        if os.path.exists(dev):
            changers.append({"sg": dev, "model": "Media Changer (ch{})".format(i)})

    result = run_cmd(["system_profiler", "SPSASDataType", "-json"], timeout=30)
    if result.returncode == 0:
        try:
            import json
            data = json.loads(result.stdout)
            for item in data.get("SPSASDataType", []):
                for key, val in item.items():
                    if isinstance(val, dict):
                        dev_type = val.get("sas_device_type", "")
                        if "changer" in dev_type.lower() or "medium" in dev_type.lower():
                            model = val.get("sas_product_name", "Unknown Changer")
                            sg = "/dev/sg{}".format(len(changers))
                            if not any(c["sg"] == sg for c in changers):
                                changers.append({"sg": sg, "model": model})
        except Exception:
            pass
    return changers

# ── Tool Detection ────────────────────────────────────────────────────────────

def check_tool(name) -> bool:
    result = run_cmd(["which", name])
    return result.returncode == 0

def check_macfuse() -> bool:
    paths = [
        "/Library/Filesystems/macfuse.fs",
        "/Library/Extensions/macfuse.kext",
        "/usr/local/lib/libfuse.dylib",
    ]
    return any(os.path.exists(p) for p in paths)

def check_ltfs() -> bool:    return check_tool("ltfs")
def check_mkltfs() -> bool:  return check_tool("mkltfs")
def check_mtx() -> bool:     return check_tool("mtx")
def check_sg_logs() -> bool: return check_tool("sg_logs")
def check_brew() -> bool:    return check_tool("brew")

MACFUSE_URL  = "https://github.com/osxfuse/osxfuse/releases"
LTFS_IBM_URL = "https://www.ibm.com/support/pages/ibm-spectrum-archive-single-drive-edition"
LTFS_HPE_URL = "https://h20392.www2.hp.com/portal/swdepot/displayProductInfo.do?productNumber=HPLTFS"

def install_guidance() -> str:
    return """
macOS Setup Requirements:

1. macFUSE:
   brew install --cask macfuse
   OR: {macfuse}
   (Allow in System Preferences → Security after install, then reboot)

2. LTFS (choose one):
   IBM Spectrum Archive SDE: {ltfs_ibm}
   HPE StoreOpen:            {ltfs_hpe}

3. sg3_utils (for cleaning detection):
   brew install sg3_utils

4. mtx (library/changer support):
   brew install mtx

5. mt tape control:
   brew install cdrtools

6. SAS HBA driver: install vendor driver (ATTO/Areca/LSI), reboot.
""".format(macfuse=MACFUSE_URL, ltfs_ibm=LTFS_IBM_URL, ltfs_hpe=LTFS_HPE_URL)
