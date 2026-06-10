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

# ── Drive / Tape State Detection ─────────────────────────────────────────────

TAPE_STATE_READY        = "ready"
TAPE_STATE_INITIALIZING = "initializing"
TAPE_STATE_NO_TAPE      = "no_tape"
TAPE_STATE_NOT_READY    = "not_ready"
TAPE_STATE_UNKNOWN      = "unknown"

def tape_ready_progress(sg_device):
    """
    Parse 'becoming ready' progress percent from sg_turs, if sg3_utils
    is available on macOS (via Homebrew). Returns float 0-100 or None.
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
    Returns (state, message). macOS uses mt status; sg_turs if available.
    States: ready | initializing | no_tape | not_ready | unknown
    """
    result = run_cmd(["mt", "-f", st_device, "status"], timeout=10)
    if result.returncode == 0:
        out = result.stdout.upper()
        if "DR_OPEN" in out and "ONLINE" not in out:
            return TAPE_STATE_NO_TAPE, "No tape loaded in drive"
        if "ONLINE" in out:
            return TAPE_STATE_READY, "Tape ready"
        if "NOT READY" in out:
            return TAPE_STATE_NOT_READY, "Drive not ready"

    # sg_turs if sg3_utils installed (brew)
    if sg_device and check_tool("sg_turs"):
        result = run_cmd(["sg_turs", "-v", sg_device], timeout=10)
        text = (result.stdout + result.stderr).lower()
        if result.returncode != 0:
            pct = None
            m = re.search(r"progress indication:\s*([\d.]+)%", text)
            if m:
                try:
                    pct = float(m.group(1))
                except ValueError:
                    pct = None
            if "becoming ready" in text or "initializ" in text or pct is not None:
                if pct is not None:
                    return TAPE_STATE_INITIALIZING, \
                        "Tape initializing — {:.0f}% complete".format(pct)
                return TAPE_STATE_INITIALIZING, \
                    "Tape initializing — new LTO-9 cartridges require 15-30 min"
            if "no medium" in text or "no tape" in text:
                return TAPE_STATE_NO_TAPE, "No tape loaded"
            return TAPE_STATE_NOT_READY, "Drive not ready"
        return TAPE_STATE_READY, "Tape ready"

    return TAPE_STATE_UNKNOWN, "Could not determine drive state"

def wait_for_tape_ready(st_device, sg_device=None,
                         timeout_minutes=40, progress_cb=None):
    """Poll until tape ready or timeout. Returns (bool, message)."""
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
        mins, secs = divmod(elapsed, 60)
        timer = "{}m {}s".format(mins, secs) if mins else "{}s".format(secs)
        pct = tape_ready_progress(sg_device)
        if state == TAPE_STATE_INITIALIZING:
            if pct is not None:
                status_msg = "⏳ Tape initializing — {:.0f}% complete ({} elapsed)...".format(pct, timer)
            else:
                status_msg = "⏳ Tape initializing ({} elapsed) — please wait...".format(timer)
        else:
            status_msg = "⏳ Drive not ready ({} elapsed) — waiting...".format(timer)
        if progress_cb:
            progress_cb(status_msg)
        time.sleep(interval)
    return False, "Timed out after {} minutes waiting for tape.".format(timeout_minutes)

def read_error_counters(sg_device):
    """
    Read write/read error counters via sg_logs if sg3_utils is installed.
    Returns (write_errors, read_errors), or (0,0) if unavailable on macOS.
    """
    if not sg_device or not check_tool("sg_logs"):
        return 0, 0
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

def mount_tape(sg_device, mount_point, progress_cb=None, st_device=None):
    """
    Mount LTFS tape on macOS. Waits for tape initialization if needed,
    polls for the mount to appear (LTFS backgrounds itself), and enables
    multi-user access via allow_other.
    """
    from pathlib import Path
    import subprocess as _sp
    import time as _time

    Path(mount_point).mkdir(parents=True, exist_ok=True)
    if is_mounted(mount_point):
        return

    # Wait for tape readiness if we can detect it
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

    # LTFS daemonizes on success; poll for the mount rather than waiting
    # for the command to exit. allow_other lets any user reach the mount.
    proc = _sp.Popen(
        ["ltfs", "-o", "devname={}".format(sg_device),
         "-o", "allow_other", mount_point],
        stdout=_sp.PIPE, stderr=_sp.PIPE, universal_newlines=True)

    mount_timeout = 300
    waited = 0
    captured_err = ""
    while waited < mount_timeout:
        if is_mounted(mount_point):
            if progress_cb:
                progress_cb("✔ Tape mounted at {}".format(mount_point))
            return
        rc = proc.poll()
        if rc is not None:
            for _ in range(5):
                if is_mounted(mount_point):
                    if progress_cb:
                        progress_cb("✔ Tape mounted at {}".format(mount_point))
                    return
                _time.sleep(1)
            try:
                out, err = proc.communicate(timeout=5)
                captured_err = (err or "") + (out or "")
            except Exception:
                pass
            break
        _time.sleep(2)
        waited += 2
        if progress_cb and waited % 20 == 0:
            progress_cb("Still mounting... ({}s)".format(waited))

    if is_mounted(mount_point):
        return

    low = captured_err.lower()
    if "not partitioned" in low or "medium is not partitioned" in low:
        raise RuntimeError(
            "Tape is not formatted with LTFS.\n"
            "Format it first via Tape Management → Format tape.\n"
            "(Warning: formatting erases all data on the tape.)")
    if "no medium" in low or "no tape" in low:
        raise RuntimeError("No tape loaded. Please insert a cartridge.")
    if not captured_err.strip():
        raise RuntimeError(
            "Mount did not complete within {}s.\n"
            "The tape may still be initializing — check Drive status "
            "and try again once ready.".format(mount_timeout))
    raise RuntimeError("Failed to mount tape:\n{}".format(captured_err))

def unmount_tape(mount_point, progress_cb=None):
    if not is_mounted(mount_point):
        return
    if progress_cb:
        progress_cb("Flushing LTFS index to tape (please wait)...")
    result = unmount_fuse(mount_point)
    if is_mounted(mount_point):
        raise RuntimeError("Failed to unmount tape:\n{}".format(result.stderr))

def eject_tape(st_device, sg_device=None):
    """
    Rewind and eject the tape on macOS. Returns (ok, message).
    Rewind from end-of-tape can be slow, so allow a generous timeout.
    """
    result = run_cmd(["mt", "-f", st_device, "offline"], timeout=300)
    if result.returncode == 0:
        return True, "Tape ejected."

    err = (result.stderr or result.stdout or "").strip()
    # Fallback: SCSI unload via sg_start if sg3_utils is present
    if sg_device and check_tool("sg_start"):
        alt = run_cmd(["sg_start", "--eject", sg_device], timeout=300)
        if alt.returncode == 0:
            return True, "Tape ejected (via SCSI unload)."
        err = err or (alt.stderr or alt.stdout or "").strip()

    low = err.lower()
    if "busy" in low:
        return False, ("Eject failed — drive is busy. Unmount the tape first.")
    if "timeout" in low:
        return False, ("Eject timed out — the drive may still be rewinding. "
                       "Wait a moment and try again.")
    if "no medium" in low or "no tape" in low:
        return False, "No tape loaded in the drive."
    return False, ("Eject failed: {}".format(err) if err
                   else "Eject failed (no error detail from drive).")

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
