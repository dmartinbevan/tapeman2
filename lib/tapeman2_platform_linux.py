#!/usr/bin/env python3
"""
tapeman2_platform_linux.py — Linux platform layer for tapeman2
Includes TapeAlert cleaning detection via sg_logs / mt status.
"""

import os
import re
import subprocess
import logging
from typing import List, Dict, Tuple

log = logging.getLogger("tapeman2")

PLATFORM = "linux"

# ── Default Device Paths ──────────────────────────────────────────────────────

DEFAULT_SG_DEVICE      = "/dev/sg0"
DEFAULT_ST_DEVICE      = "/dev/nst0"
DEFAULT_CHANGER_DEVICE = "/dev/sg1"
DEFAULT_MOUNT_POINT    = "/mnt/tape"
DEFAULT_STAGING_DIR    = "/data/staging"
DEFAULT_RESTORE_DIR    = "/data/restore"
DEFAULT_STATE_DIR      = "/var/lib/tapeman2"
DEFAULT_LOG_FILE       = "/var/log/tapeman2/tapeman2.log"
DEFAULT_DB_PATH        = "/var/lib/tapeman2/archives.db"
DEFAULT_CONFIG_PATH    = "/etc/tapeman2/tapeman2.conf"

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
    run_cmd(["modprobe", "fuse"])

def unmount_fuse(mount_point):
    return run_cmd(["fusermount", "-u", mount_point], timeout=300)

def is_mounted(mount_point):
    result = run_cmd(["mountpoint", "-q", mount_point])
    return result.returncode == 0

def mount_tape(sg_device, mount_point, progress_cb=None):
    from pathlib import Path
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    if is_mounted(mount_point):
        return
    load_fuse()
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

# TapeAlert log page (0x2e) flag meanings we care about
_TAPEALERT_CLEAN_FLAGS = {
    "20": "Clean now",
    "21": "Clean periodic",
    "0x0020": "Clean now",
    "0x0021": "Clean periodic",
}

def check_cleaning_needed(sg_device, st_device=None) -> Tuple[bool, str]:
    """
    Check if the tape drive needs cleaning.

    Returns (needs_cleaning: bool, message: str)

    Tries three methods in order:
    1. sg_logs TapeAlert page (most reliable — requires sg3_utils)
    2. mt status NEED_CLEANING bit
    3. Quantum-specific sense data via sg_logs general page

    Returns (False, "ok") if drive does not need cleaning or cannot be determined.
    Returns (True, reason) if cleaning is needed.
    """

    # ── Method 1: sg_logs TapeAlert (0x2e) ───────────────────────────────────
    result = run_cmd(["sg_logs", "-p", "0x2e", sg_device], timeout=10)
    if result.returncode == 0 and result.stdout:
        out = result.stdout
        # Parse TapeAlert flags — look for flags set to "1" (active)
        # sg_logs output format:
        #   TapeAlert[0x20]: Clean now [0x1]
        #   TapeAlert[0x21]: Clean periodic [0x1]
        for line in out.splitlines():
            line_lower = line.lower()
            # Flag is set if value ends in [0x1] or = 1
            flag_set = "[0x1]" in line or "= 1" in line.lower()
            if flag_set:
                if "clean now" in line_lower:
                    log.warning("TapeAlert: Clean Now flag set on %s", sg_device)
                    return True, "Clean Now (TapeAlert flag 0x20)"
                if "clean periodic" in line_lower:
                    log.warning("TapeAlert: Clean Periodic flag set on %s", sg_device)
                    return True, "Periodic cleaning recommended (TapeAlert flag 0x21)"
        # sg_logs ran successfully, no cleaning flags set
        return False, "ok (TapeAlert)"

    # ── Method 2: mt status NEED_CLEANING bit ────────────────────────────────
    if st_device:
        result = run_cmd(["mt", "-f", st_device, "status"], timeout=10)
        if result.returncode == 0:
            out = result.stdout.upper()
            if "NEED_CLEANING" in out:
                log.warning("mt status: NEED_CLEANING on %s", st_device)
                return True, "Clean Now (mt status)"
            if "CLN" in out:
                return True, "Cleaning required (mt status)"

    # ── Method 3: sg_logs general error counter / vendor page ────────────────
    # Some Quantum drives report cleaning via page 0x08 (write error counters)
    # or the vendor-specific page. Try page 0x08 for high error counts.
    result = run_cmd(["sg_logs", "-p", "0x08", sg_device], timeout=10)
    if result.returncode == 0 and result.stdout:
        for line in result.stdout.splitlines():
            if "errors corrected" in line.lower():
                m = re.search(r"(\d+)", line)
                if m:
                    count = int(m.group(1))
                    if count > 1000:
                        return True, "High write error count — consider cleaning"

    # ── Could not determine — return safe default ─────────────────────────────
    return False, "unknown (sg_logs not available — install sg3_utils)"

