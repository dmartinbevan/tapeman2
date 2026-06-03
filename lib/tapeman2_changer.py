#!/usr/bin/env python3
"""
tapeman2_changer.py — Robotic tape library / media changer support
Wraps the 'mtx' utility for changer control.
Imported by tapeman2_core when changer.enabled = true in config.
"""

import re
import subprocess
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("tapeman2")

# ── Exceptions ────────────────────────────────────────────────────────────────

class ChangerError(Exception):
    pass

# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class SlotInfo:
    slot_num: int
    slot_type: str          # "storage", "drive", "ie" (import/export)
    occupied: bool
    barcode: str = ""
    label: str = ""         # matched from DB
    drive_num: Optional[int] = None   # set if slot_type == "drive"
    loaded_from: Optional[int] = None # slot the tape came from (if in drive)

@dataclass
class LibraryStatus:
    drives: List[SlotInfo] = field(default_factory=list)
    slots: List[SlotInfo] = field(default_factory=list)
    ie_slots: List[SlotInfo] = field(default_factory=list)

    @property
    def all_slots(self) -> List[SlotInfo]:
        return self.slots + self.ie_slots + self.drives

    def find_tape_by_barcode(self, barcode: str) -> Optional[SlotInfo]:
        for s in self.all_slots:
            if s.barcode.upper() == barcode.upper():
                return s
        return None

    def find_tape_by_label(self, label: str) -> Optional[SlotInfo]:
        for s in self.all_slots:
            if s.label.upper() == label.upper() or s.barcode.upper() == label.upper():
                return s
        return None

    def free_slots(self) -> List[SlotInfo]:
        return [s for s in self.slots if not s.occupied]

    def loaded_drives(self) -> List[SlotInfo]:
        return [d for d in self.drives if d.occupied]

    def empty_drives(self) -> List[SlotInfo]:
        return [d for d in self.drives if not d.occupied]

# ── MTX Interface ─────────────────────────────────────────────────────────────

