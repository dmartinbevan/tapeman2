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

# ── Drive / Tape State Detection ─────────────────────────────────────────────

TAPE_STATE_READY        = "ready"
TAPE_STATE_INITIALIZING = "initializing"
TAPE_STATE_NO_TAPE      = "no_tape"
TAPE_STATE_NOT_READY    = "not_ready"
TAPE_STATE_UNKNOWN      = "unknown"

def tape_ready_progress(sg_device):
    """
    Query sg_turs -v and parse the 'becoming ready' progress percentage.
    Returns float 0-100 if the drive reports progress, else None.
    """
    if not sg_device:
        return None
    result = run_cmd(["sg_turs", "-v", sg_device], timeout=10)
    text = result.stdout + result.stderr
    m = re.search(r"Progress indication:\s*([\d.]+)%", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None

def tape_drive_state(st_device, sg_device=None):
    """
    Returns (state, message).
    States: ready | initializing | no_tape | not_ready | unknown
    """
    import re
    result = run_cmd(["mt", "-f", st_device, "status"], timeout=10)
    if result.returncode == 0:
        out = result.stdout.upper()

        if "DR_OPEN" in out and "ONLINE" not in out:
            return TAPE_STATE_NO_TAPE, "No tape loaded in drive"

        # ONLINE means the tape is loaded and ready — block number is
        # often -1 on LTO drives even when fully ready, so don't check it
        if "ONLINE" in out:
            return TAPE_STATE_READY, "Tape ready"

        if "NOT READY" in out or "ILI" in out:
            return TAPE_STATE_NOT_READY, "Drive not ready"

    # sg_turs — Test Unit Ready (with progress detection)
    if sg_device:
        result = run_cmd(["sg_turs", "-v", sg_device], timeout=10)
        text = result.stdout + result.stderr
        if result.returncode != 0:
            low = text.lower()
            # Parse progress percentage if present
            pct = None
            m = re.search(r"progress indication:\s*([\d.]+)%", low)
            if m:
                try:
                    pct = float(m.group(1))
                except ValueError:
                    pct = None

            if "becoming ready" in low or "initializ" in low or "04/03" in low \
               or "04/01" in low or pct is not None:
                if pct is not None:
                    return TAPE_STATE_INITIALIZING, \
                        "Tape initializing — {:.0f}% complete".format(pct)
                return TAPE_STATE_INITIALIZING, \
                    "Tape initializing — new LTO-9 cartridges require 15-30 min"
            if "no medium" in low or "no tape" in low or "3a" in low:
                return TAPE_STATE_NO_TAPE, "No tape loaded"
            return TAPE_STATE_NOT_READY, "Drive not ready"
        return TAPE_STATE_READY, "Tape ready"

    return TAPE_STATE_UNKNOWN, "Could not determine drive state"

def wait_for_tape_ready(st_device, sg_device=None,
                         timeout_minutes=40, progress_cb=None):
    """
    Poll until tape is ready or timeout. Returns (bool, message).
    Displays live initialization progress percentage when available.
    """
    import time
    interval  = 10
    max_polls = (timeout_minutes * 60) // interval

    for poll in range(max_polls):
        state, msg = tape_drive_state(st_device, sg_device)

        if state == TAPE_STATE_READY:
            return True, "Tape ready."
        if state == TAPE_STATE_NO_TAPE:
            return False, "No tape loaded — please insert a cartridge."

        elapsed = poll * interval
        mins = elapsed // 60
        secs = elapsed % 60
        timer = "{}m {}s".format(mins, secs) if mins else "{}s".format(secs)

        # Try to get a live progress percentage
        pct = tape_ready_progress(sg_device)

        if state == TAPE_STATE_INITIALIZING:
            if pct is not None:
                status_msg = ("⏳ Tape initializing — {:.0f}% complete "
                              "({} elapsed)...".format(pct, timer))
            else:
                status_msg = ("⏳ Tape initializing ({} elapsed) — "
                              "new LTO-9 cartridges require 15-30 min. "
                              "Please wait...".format(timer))
        else:
            if pct is not None:
                status_msg = ("⏳ Drive becoming ready — {:.0f}% complete "
                              "({} elapsed)...".format(pct, timer))
            else:
                status_msg = "⏳ Drive not ready ({} elapsed) — waiting...".format(timer)

        if progress_cb:
            progress_cb(status_msg)

        time.sleep(interval)

    return False, "Timed out after {} minutes waiting for tape.".format(
        timeout_minutes)

def mount_tape(sg_device, mount_point, progress_cb=None, st_device=None):
    """
    Mount LTFS tape. Automatically waits for tape initialization if needed.
    Pass st_device to enable drive state detection.
    """
    from pathlib import Path
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    if is_mounted(mount_point):
        return
    load_fuse()

    # Check drive state and wait if initializing
    if st_device:
        state, msg = tape_drive_state(st_device, sg_device)
        if state == TAPE_STATE_NO_TAPE:
            raise RuntimeError("No tape loaded. Please insert a cartridge.")
        if state in (TAPE_STATE_INITIALIZING, TAPE_STATE_NOT_READY):
            if progress_cb:
                progress_cb(msg)
            ready, wait_msg = wait_for_tape_ready(
                st_device, sg_device, progress_cb=progress_cb)
            if not ready:
                raise RuntimeError(wait_msg)

    if progress_cb:
        progress_cb("Mounting tape...")
    result = run_cmd(
        ["ltfs", "-o", "devname={}".format(sg_device), mount_point],
        timeout=120
    )
    if not is_mounted(mount_point):
        stderr = result.stderr + result.stdout
        if "not partitioned" in stderr or "medium is not partitioned" in stderr:
            raise RuntimeError(
                "Tape is not formatted with LTFS.\n"
                "Format it first via Tape Management → Format tape.\n"
                "(Warning: formatting erases all data on the tape.)")
        if "no medium" in stderr.lower() or "no tape" in stderr.lower():
            raise RuntimeError("No tape loaded. Please insert a cartridge.")
        raise RuntimeError("Failed to mount tape:\n{}\n{}".format(
            result.stderr, result.stdout))

# ── Error Counters (tape health) ──────────────────────────────────────────────

def read_error_counters(sg_device):
    """
    Read write/read error counters from SCSI log pages.
    Page 0x02 = write errors, Page 0x03 = read errors.
    Returns (write_errors, read_errors). Returns (0,0) if unavailable.
    """
    def _sum_page(page):
        result = run_cmd(["sg_logs", "-p", page, sg_device], timeout=10)
        if result.returncode != 0:
            return 0
        total = 0
        for line in result.stdout.splitlines():
            low = line.lower()
            if "total" in low and "corrected" in low:
                m = re.search(r"=\s*(\d+)", line)
                if m:
                    total = max(total, int(m.group(1)))
        return total

    try:
        return _sum_page("0x02"), _sum_page("0x03")
    except Exception:
        return 0, 0

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