def cleaning_status_str(sg_device, st_device=None) -> Tuple[bool, str]:
    """
    Convenience wrapper — returns (needs_cleaning, display_string).
    Safe to call even if drive is not present.
    """
    try:
        needed, msg = check_cleaning_needed(sg_device, st_device)
        return needed, msg
    except Exception as e:
        log.debug("cleaning check error: %s", e)
        return False, "unknown"

# ── Device Detection ──────────────────────────────────────────────────────────

def detect_tape_drives() -> List[Dict]:
    drives = []
    result = run_cmd(["lsscsi", "-g"])
    if result.returncode != 0:
        return drives
    st_idx = 0
    for line in result.stdout.splitlines():
        if "tape" in line.lower():
            parts = line.split()
            sg    = next((p for p in parts if p.startswith("/dev/sg")), "")
            model = " ".join(parts[2:5]) if len(parts) >= 5 else "Unknown"
            drives.append({
                "sg":    sg,
                "st":    "/dev/st{}".format(st_idx),
                "nst":   "/dev/nst{}".format(st_idx),
                "model": model,
                "index": st_idx,
            })
            st_idx += 1
    return drives

def detect_changers() -> List[Dict]:
    changers = []
    result = run_cmd(["lsscsi", "-g"])
    if result.returncode != 0:
        return changers
    for line in result.stdout.splitlines():
        if "mediumx" in line.lower() or "changer" in line.lower():
            parts = line.split()
            sg    = next((p for p in parts if p.startswith("/dev/sg")), "")
            model = " ".join(parts[2:5]) if len(parts) >= 5 else "Unknown"
            changers.append({"sg": sg, "model": model})
    return changers

# ── Tool Detection ────────────────────────────────────────────────────────────

def check_tool(name) -> bool:
    result = run_cmd(["which", name])
    return result.returncode == 0

def check_ltfs() -> bool:   return check_tool("ltfs")
def check_mkltfs() -> bool: return check_tool("mkltfs")
def check_mtx() -> bool:    return check_tool("mtx")
def check_sg_logs() -> bool: return check_tool("sg_logs")
def check_macfuse() -> bool: return True   # N/A on Linux
def check_brew() -> bool:    return False  # N/A on Linux

def install_guidance() -> str:
    return """
Linux Setup Requirements:

1. LTFS (build from source):
   git clone -b master https://github.com/LinearTapeFileSystem/ltfs.git
   cd ltfs && git submodule update --init --recursive
   ./autogen.sh && ./configure --disable-snmp LIBS="-ldl"
   make -j$(nproc) && make install && ldconfig

2. mt-st:       dnf install mt-st
3. lsscsi:      dnf install lsscsi
4. sg3_utils:   dnf install sg3_utils   (for cleaning detection)
5. mtx:         dnf install mtx         (library/changer support)
6. rsync:       dnf install rsync
"""