def _run_mtx(changer_device: str, args: list, timeout: int = 120) -> str:
    cmd = ["mtx", "-f", changer_device] + args
    log.debug("mtx cmd: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout
    )
    if result.returncode != 0:
        raise ChangerError(
            f"mtx command failed: {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout

def mtx_installed() -> bool:
    result = subprocess.run(
        ["which", "mtx"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )
    return result.returncode == 0

# ── Status Parsing ────────────────────────────────────────────────────────────

def get_library_status(changer_device: str) -> LibraryStatus:
    """
    Parse 'mtx status' output into a LibraryStatus object.

    Example mtx output lines:
      Storage Element 1:Empty
      Storage Element 2:Full :VolumeTag=CRYO01L9
      Data Transfer Element 0:Empty
      Data Transfer Element 0:Full (Storage Element 3 Loaded):VolumeTag=ARCH01L9
      Import/Export Element 1:Empty
    """
    raw = _run_mtx(changer_device, ["status"])
    status = LibraryStatus()

    for line in raw.splitlines():
        line = line.strip()

        # Data Transfer Element (drive)
        m = re.match(
            r"Data Transfer Element\s+(\d+):(Full|Empty)"
            r"(?:\s+\(Storage Element\s+(\d+) Loaded\))?"
            r"(?::VolumeTag=(\S+))?",
            line, re.IGNORECASE
        )
        if m:
            drive_num = int(m.group(1))
            occupied  = m.group(2).lower() == "full"
            loaded_from = int(m.group(3)) if m.group(3) else None
            barcode   = m.group(4).strip() if m.group(4) else ""
            status.drives.append(SlotInfo(
                slot_num=drive_num,
                slot_type="drive",
                occupied=occupied,
                barcode=barcode,
                drive_num=drive_num,
                loaded_from=loaded_from,
            ))
            continue

        # Import/Export Element
        m = re.match(
            r"Import/Export Element\s+(\d+):(Full|Empty)"
            r"(?::VolumeTag=(\S+))?",
            line, re.IGNORECASE
        )
        if m:
            slot_num = int(m.group(1))
            occupied = m.group(2).lower() == "full"
            barcode  = m.group(3).strip() if m.group(3) else ""
            status.ie_slots.append(SlotInfo(
                slot_num=slot_num,
                slot_type="ie",
                occupied=occupied,
                barcode=barcode,
            ))
            continue

        # Storage Element
        m = re.match(
            r"Storage Element\s+(\d+):(Full|Empty)"
            r"(?::VolumeTag=(\S+))?",
            line, re.IGNORECASE
        )
        if m:
            slot_num = int(m.group(1))
            occupied = m.group(2).lower() == "full"
            barcode  = m.group(3).strip() if m.group(3) else ""
            status.slots.append(SlotInfo(
                slot_num=slot_num,
                slot_type="storage",
                occupied=occupied,
                barcode=barcode,
            ))
            continue

    return status

# ── Changer Operations ────────────────────────────────────────────────────────

def load_tape(changer_device: str, slot: int, drive: int = 0,
              progress_cb=None) -> None:
    """Load tape from storage slot into drive."""
    if progress_cb:
        progress_cb(f"Loading tape from slot {slot} into drive {drive}...")
    log.info("changer: load slot=%d drive=%d", slot, drive)
    _run_mtx(changer_device, ["load", str(slot), str(drive)], timeout=120)
    if progress_cb:
        progress_cb(f"Tape loaded from slot {slot}.")

def unload_tape(changer_device: str, slot: int, drive: int = 0,
                progress_cb=None) -> None:
    """Return tape from drive back to storage slot."""
    if progress_cb:
        progress_cb(f"Returning tape from drive {drive} to slot {slot}...")
    log.info("changer: unload drive=%d slot=%d", drive, slot)
    _run_mtx(changer_device, ["unload", str(slot), str(drive)], timeout=120)
    if progress_cb:
        progress_cb(f"Tape returned to slot {slot}.")

def transfer_tape(changer_device: str, src_slot: int, dst_slot: int,
                  progress_cb=None) -> None:
    """Move tape between two storage slots (no drive involved)."""
    if progress_cb:
        progress_cb(f"Moving tape from slot {src_slot} to slot {dst_slot}...")
    log.info("changer: transfer src=%d dst=%d", src_slot, dst_slot)
    _run_mtx(changer_device, ["transfer", str(src_slot), str(dst_slot)], timeout=120)

def inventory(changer_device: str, progress_cb=None) -> None:
    """Force library to re-read barcodes (inventory scan)."""
    if progress_cb:
        progress_cb("Running library inventory scan...")
    log.info("changer: inventory scan")
    _run_mtx(changer_device, ["inventory"], timeout=300)
    if progress_cb:
        progress_cb("Inventory scan complete.")

def eject_to_ie(changer_device: str, slot: int, ie_slot: int,
                progress_cb=None) -> None:
    """Move tape from storage to import/export slot for removal."""
    transfer_tape(changer_device, slot, ie_slot, progress_cb)

# ── High-Level: Auto-load by label ────────────────────────────────────────────

def find_and_load(
    changer_device: str,
    tape_label: str,
    drive: int = 0,
    progress_cb=None,
) -> int:
    """
    Find a tape by label/barcode in the library and load it into a drive.
    Returns the slot number the tape was loaded from.
    Raises ChangerError if tape not found.
    """
    status = get_library_status(changer_device)

    # Check if already in the target drive
    if drive < len(status.drives):
        drv = status.drives[drive]
        if drv.occupied and (
            drv.barcode.upper() == tape_label.upper() or
            drv.label.upper() == tape_label.upper()
        ):
            if progress_cb:
                progress_cb(f"Tape '{tape_label}' is already in drive {drive}.")
            return drv.loaded_from or -1

    # Search storage slots
    slot = status.find_tape_by_label(tape_label)
    if not slot:
        raise ChangerError(
            f"Tape '{tape_label}' not found in library.\n"
            f"Check that the tape is loaded in the library and the label/barcode matches."
        )

    if slot.slot_type == "drive":
        if progress_cb:
            progress_cb(f"Tape '{tape_label}' is already in drive {slot.drive_num}.")
        return slot.loaded_from or -1

    # Unload current tape from drive if occupied
    if drive < len(status.drives) and status.drives[drive].occupied:
        current = status.drives[drive]
        return_slot = current.loaded_from
        if return_slot is None:
            raise ChangerError(
                f"Drive {drive} has a tape loaded but origin slot is unknown. "
                f"Manually unload before proceeding."
            )
        unload_tape(changer_device, return_slot, drive, progress_cb)

    load_tape(changer_device, slot.slot_num, drive, progress_cb)
    return slot.slot_num

def unload_to_home(
    changer_device: str,
    drive: int = 0,
    progress_cb=None,
) -> Optional[int]:
    """
    Unload tape from drive back to its home slot.
    Returns slot number or None if drive was empty.
    """
    status = get_library_status(changer_device)
    if drive >= len(status.drives):
        raise ChangerError(f"Drive {drive} not found in library.")

    drv = status.drives[drive]
    if not drv.occupied:
        if progress_cb:
            progress_cb(f"Drive {drive} is already empty.")
        return None

    if drv.loaded_from is None:
        raise ChangerError(
            f"Drive {drive} has a tape but home slot is unknown. Unload manually."
        )

    unload_tape(changer_device, drv.loaded_from, drive, progress_cb)
    return drv.loaded_from

# ── Label Resolution: match barcodes to DB labels ─────────────────────────────

def annotate_with_db(status: LibraryStatus, db_tapes: list) -> None:
    """
    Cross-reference barcode labels in library status with tape records in DB.
    Mutates status in-place, setting slot.label where a match is found.
    """
    barcode_map = {}
    for tape in db_tapes:
        if tape.barcode:
            barcode_map[tape.barcode.upper()] = tape.label
        barcode_map[tape.label.upper()] = tape.label

    for slot in status.all_slots:
        if slot.barcode:
            slot.label = barcode_map.get(slot.barcode.upper(), slot.barcode)

# ── Multi-drive job queue (simple sequential) ─────────────────────────────────

class DriveQueue:
    """
    Simple sequential job queue for multi-drive libraries.
    Assigns jobs to the next available drive.
    For parallel execution, jobs run in threads — see tapeman2_core.
    """
    def __init__(self, drive_count: int):
        self.drive_count = drive_count
        self._busy = {i: False for i in range(drive_count)}

    def next_free_drive(self) -> Optional[int]:
        for i in range(self.drive_count):
            if not self._busy[i]:
                return i
        return None

    def mark_busy(self, drive: int) -> None:
        self._busy[drive] = True

    def mark_free(self, drive: int) -> None:
        self._busy[drive] = False

    def all_free(self) -> bool:
        return all(not v for v in self._busy.values())
