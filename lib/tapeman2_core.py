#!/usr/bin/env python3
"""
tapeman2_core.py — Core logic for tapeman2
All archive, restore, verify, DB, checksum, tape, and changer operations.
Shared by both the TUI and GUI frontends.
Platform-aware: works on Linux and macOS.
"""

import configparser
import hashlib
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Tuple

# ── Platform detection ────────────────────────────────────────────────────────

_PLATFORM = platform.system().lower()   # "linux" or "darwin"
IS_MAC    = _PLATFORM == "darwin"
IS_LINUX  = _PLATFORM == "linux"

# Load platform module
_plat = None

def _get_platform():
    global _plat
    if _plat is not None:
        return _plat
    _lib = "/usr/local/lib/tapeman2"
    if _lib not in sys.path:
        sys.path.insert(0, _lib)
    if IS_MAC:
        import tapeman2_platform_mac as _plat
    else:
        import tapeman2_platform_linux as _plat
    return _plat

# Convenience wrappers — core always calls these, never platform directly
def _is_mounted(mount_point):
    return _get_platform().is_mounted(mount_point)

def _mount_tape(sg_device, mount_point, progress_cb=None):
    _get_platform().mount_tape(sg_device, mount_point, progress_cb)

def _unmount_tape(mount_point, progress_cb=None):
    _get_platform().unmount_tape(mount_point, progress_cb)

def _eject_tape(st_device):
    _get_platform().eject_tape(st_device)

def _tape_status(st_device):
    return _get_platform().tape_status(st_device)

def _detect_drives():
    return _get_platform().detect_tape_drives()

def _detect_changers():
    return _get_platform().detect_changers()

# Public API (used by frontends)
def is_mounted(mount_point):
    return _is_mounted(mount_point)

def detect_tape_drives():
    return _detect_drives()

def detect_changers():
    return _detect_changers()

def platform_name():
    return "macOS" if IS_MAC else "Linux"

# ── Optional changer support ──────────────────────────────────────────────────

_changer_mod = None

def _get_changer():
    global _changer_mod
    if _changer_mod is None:
        _lib = "/usr/local/lib/tapeman2"
        if _lib not in sys.path:
            sys.path.insert(0, _lib)
        import tapeman2_changer as _changer_mod
    return _changer_mod

def changer_available():
    try:
        _get_changer()
        return True
    except ImportError:
        return False

# ── Config ────────────────────────────────────────────────────────────────────

# Config path is platform-dependent
if IS_MAC:
    CONFIG_PATH = "/usr/local/etc/tapeman2/tapeman2.conf"
else:
    CONFIG_PATH = "/etc/tapeman2/tapeman2.conf"

def load_config(path=CONFIG_PATH):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg

def changer_enabled(cfg):
    return cfg.getboolean("changer", "enabled", fallback=False)

def get_changer_device(cfg):
    return cfg.get("changer", "device", fallback="/dev/sg1")

def get_drive_count(cfg):
    return cfg.getint("changer", "drive_count", fallback=1)

def get_slot_count(cfg):
    return cfg.getint("changer", "slot_count", fallback=0)

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(log_file):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tapeman2")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    return logger

# ── Database ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS tapes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT    NOT NULL UNIQUE,
    barcode         TEXT,
    slot_number     INTEGER DEFAULT -1,
    date_first_used TEXT,
    notes           TEXT,
    last_verified   TEXT,
    total_archives  INTEGER DEFAULT 0,
    total_bytes     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS archives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    lab             TEXT,
    pi              TEXT,
    notes           TEXT,
    source_path     TEXT    NOT NULL,
    tape_label      TEXT    NOT NULL,
    tape_path       TEXT    NOT NULL,
    tar_bundle      INTEGER DEFAULT 0,
    size_bytes      INTEGER,
    file_count      INTEGER,
    checksum_src    TEXT,
    checksum_tape   TEXT,
    date_archived   TEXT,
    date_verified   TEXT,
    date_restored   TEXT,
    status          TEXT    DEFAULT 'archived',
    FOREIGN KEY (tape_label) REFERENCES tapes(label)
);

CREATE TABLE IF NOT EXISTS restore_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      TEXT    NOT NULL,
    restore_dest    TEXT    NOT NULL,
    date_restored   TEXT    NOT NULL,
    checksum_ok     INTEGER,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS changer_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    operation       TEXT    NOT NULL,
    slot            INTEGER,
    drive           INTEGER,
    tape_label      TEXT,
    result          TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS file_manifest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      TEXT    NOT NULL,
    tape_label      TEXT    NOT NULL,
    file_path       TEXT    NOT NULL,
    size_bytes      INTEGER
);

CREATE TABLE IF NOT EXISTS tape_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tape_label      TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    operation       TEXT,
    write_errors    INTEGER DEFAULT 0,
    read_errors     INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_manifest_path
    ON file_manifest(file_path);
CREATE INDEX IF NOT EXISTS idx_manifest_archive
    ON file_manifest(archive_id);
"""

@contextmanager
def get_db(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(db_path):
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migrate older DBs
        for col, defn in [("barcode", "TEXT"), ("slot_number", "INTEGER DEFAULT -1")]:
            try:
                conn.execute("ALTER TABLE tapes ADD COLUMN {} {}".format(col, defn))
            except Exception:
                pass
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS changer_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL, operation TEXT NOT NULL,
                    slot INTEGER, drive INTEGER, tape_label TEXT,
                    result TEXT, notes TEXT
                );
                CREATE TABLE IF NOT EXISTS file_manifest (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    archive_id TEXT NOT NULL, tape_label TEXT NOT NULL,
                    file_path TEXT NOT NULL, size_bytes INTEGER
                );
                CREATE TABLE IF NOT EXISTS tape_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tape_label TEXT NOT NULL, timestamp TEXT NOT NULL,
                    operation TEXT, write_errors INTEGER DEFAULT 0,
                    read_errors INTEGER DEFAULT 0, notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_manifest_path
                    ON file_manifest(file_path);
                CREATE INDEX IF NOT EXISTS idx_manifest_archive
                    ON file_manifest(archive_id);
            """)
        except Exception:
            pass

def next_archive_id(db_path):
    with get_db(db_path) as conn:
        row = conn.execute("SELECT MAX(id) FROM archives").fetchone()
        next_id = (row[0] or 0) + 1
    return "ARCH-{:04d}".format(next_id)

# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class ArchiveRecord:
    archive_id: str
    name: str
    tape_label: str
    tape_path: str
    source_path: str
    size_bytes: int = 0
    file_count: int = 0
    checksum_src: str = ""
    checksum_tape: str = ""
    date_archived: str = ""
    status: str = "archived"
    lab: str = ""
    pi: str = ""
    notes: str = ""
    tar_bundle: bool = False
    date_verified: str = ""
    date_restored: str = ""

@dataclass
class TapeRecord:
    label: str
    barcode: str = ""
    slot_number: int = -1
    date_first_used: str = ""
    notes: str = ""
    last_verified: str = ""
    total_archives: int = 0
    total_bytes: int = 0

# ── Errors ────────────────────────────────────────────────────────────────────

class TapeError(Exception):
    pass

class ArchiveError(Exception):
    pass

# ── Shell Commands ────────────────────────────────────────────────────────────

def run_cmd(cmd, timeout=300):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout
    )
    return result

# ── Tape Operations (platform-delegated) ─────────────────────────────────────

def is_mounted(mount_point):
    return _get_platform().is_mounted(mount_point)

def mount_tape(sg_device, mount_point, progress_cb=None, st_device=None):
    _get_platform().mount_tape(sg_device, mount_point, progress_cb,
                               st_device=st_device)

def unmount_tape(mount_point, progress_cb=None):
    _get_platform().unmount_tape(mount_point, progress_cb)

def eject_tape(st_device):
    _get_platform().eject_tape(st_device)

def format_tape(sg_device, serial, volume_name="", progress_cb=None):
    """
    Format a tape with LTFS.
    serial      — exactly 6 alphanumeric characters (LTFS requirement)
    volume_name — optional longer descriptive name (stored in LTFS label)
    """
    # Validate serial
    import re
    serial = serial.upper().strip()
    if not re.match(r'^[A-Z0-9]{6}$', serial):
        raise TapeError(
            "Tape serial must be exactly 6 alphanumeric characters (e.g. LAB001).\n"
            "Got: '{}'".format(serial))

    if progress_cb:
        progress_cb("Formatting tape serial='{}' name='{}' — this may take several minutes...".format(
            serial, volume_name or serial))

    cmd = ["mkltfs", "-d", sg_device, "-s", serial]
    if volume_name:
        cmd += ["-n", volume_name]

    result = run_cmd(cmd, timeout=3600)
    if "succeeded" not in result.stdout and "succeeded" not in result.stderr:
        raise TapeError("Format failed:\n{}{}".format(result.stdout, result.stderr))

def tape_status(st_device):
    return _get_platform().tape_status(st_device)

def drive_detected(sg_device):
    drives = _get_platform().detect_tape_drives()
    return any(d["sg"] == sg_device for d in drives)

def tape_free_bytes(mount_point):
    if not is_mounted(mount_point):
        return 0
    return shutil.disk_usage(mount_point).free

def tape_total_bytes(mount_point):
    if not is_mounted(mount_point):
        return 0
    return shutil.disk_usage(mount_point).total

def tape_drive_state(st_device, sg_device=None):
    """Return (state, message) for current drive/tape state."""
    try:
        return _get_platform().tape_drive_state(st_device, sg_device)
    except Exception:
        return _get_platform().TAPE_STATE_UNKNOWN, "unknown"

# ── Cleaning Detection ────────────────────────────────────────────────────────

def check_cleaning_needed(sg_device, st_device=None):
    """
    Check if the tape drive needs cleaning.
    Delegates to platform module.
    Returns (needs_cleaning: bool, message: str)
    """
    try:
        return _get_platform().check_cleaning_needed(sg_device, st_device)
    except Exception as e:
        logging.getLogger("tapeman2").debug("cleaning check failed: %s", e)
        return False, "unknown"

def cleaning_status_str(sg_device, st_device=None):
    """Non-blocking cleaning check with 5s timeout. Returns (bool, str)."""
    import threading
    result = [False, "unknown"]
    def _check():
        try:
            result[0], result[1] = check_cleaning_needed(sg_device, st_device)
        except Exception:
            pass
    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout=5)
    return result[0], result[1]

def log_cleaning_event(db_path, sg_device, action="cleaned", notes=""):
    """Log a cleaning cartridge event to the maintenance log."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        # Create maintenance_log table if it doesn't exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                device      TEXT NOT NULL,
                action      TEXT NOT NULL,
                notes       TEXT
            )
        """)
        conn.execute(
            "INSERT INTO maintenance_log (timestamp, device, action, notes) "
            "VALUES (?,?,?,?)",
            (now, sg_device, action, notes)
        )

def get_maintenance_log(db_path, limit=50):
    """Return recent maintenance log entries."""
    try:
        with get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM maintenance_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

# ── Changer Wrappers ──────────────────────────────────────────────────────────

def library_status(changer_device):
    return _get_changer().get_library_status(changer_device)

def library_status_with_db(changer_device, db_path):
    ch = _get_changer()
    status = ch.get_library_status(changer_device)
    tapes = list_tapes(db_path)
    ch.annotate_with_db(status, tapes)
    return status

def changer_load(changer_device, tape_label, drive=0,
                 db_path=None, progress_cb=None):
    ch = _get_changer()
    slot = ch.find_and_load(changer_device, tape_label, drive, progress_cb)
    if db_path:
        _log_changer_op(db_path, "load", slot, drive, tape_label, "ok")
    return slot

def changer_unload(changer_device, drive=0, db_path=None, progress_cb=None):
    ch = _get_changer()
    slot = ch.unload_to_home(changer_device, drive, progress_cb)
    if db_path and slot is not None:
        _log_changer_op(db_path, "unload", slot, drive, "", "ok")
    return slot

def changer_inventory_scan(changer_device, db_path=None, progress_cb=None):
    ch = _get_changer()
    ch.inventory(changer_device, progress_cb)
    if db_path:
        _log_changer_op(db_path, "inventory", None, None, "", "ok")

def _log_changer_op(db_path, operation, slot, drive, tape_label, result):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO changer_log "
            "(timestamp, operation, slot, drive, tape_label, result) "
            "VALUES (?,?,?,?,?,?)",
            (now, operation, slot, drive, tape_label, result)
        )

def get_changer_log(db_path, limit=100):
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM changer_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def smart_mount(tape_label, sg_device, mount_point, cfg, db_path,
                progress_cb=None):
    """Mount a tape — auto-load from library if changer enabled."""
    if changer_enabled(cfg):
        changer_dev = get_changer_device(cfg)
        if progress_cb:
            progress_cb("Library: locating tape '{}'...".format(tape_label))
        changer_load(changer_dev, tape_label, 0, db_path, progress_cb)
        import time; time.sleep(3)
    else:
        if progress_cb:
            progress_cb("Please load tape '{}' and press Enter to continue.".format(
                tape_label))
    mount_tape(sg_device, mount_point, progress_cb)

def smart_unmount(mount_point, st_device, sg_device, cfg, db_path,
                  eject=True, progress_cb=None):
    """Unmount tape — auto-return to library if changer enabled."""
    unmount_tape(mount_point, progress_cb)
    if changer_enabled(cfg):
        changer_dev = get_changer_device(cfg)
        changer_unload(changer_dev, drive=0, db_path=db_path,
                       progress_cb=progress_cb)
    elif eject:
        eject_tape(st_device)
        if progress_cb:
            progress_cb("Tape ejected. Safe to remove.")

# ── Filesystem Helpers ────────────────────────────────────────────────────────

def dir_size_and_count(path):
    total = 0
    count = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
                count += 1
            except OSError:
                pass
    return total, count

def free_bytes(path):
    return shutil.disk_usage(path).free

def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return "{:.1f} {}".format(n, unit)
        n /= 1024
    return "{:.1f} PB".format(n)

# ── Checksums ─────────────────────────────────────────────────────────────────

def checksum_file(path, algo="sha256", progress_cb=None):
    h = hashlib.new(algo)
    done = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done)
    return h.hexdigest()

def checksum_tree(path, algo="sha256", progress_cb=None):
    h = hashlib.new(algo)
    for dirpath, _, filenames in os.walk(path):
        for fname in sorted(filenames):
            fp = os.path.join(dirpath, fname)
            rel = os.path.relpath(fp, path)
            h.update(rel.encode())
            try:
                with open(fp, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                        if progress_cb:
                            progress_cb(fp, len(chunk))
            except OSError:
                pass
    return h.hexdigest()

# ── Preflight ─────────────────────────────────────────────────────────────────

def preflight_check(source_path, staging_dir, mount_point, min_staging_free_gb,
                    direct_write=False):
    """
    Preflight checks before archiving.
    direct_write=True skips staging space checks — source is written directly to tape.
    """
    errors = []
    if not os.path.exists(source_path):
        errors.append("Source path does not exist: {}".format(source_path))
        return errors

    src_size, _ = dir_size_and_count(source_path)

    if not direct_write:
        staging_free = free_bytes(staging_dir)
        min_bytes = min_staging_free_gb * 1024 ** 3
        if staging_free < min_bytes:
            errors.append("Insufficient staging space: {} free, need {} GB minimum".format(
                human_size(staging_free), min_staging_free_gb))
        if staging_free < src_size * 1.1:
            errors.append("Staging may not fit source ({} source, {} free)".format(
                human_size(src_size), human_size(staging_free)))

    if not is_mounted(mount_point):
        errors.append("Tape is not mounted.")
    else:
        tape_free = tape_free_bytes(mount_point)
        if tape_free < src_size * 1.05:
            errors.append("Tape may not have enough space ({} source, {} free)".format(
                human_size(src_size), human_size(tape_free)))
    return errors

# ── Import Existing Tape ──────────────────────────────────────────────────────

def scan_tape_contents(mount_point):
    """
    Scan a mounted LTFS tape and return a list of top-level directories/files.
    Each entry: {name, path, size_bytes, file_count, is_dir}
    """
    contents = []
    try:
        for entry in sorted(os.listdir(mount_point)):
            full = os.path.join(mount_point, entry)
            if os.path.isdir(full):
                size, count = dir_size_and_count(full)
                contents.append({
                    "name":       entry,
                    "path":       full,
                    "size_bytes": size,
                    "file_count": count,
                    "is_dir":     True,
                })
            elif os.path.isfile(full):
                size = os.path.getsize(full)
                contents.append({
                    "name":       entry,
                    "path":       full,
                    "size_bytes": size,
                    "file_count": 1,
                    "is_dir":     False,
                })
    except Exception as e:
        logging.getLogger("tapeman2").warning("scan_tape_contents: %s", e)
    return contents

def import_tape_entry(
    tape_path,
    tape_label,
    db_path,
    mount_point,
    name,
    lab="",
    pi="",
    notes="",
    checksum_algo="sha256",
    compute_checksum=True,
    progress_cb=None,
):
    """
    Register an existing dataset on a tape into the database.
    Optionally computes a checksum by reading the tape (serves as a verify pass).

    tape_path   — full path on the mounted tape e.g. /mnt/tape/MYDATA
    tape_label  — label of the tape cartridge
    """
    def progress(msg):
        if progress_cb:
            progress_cb(msg)
        logging.getLogger("tapeman2").info(msg)

    if not os.path.exists(tape_path):
        raise ArchiveError("Path not found on tape: {}".format(tape_path))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Size and file count
    if os.path.isdir(tape_path):
        size_bytes, file_count = dir_size_and_count(tape_path)
        is_tar = False
    else:
        size_bytes = os.path.getsize(tape_path)
        file_count = 1
        is_tar = tape_path.endswith(".tar")

    # Determine tape_path relative to mount point for storage in DB
    rel_path = "/" + os.path.relpath(tape_path, mount_point)

    # Compute checksum if requested
    checksum = ""
    if compute_checksum:
        progress("Computing checksum for {} ({})...".format(
            name, human_size(size_bytes)))
        if os.path.isdir(tape_path):
            checksum = checksum_tree(tape_path, checksum_algo, progress_cb)
        else:
            checksum = checksum_file(tape_path, checksum_algo)
        progress("Checksum: {}".format(checksum))

    # Generate archive ID
    archive_id = next_archive_id(db_path)

    record = ArchiveRecord(
        archive_id=archive_id,
        name=name,
        tape_label=tape_label,
        tape_path=rel_path,
        source_path="(imported — original source unknown)",
        size_bytes=size_bytes,
        file_count=file_count,
        checksum_src=checksum,
        checksum_tape=checksum,
        date_archived=now,
        status="verified" if compute_checksum else "archived",
        lab=lab,
        pi=pi,
        notes=notes,
        tar_bundle=is_tar,
        date_verified=now if compute_checksum else "",
    )

    _db_upsert_tape(db_path, tape_label, size_bytes)
    _db_insert_archive(db_path, record)
    progress("✔ Registered as {} in database.".format(archive_id))
    return record

def import_full_tape(
    tape_label,
    db_path,
    mount_point,
    lab="",
    pi="",
    checksum_algo="sha256",
    compute_checksum=True,
    progress_cb=None,
    entry_metadata=None,
):
    """
    Import all top-level entries from a mounted tape into the database.

    entry_metadata — optional dict mapping entry name to {name, lab, pi, notes}
                     for per-entry overrides. If None, uses top-level dir name
                     as dataset name.
    Returns list of imported ArchiveRecords.
    """
    def progress(msg):
        if progress_cb:
            progress_cb(msg)

    progress("Scanning tape {}...".format(tape_label))
    contents = scan_tape_contents(mount_point)

    if not contents:
        raise ArchiveError("No entries found on tape at {}".format(mount_point))

    progress("Found {} entries on tape.".format(len(contents)))
    records = []

    for entry in contents:
        meta = (entry_metadata or {}).get(entry["name"], {})
        name  = meta.get("name",  entry["name"])
        elab  = meta.get("lab",   lab)
        epi   = meta.get("pi",    pi)
        enotes= meta.get("notes", "Imported from existing tape {}".format(tape_label))

        progress("Importing: {} ({})...".format(
            name, human_size(entry["size_bytes"])))

        try:
            rec = import_tape_entry(
                tape_path=entry["path"],
                tape_label=tape_label,
                db_path=db_path,
                mount_point=mount_point,
                name=name,
                lab=elab,
                pi=epi,
                notes=enotes,
                checksum_algo=checksum_algo,
                compute_checksum=compute_checksum,
                progress_cb=progress_cb,
            )
            records.append(rec)
        except Exception as e:
            progress("  Error importing {}: {}".format(entry["name"], e))

    progress("✔ Import complete — {} entries registered.".format(len(records)))
    return records

# ── Archive ───────────────────────────────────────────────────────────────────

def archive_dataset(
    source_path, name, tape_label, db_path, staging_dir, mount_point,
    checksum_algo="sha256", use_tar=False, lab="", pi="", notes="",
    progress_cb=None, dry_run=False,
    cfg=None, sg_device="", st_device="",
    direct_write=False,
):
    def progress(msg):
        if progress_cb:
            progress_cb(msg)
        logging.getLogger("tapeman2").info(msg)

    archive_id = next_archive_id(db_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    progress("Starting archive {}: {}".format(archive_id, name))

    if dry_run:
        src_size, src_count = dir_size_and_count(source_path)
        progress("DRY RUN — {} ({} files) from {}".format(
            human_size(src_size), src_count, source_path))
        return ArchiveRecord(
            archive_id=archive_id, name=name, tape_label=tape_label,
            tape_path="/{}/".format(archive_id), source_path=source_path,
            size_bytes=src_size, file_count=src_count,
            date_archived=now, status="dry_run",
            lab=lab, pi=pi, notes=notes, tar_bundle=use_tar
        )

    # Auto-load if changer enabled
    auto_unload_slot = None
    if cfg and changer_enabled(cfg) and sg_device:
        changer_dev = get_changer_device(cfg)
        auto_unload_slot = changer_load(
            changer_dev, tape_label, 0, db_path, progress_cb)
        import time; time.sleep(3)
        if not is_mounted(mount_point):
            mount_tape(sg_device, mount_point, progress_cb)

    stage_path = os.path.join(staging_dir, archive_id)

    try:
        if direct_write:
            # ── Direct write — no staging, source goes straight to tape ───────
            progress("Direct write mode — writing from source to tape...")
            tape_dest = os.path.join(mount_point, archive_id)
            Path(tape_dest).mkdir(parents=True, exist_ok=True)

            if use_tar:
                tar_name = "{}.tar".format(archive_id)
                tape_file = os.path.join(tape_dest, tar_name)
                progress("Creating tar bundle directly on tape: {}".format(tape_file))
                with tarfile.open(tape_file, "w") as tf:
                    tf.add(source_path, arcname=os.path.basename(source_path))
                size_bytes = os.path.getsize(tape_file)
                file_count = 1
                progress("Computing checksum...")
                checksum_src   = checksum_file(tape_file, checksum_algo)
                checksum_tape  = checksum_src
            else:
                size_bytes, file_count = dir_size_and_count(source_path)
                progress("Copying directly to tape: {}".format(tape_dest))
                result = subprocess.run(
                    ["rsync", "-a", "--info=progress2",
                     source_path + "/", tape_dest + "/"],
                    stdout=None, stderr=None, universal_newlines=True
                )
                if result.returncode != 0:
                    raise ArchiveError("rsync to tape failed (exit {})".format(
                        result.returncode))
                progress("Computing tape checksum...")
                checksum_src  = checksum_tree(tape_dest, checksum_algo)
                checksum_tape = checksum_src

            progress("Checksum: {}".format(checksum_src))
            progress("✔ Direct write complete.")

        else:
            # ── Staged write — copy to staging first, then to tape ────────────
            Path(stage_path).mkdir(parents=True, exist_ok=True)

            if use_tar:
                tar_name = "{}.tar".format(archive_id)
                tar_path = os.path.join(stage_path, tar_name)
                progress("Creating tar bundle: {}".format(tar_path))
                with tarfile.open(tar_path, "w") as tf:
                    tf.add(source_path, arcname=os.path.basename(source_path))
                staged_path = tar_path
                size_bytes = os.path.getsize(tar_path)
                file_count = 1
            else:
                dest = os.path.join(stage_path, os.path.basename(source_path))
                progress("Staging data to {}...".format(stage_path))
                result = subprocess.run(
                    ["rsync", "-a", "--info=progress2",
                     source_path + "/", dest + "/"],
                    stdout=None, stderr=None, universal_newlines=True
                )
                if result.returncode != 0:
                    raise ArchiveError("rsync staging failed (exit {})".format(
                        result.returncode))
                staged_path = dest
                size_bytes, file_count = dir_size_and_count(staged_path)

            progress("Computing source checksum...")
            if use_tar:
                checksum_src = checksum_file(staged_path, checksum_algo)
            else:
                checksum_src = checksum_tree(staged_path, checksum_algo)
            progress("Source checksum: {}".format(checksum_src))

            tape_dest = os.path.join(mount_point, archive_id)
            Path(tape_dest).mkdir(parents=True, exist_ok=True)
            progress("Copying to tape: {}".format(tape_dest))

            if use_tar:
                shutil.copy2(staged_path, os.path.join(tape_dest, tar_name))
                tape_file = os.path.join(tape_dest, tar_name)
            else:
                result = subprocess.run(
                    ["rsync", "-a", "--info=progress2",
                     staged_path + "/", tape_dest + "/"],
                    stdout=None, stderr=None, universal_newlines=True
                )
                if result.returncode != 0:
                    raise ArchiveError("rsync to tape failed (exit {})".format(
                        result.returncode))

            progress("Verifying tape copy...")
            if use_tar:
                checksum_tape = checksum_file(tape_file, checksum_algo)
            else:
                checksum_tape = checksum_tree(tape_dest, checksum_algo)
            progress("Tape checksum:   {}".format(checksum_tape))

            if checksum_src != checksum_tape:
                raise ArchiveError(
                    "CHECKSUM MISMATCH!\n"
                    "Source: {}\nTape:   {}\n"
                    "Archive aborted — data on tape may be corrupt.".format(
                        checksum_src, checksum_tape))

            progress("✔ Checksums match — archive verified.")

        record = ArchiveRecord(
            archive_id=archive_id, name=name, tape_label=tape_label,
            tape_path="/{}/".format(archive_id), source_path=source_path,
            size_bytes=size_bytes, file_count=file_count,
            checksum_src=checksum_src, checksum_tape=checksum_tape,
            date_archived=now, status="archived",
            lab=lab, pi=pi, notes=notes, tar_bundle=use_tar,
        )
        _db_upsert_tape(db_path, tape_label, size_bytes)
        _db_insert_archive(db_path, record)
        progress("✔ Archive {} logged to database.".format(archive_id))

        # Record file manifest for filename search / single-file restore
        try:
            manifest_base = os.path.join(mount_point, archive_id)
            n = record_manifest(db_path, archive_id, tape_label, manifest_base)
            if n:
                progress("Recorded {} files in manifest.".format(n))
        except Exception as e:
            progress("Manifest recording skipped: {}".format(e))

        # Snapshot tape health (drive error counters)
        try:
            record_tape_health(db_path, tape_label, "archive", sg_device)
        except Exception:
            pass

        return record

    finally:
        if not direct_write and os.path.exists(stage_path):
            progress("Cleaning up staging area...")
            shutil.rmtree(stage_path, ignore_errors=True)

        if cfg and changer_enabled(cfg) and auto_unload_slot is not None:
            try:
                unmount_tape(mount_point, progress_cb)
                changer_dev = get_changer_device(cfg)
                changer_unload(changer_dev, drive=0,
                               db_path=db_path, progress_cb=progress_cb)
            except Exception as e:
                progress("Warning: auto-unload failed: {}".format(e))

# ── Restore ───────────────────────────────────────────────────────────────────

def restore_dataset(
    archive_id, dest_path, db_path, mount_point,
    checksum_algo="sha256", progress_cb=None,
    cfg=None, sg_device="", st_device="",
):
    def progress(msg):
        if progress_cb:
            progress_cb(msg)
        logging.getLogger("tapeman2").info(msg)

    record = get_archive(db_path, archive_id)
    if not record:
        raise ArchiveError("Archive ID {} not found.".format(archive_id))

    if cfg and changer_enabled(cfg) and sg_device:
        changer_dev = get_changer_device(cfg)
        progress("Library: loading tape '{}'...".format(record.tape_label))
        changer_load(changer_dev, record.tape_label, 0, db_path, progress_cb)
        import time; time.sleep(3)
        if not is_mounted(mount_point):
            mount_tape(sg_device, mount_point, progress_cb)
    else:
        if not is_mounted(mount_point):
            raise ArchiveError(
                "No tape mounted. Please load tape '{}' and mount it.".format(
                    record.tape_label))

    tape_src = os.path.join(mount_point, archive_id.lstrip("/"))
    if not os.path.exists(tape_src):
        raise ArchiveError("Archive path not found on tape: {}\n"
                           "Is tape '{}' loaded?".format(tape_src, record.tape_label))

    Path(dest_path).mkdir(parents=True, exist_ok=True)
    progress("Restoring {} to {}...".format(archive_id, dest_path))

    if record.tar_bundle:
        tar_files = list(Path(tape_src).glob("*.tar"))
        if not tar_files:
            raise ArchiveError("No tar file found in {}".format(tape_src))
        progress("Extracting tar bundle...")
        with tarfile.open(str(tar_files[0]), "r") as tf:
            tf.extractall(dest_path)
        restored_checksum = checksum_file(str(tar_files[0]), checksum_algo)
    else:
        result = subprocess.run(
            ["rsync", "-a", "--info=progress2",
             tape_src + "/", dest_path + "/"],
            stdout=None, stderr=None, universal_newlines=True
        )
        if result.returncode != 0:
            raise ArchiveError("rsync restore failed (exit {})".format(
                result.returncode))
        progress("Computing restore checksum...")
        restored_checksum = checksum_tree(dest_path, checksum_algo)

    progress("Restore checksum:  {}".format(restored_checksum))
    progress("Original checksum: {}".format(record.checksum_src))

    checksum_ok = restored_checksum == record.checksum_src
    if checksum_ok:
        progress("✔ Checksum verified — restore successful.")
    else:
        progress("⚠ WARNING: Checksum mismatch after restore!")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE archives SET date_restored=?, status='restored' "
            "WHERE archive_id=?", (now, archive_id))
        conn.execute(
            "INSERT INTO restore_log "
            "(archive_id, restore_dest, date_restored, checksum_ok) "
            "VALUES (?,?,?,?)",
            (archive_id, dest_path, now, 1 if checksum_ok else 0))

    if cfg and changer_enabled(cfg):
        try:
            unmount_tape(mount_point, progress_cb)
            changer_dev = get_changer_device(cfg)
            changer_unload(changer_dev, drive=0,
                           db_path=db_path, progress_cb=progress_cb)
        except Exception as e:
            progress("Warning: auto-unload failed: {}".format(e))

    return checksum_ok

# ── Verify ────────────────────────────────────────────────────────────────────

def verify_dataset(archive_id, db_path, mount_point,
                   checksum_algo="sha256", progress_cb=None):
    def progress(msg):
        if progress_cb:
            progress_cb(msg)

    record = get_archive(db_path, archive_id)
    if not record:
        raise ArchiveError("Archive {} not found.".format(archive_id))
    if not is_mounted(mount_point):
        raise ArchiveError("No tape mounted.")

    tape_path = os.path.join(mount_point, archive_id.lstrip("/"))
    if not os.path.exists(tape_path):
        raise ArchiveError("Archive path not found on tape: {}".format(tape_path))

    progress("Verifying {}...".format(archive_id))
    if record.tar_bundle:
        tar_files = list(Path(tape_path).glob("*.tar"))
        if not tar_files:
            raise ArchiveError("No tar file found on tape.")
        current = checksum_file(str(tar_files[0]), checksum_algo)
    else:
        current = checksum_tree(tape_path, checksum_algo)

    ok = current == record.checksum_tape
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE archives SET date_verified=?, status=? WHERE archive_id=?",
            (now, "verified" if ok else "corrupt", archive_id))
        conn.execute(
            "UPDATE tapes SET last_verified=? WHERE label=?",
            (now, record.tape_label))

    if ok:
        progress("✔ {} verified successfully.".format(archive_id))
    else:
        progress("✘ {} FAILED! Expected: {} Got: {}".format(
            archive_id, record.checksum_tape, current))
    return ok

def verify_tape_datasets(tape_label, db_path, mount_point,
                         checksum_algo="sha256", progress_cb=None):
    archives = list_archives(db_path, tape_label=tape_label)
    results = {}
    for rec in archives:
        try:
            ok = verify_dataset(rec.archive_id, db_path, mount_point,
                                checksum_algo, progress_cb)
            results[rec.archive_id] = ok
        except ArchiveError as e:
            if progress_cb:
                progress_cb("Error verifying {}: {}".format(rec.archive_id, e))
            results[rec.archive_id] = False
    return results

# ── Database Queries ──────────────────────────────────────────────────────────

def _db_insert_archive(db_path, rec):
    with get_db(db_path) as conn:
        conn.execute("""
            INSERT INTO archives
            (archive_id, name, lab, pi, notes, source_path, tape_label, tape_path,
             tar_bundle, size_bytes, file_count, checksum_src, checksum_tape,
             date_archived, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            rec.archive_id, rec.name, rec.lab, rec.pi, rec.notes,
            rec.source_path, rec.tape_label, rec.tape_path,
            1 if rec.tar_bundle else 0,
            rec.size_bytes, rec.file_count,
            rec.checksum_src, rec.checksum_tape,
            rec.date_archived, rec.status
        ))

def _db_upsert_tape(db_path, label, added_bytes):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM tapes WHERE label=?", (label,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE tapes SET total_archives=total_archives+1, "
                "total_bytes=total_bytes+? WHERE label=?",
                (added_bytes, label))
        else:
            conn.execute(
                "INSERT INTO tapes (label, date_first_used, total_archives, total_bytes) "
                "VALUES (?,?,1,?)", (label, now, added_bytes))

def get_archive(db_path, archive_id):
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM archives WHERE archive_id=?", (archive_id,)).fetchone()
    return _row_to_archive(row) if row else None

def list_archives(db_path, tape_label=None, lab=None, search=None, status=None):
    query = "SELECT * FROM archives WHERE 1=1"
    params = []
    if tape_label:
        query += " AND tape_label=?"; params.append(tape_label)
    if lab:
        query += " AND lab=?"; params.append(lab)
    if status:
        query += " AND status=?"; params.append(status)
    if search:
        query += (" AND (name LIKE ? OR pi LIKE ? OR lab LIKE ? "
                  "OR notes LIKE ? OR archive_id LIKE ?)")
        s = "%{}%".format(search)
        params.extend([s, s, s, s, s])
    query += " ORDER BY date_archived DESC"
    with get_db(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_archive(r) for r in rows]

def list_tapes(db_path):
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tapes ORDER BY date_first_used DESC").fetchall()
    return [_row_to_tape(r) for r in rows]

# ── Capacity Check ────────────────────────────────────────────────────────────

def check_capacity(source_path, mount_point, warn_threshold=0.90):
    """
    Check whether source will fit on the mounted tape.
    Returns dict: {fits, source_bytes, tape_free, tape_total,
                   pct_after, warning}
    """
    src_size, _ = dir_size_and_count(source_path)
    if not is_mounted(mount_point):
        return {"fits": False, "source_bytes": src_size, "tape_free": 0,
                "tape_total": 0, "pct_after": 0,
                "warning": "Tape not mounted."}

    free  = tape_free_bytes(mount_point)
    total = tape_total_bytes(mount_point)
    fits  = src_size < free
    used_after = (total - free) + src_size
    pct_after  = (used_after / total) if total else 0

    warning = None
    if not fits:
        warning = ("Source ({}) will NOT fit — only {} free.".format(
            human_size(src_size), human_size(free)))
    elif pct_after >= warn_threshold:
        warning = ("Tape will be {:.0f}% full after this archive.".format(
            pct_after * 100))

    return {"fits": fits, "source_bytes": src_size, "tape_free": free,
            "tape_total": total, "pct_after": pct_after, "warning": warning}

# ── File Manifest (filename search / restore) ─────────────────────────────────

def record_manifest(db_path, archive_id, tape_label, base_path):
    """
    Walk an archived directory and record every file in the manifest table.
    Enables search-by-filename and single-file restore.
    """
    entries = []
    if os.path.isdir(base_path):
        for root, _dirs, files in os.walk(base_path):
            for fn in files:
                full = os.path.join(root, fn)
                rel  = os.path.relpath(full, base_path)
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    sz = 0
                entries.append((archive_id, tape_label, rel, sz))
    else:
        entries.append((archive_id, tape_label,
                        os.path.basename(base_path),
                        os.path.getsize(base_path) if os.path.exists(base_path) else 0))

    if entries:
        with get_db(db_path) as conn:
            conn.executemany(
                "INSERT INTO file_manifest "
                "(archive_id, tape_label, file_path, size_bytes) "
                "VALUES (?,?,?,?)", entries)
    return len(entries)

def search_files(db_path, pattern):
    """
    Search the file manifest by filename pattern.
    Returns list of dicts: {archive_id, tape_label, file_path, size_bytes}
    """
    like = "%{}%".format(pattern)
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT archive_id, tape_label, file_path, size_bytes "
            "FROM file_manifest WHERE file_path LIKE ? "
            "ORDER BY archive_id, file_path LIMIT 500", (like,)).fetchall()
    return [dict(r) for r in rows]

def get_archive_files(db_path, archive_id):
    """Return all files recorded for an archive."""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT file_path, size_bytes FROM file_manifest "
            "WHERE archive_id=? ORDER BY file_path", (archive_id,)).fetchall()
    return [dict(r) for r in rows]

# ── Email Notifications ───────────────────────────────────────────────────────

def send_email(to_addr, subject, body, cfg=None):
    """
    Send an email notification via the local 'mail' command (uses system MTA).
    Returns (success, message). Silently no-ops if to_addr is empty.
    """
    if not to_addr:
        return False, "No recipient configured."
    try:
        proc = subprocess.run(
            ["mail", "-s", subject, to_addr],
            input=body, universal_newlines=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if proc.returncode == 0:
            return True, "Email sent to {}".format(to_addr)
        return False, "mail command failed: {}".format(proc.stderr)
    except FileNotFoundError:
        return False, "'mail' command not found — install mailx."
    except Exception as e:
        return False, "Email error: {}".format(e)

def notify_job_complete(cfg, job):
    """Send completion email if notifications are configured."""
    if not cfg:
        return
    try:
        to_addr = cfg.get("notify", "email", fallback="").strip()
    except Exception:
        to_addr = ""
    if not to_addr:
        return

    status  = job.get("status", "unknown")
    jtype   = job.get("type", "job")
    jobid   = job.get("job_id", "")
    name    = job.get("name") or job.get("archive_id") or ""

    subject = "[tapeman2] {} {} — {}".format(jtype, jobid, status)
    body = (
        "tapeman2 job notification\n"
        "-------------------------\n"
        "Job ID:    {}\n"
        "Type:      {}\n"
        "Name:      {}\n"
        "Status:    {}\n"
        "Started:   {}\n"
        "Finished:  {}\n".format(
            jobid, jtype, name, status,
            job.get("started", ""), job.get("finished", "")))
    if job.get("archive_id"):
        body += "Archive:   {}\n".format(job["archive_id"])
    if job.get("error"):
        body += "\nError:\n{}\n".format(job["error"])
    body += "\n-- \ntapeman2 on {}\n".format(socket.gethostname())

    send_email(to_addr, subject, body, cfg)

# ── Tape Health ───────────────────────────────────────────────────────────────

def record_tape_health(db_path, tape_label, operation="", sg_device=""):
    """
    Read drive error counters and record a health snapshot for this tape.
    """
    write_err, read_err = 0, 0
    plat = _get_platform()
    if sg_device and hasattr(plat, "read_error_counters"):
        try:
            write_err, read_err = plat.read_error_counters(sg_device)
        except Exception:
            pass

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO tape_health "
            "(tape_label, timestamp, operation, write_errors, read_errors) "
            "VALUES (?,?,?,?,?)",
            (tape_label, now, operation, write_err, read_err))
    return write_err, read_err

def get_tape_health(db_path, tape_label=None):
    """
    Return health history. If tape_label given, just that tape.
    Otherwise a summary per tape (latest snapshot + totals).
    """
    with get_db(db_path) as conn:
        if tape_label:
            rows = conn.execute(
                "SELECT * FROM tape_health WHERE tape_label=? "
                "ORDER BY timestamp DESC", (tape_label,)).fetchall()
            return [dict(r) for r in rows]
        else:
            # Summary: per tape, sum errors and latest timestamp
            rows = conn.execute(
                "SELECT tape_label, "
                "  COUNT(*) AS snapshots, "
                "  SUM(write_errors) AS total_write_errors, "
                "  SUM(read_errors) AS total_read_errors, "
                "  MAX(timestamp) AS last_check "
                "FROM tape_health GROUP BY tape_label "
                "ORDER BY total_write_errors DESC").fetchall()
            return [dict(r) for r in rows]

def get_tape(db_path, label):
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tapes WHERE label=?", (label,)).fetchone()
    return _row_to_tape(row) if row else None

def which_tape(db_path, archive_id):
    rec = get_archive(db_path, archive_id)
    return rec.tape_label if rec else None

def add_tape(db_path, label, notes="", barcode="", slot_number=-1):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tapes "
            "(label, date_first_used, notes, barcode, slot_number) "
            "VALUES (?,?,?,?,?)",
            (label, now, notes, barcode, slot_number))

def update_tape(db_path, label, notes=None, barcode=None, slot_number=None):
    sets, vals = [], []
    if notes is not None:
        sets.append("notes=?"); vals.append(notes)
    if barcode is not None:
        sets.append("barcode=?"); vals.append(barcode)
    if slot_number is not None:
        sets.append("slot_number=?"); vals.append(slot_number)
    if not sets:
        return
    vals.append(label)
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE tapes SET {} WHERE label=?".format(", ".join(sets)), vals)

def _safe_col(row, col, default=""):
    try:
        return row[col] if row[col] is not None else default
    except (IndexError, KeyError):
        return default

def _row_to_archive(row):
    return ArchiveRecord(
        archive_id=row["archive_id"],
        name=row["name"],
        tape_label=row["tape_label"],
        tape_path=row["tape_path"],
        source_path=row["source_path"],
        size_bytes=row["size_bytes"] or 0,
        file_count=row["file_count"] or 0,
        checksum_src=row["checksum_src"] or "",
        checksum_tape=row["checksum_tape"] or "",
        date_archived=row["date_archived"] or "",
        status=row["status"] or "archived",
        lab=row["lab"] or "",
        pi=row["pi"] or "",
        notes=row["notes"] or "",
        tar_bundle=bool(row["tar_bundle"]),
        date_verified=row["date_verified"] or "",
        date_restored=row["date_restored"] or "",
    )

def _row_to_tape(row):
    return TapeRecord(
        label=row["label"],
        barcode=_safe_col(row, "barcode"),
        slot_number=_safe_col(row, "slot_number", -1),
        date_first_used=row["date_first_used"] or "",
        notes=row["notes"] or "",
        last_verified=row["last_verified"] or "",
        total_archives=row["total_archives"] or 0,
        total_bytes=row["total_bytes"] or 0,
    )

# ── Reporting ─────────────────────────────────────────────────────────────────

def export_csv(db_path, output_path, tape_label=None):
    import csv
    archives = list_archives(db_path, tape_label=tape_label)
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Archive ID","Name","Lab","PI","Tape","Size",
                          "Files","Date Archived","Status","Notes","Source Path"])
        for a in archives:
            writer.writerow([
                a.archive_id, a.name, a.lab, a.pi, a.tape_label,
                human_size(a.size_bytes), a.file_count,
                a.date_archived, a.status, a.notes, a.source_path
            ])

def tape_report(db_path, tape_label):
    tape = get_tape(db_path, tape_label)
    archives = list_archives(db_path, tape_label=tape_label)
    return {
        "tape": tape, "archives": archives,
        "total_size": sum(a.size_bytes for a in archives),
        "total_files": sum(a.file_count for a in archives),
        "verified_count": sum(1 for a in archives if a.status == "verified"),
        "unverified_count": sum(1 for a in archives if not a.date_verified),
    }

# ── Init ──────────────────────────────────────────────────────────────────────

def initialize(config_path=None):
    if config_path is None:
        config_path = CONFIG_PATH
    cfg = load_config(config_path)
    plat = _get_platform()

    state_dir = cfg.get("paths", "state_dir",   fallback=plat.DEFAULT_STATE_DIR)
    log_file  = cfg.get("paths", "log_file",    fallback=plat.DEFAULT_LOG_FILE)
    db_path   = cfg.get("paths", "db_path",     fallback=plat.DEFAULT_DB_PATH)
    staging   = cfg.get("paths", "staging_dir", fallback=plat.DEFAULT_STAGING_DIR)
    restore   = cfg.get("paths", "restore_dir", fallback=plat.DEFAULT_RESTORE_DIR)

    for d in [state_dir, staging, restore]:
        Path(d).mkdir(parents=True, exist_ok=True)

    # Ensure jobs directory exists
    jobs_dir = os.path.join(state_dir, "jobs")
    Path(jobs_dir).mkdir(parents=True, exist_ok=True)

    setup_logging(log_file)
    init_db(db_path)
    return cfg

# ── Job Manager ───────────────────────────────────────────────────────────────

import json
import signal

JOB_STATUS_QUEUED    = "queued"
JOB_STATUS_RUNNING   = "running"
JOB_STATUS_COMPLETE  = "complete"
JOB_STATUS_FAILED    = "failed"
JOB_STATUS_CANCELLED = "cancelled"

def _jobs_dir(state_dir):
    return os.path.join(state_dir, "jobs")

def _next_job_id(state_dir):
    jobs = list_jobs(state_dir)
    if not jobs:
        return "JOB-0001"
    nums = []
    for j in jobs:
        try:
            nums.append(int(j["job_id"].split("-")[1]))
        except Exception:
            pass
    return "JOB-{:04d}".format(max(nums) + 1 if nums else 1)

def _job_file(state_dir, job_id):
    return os.path.join(_jobs_dir(state_dir), "{}.json".format(job_id))

def _write_job(state_dir, job):
    path = _job_file(state_dir, job["job_id"])
    tmp  = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f, indent=2)
    os.replace(tmp, path)

def _read_job(job_file):
    try:
        with open(job_file) as f:
            return json.load(f)
    except Exception:
        return None

def list_jobs(state_dir, status=None):
    """Return list of all job dicts, optionally filtered by status."""
    jdir = _jobs_dir(state_dir)
    jobs = []
    try:
        for fname in sorted(os.listdir(jdir)):
            if fname.endswith(".json"):
                job = _read_job(os.path.join(jdir, fname))
                if job:
                    # Check if running process is still alive
                    if job.get("status") == JOB_STATUS_RUNNING:
                        pid = job.get("pid")
                        if pid:
                            try:
                                os.kill(pid, 0)
                            except OSError:
                                # Process is gone — mark as failed
                                job["status"] = JOB_STATUS_FAILED
                                job["error"]  = "Process terminated unexpectedly"
                                _write_job(state_dir, job)
                    if status is None or job.get("status") == status:
                        jobs.append(job)
    except Exception:
        pass
    return sorted(jobs, key=lambda j: j.get("started", ""), reverse=True)

def get_job(state_dir, job_id):
    return _read_job(_job_file(state_dir, job_id))

def cancel_job(state_dir, job_id):
    """Send SIGTERM to a running job."""
    job = get_job(state_dir, job_id)
    if not job:
        return False
    if job.get("status") != JOB_STATUS_RUNNING:
        return False
    pid = job.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        job["status"] = JOB_STATUS_CANCELLED
        job["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_job(state_dir, job)
        return True
    except OSError:
        return False

def cleanup_jobs(state_dir, keep_days=7):
    """Remove completed/failed job files older than keep_days."""
    import time
    jdir = _jobs_dir(state_dir)
    cutoff = time.time() - (keep_days * 86400)
    removed = 0
    try:
        for fname in os.listdir(jdir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(jdir, fname)
            job = _read_job(fpath)
            if not job:
                continue
            if job.get("status") in (JOB_STATUS_COMPLETE,
                                      JOB_STATUS_FAILED,
                                      JOB_STATUS_CANCELLED):
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    removed += 1
    except Exception:
        pass
    return removed

def active_job_count(state_dir):
    return len(list_jobs(state_dir, status=JOB_STATUS_RUNNING))

# ── Job Queue (sequential execution) ──────────────────────────────────────────

def _queue_file(state_dir):
    return os.path.join(_jobs_dir(state_dir), "_queue.json")

def enqueue_job(state_dir, job_spec):
    """
    Add a job spec to the sequential queue. job_spec is a dict describing
    the job (type + parameters). Returns the queue position.
    """
    qfile = _queue_file(state_dir)
    queue = []
    if os.path.exists(qfile):
        try:
            with open(qfile) as f:
                queue = json.load(f)
        except Exception:
            queue = []
    job_spec["queued_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    queue.append(job_spec)
    tmp = qfile + ".tmp"
    with open(tmp, "w") as f:
        json.dump(queue, f, indent=2)
    os.replace(tmp, qfile)
    return len(queue)

def get_queue(state_dir):
    qfile = _queue_file(state_dir)
    if not os.path.exists(qfile):
        return []
    try:
        with open(qfile) as f:
            return json.load(f)
    except Exception:
        return []

def clear_queue(state_dir):
    qfile = _queue_file(state_dir)
    if os.path.exists(qfile):
        os.remove(qfile)

def process_queue(state_dir, db_path, cfg, sg_device="", st_device="",
                  staging_dir="", mount_point="", checksum_algo="sha256"):
    """
    Process queued jobs sequentially. Designed to run as a single background
    daemon: it pops jobs one at a time and runs them to completion in order.
    Returns immediately after forking the queue processor.
    """
    queue = get_queue(state_dir)
    if not queue:
        return None

    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
        except Exception:
            pass
        devnull = open(os.devnull, "r+")
        os.dup2(devnull.fileno(), 0)
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)

        # Process each queued job in order, synchronously
        while True:
            queue = get_queue(state_dir)
            if not queue:
                break
            spec = queue[0]

            job_id = _next_job_id(state_dir)
            now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job = {
                "job_id": job_id, "type": spec.get("type", "archive"),
                "status": JOB_STATUS_RUNNING, "name": spec.get("name", ""),
                "tape_label": spec.get("tape_label", ""),
                "source_path": spec.get("source_path", ""),
                "archive_id": spec.get("archive_id"),
                "pid": os.getpid(), "progress": "Starting (from queue)...",
                "percent": 0, "started": now, "finished": None, "error": None,
                "from_queue": True,
            }
            _write_job(state_dir, job)

            def progress_cb(msg, _jid=job_id):
                jj = _read_job(_job_file(state_dir, _jid)) or {}
                jj["progress"] = msg
                jj["status"]   = JOB_STATUS_RUNNING
                _write_job(state_dir, jj)

            try:
                if spec["type"] == "archive":
                    rec = archive_dataset(
                        source_path=spec["source_path"], name=spec["name"],
                        tape_label=spec["tape_label"], db_path=db_path,
                        staging_dir=staging_dir, mount_point=mount_point,
                        checksum_algo=checksum_algo,
                        use_tar=spec.get("use_tar", False),
                        lab=spec.get("lab", ""), pi=spec.get("pi", ""),
                        notes=spec.get("notes", ""),
                        progress_cb=progress_cb, dry_run=False, cfg=cfg,
                        sg_device=sg_device, st_device=st_device,
                        direct_write=spec.get("direct_write", False))
                    jj = _read_job(_job_file(state_dir, job_id))
                    jj["status"] = JOB_STATUS_COMPLETE
                    jj["archive_id"] = rec.archive_id
                    jj["progress"] = "Complete — {}".format(rec.archive_id)
                    jj["percent"] = 100
                    jj["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _write_job(state_dir, jj)
                    notify_job_complete(cfg, jj)
            except Exception as e:
                jj = _read_job(_job_file(state_dir, job_id)) or job
                jj["status"] = JOB_STATUS_FAILED
                jj["error"] = str(e)
                jj["progress"] = "Failed: {}".format(str(e)[:80])
                jj["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _write_job(state_dir, jj)
                notify_job_complete(cfg, jj)

            # Remove the completed job from the queue
            queue = get_queue(state_dir)
            if queue:
                queue.pop(0)
                qfile = _queue_file(state_dir)
                tmp = qfile + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp, qfile)

        os._exit(0)
    else:
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
        return pid

def submit_archive_job(
    state_dir, db_path, source_path, name, tape_label,
    staging_dir, mount_point, checksum_algo="sha256",
    use_tar=False, lab="", pi="", notes="",
    cfg=None, sg_device="", st_device="",
    direct_write=False,
):
    """
    Submit an archive job to run in the background.
    Returns the job dict immediately.
    """
    job_id  = _next_job_id(state_dir)
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    job = {
        "job_id":       job_id,
        "type":         "archive",
        "status":       JOB_STATUS_QUEUED,
        "name":         name,
        "tape_label":   tape_label,
        "source_path":  source_path,
        "archive_id":   None,
        "pid":          None,
        "progress":     "Queued",
        "percent":      0,
        "started":      now,
        "finished":     None,
        "error":        None,
        "lab":          lab,
        "pi":           pi,
        "notes":        notes,
        "use_tar":      use_tar,
        "direct_write": direct_write,
    }
    _write_job(state_dir, job)

    # Fork child process
    pid = os.fork()
    if pid == 0:
        # ── Child process ─────────────────────────────────────────────────────
        # Detach from parent completely
        try:
            os.setsid()
        except Exception:
            pass

        # Redirect stdio to /dev/null
        devnull = open(os.devnull, "r+")
        os.dup2(devnull.fileno(), 0)
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)

        def progress_cb(msg):
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["progress"] = msg
            j["status"]   = JOB_STATUS_RUNNING
            j["pid"]      = os.getpid()
            _write_job(state_dir, j)

        # Update status to running
        job["status"] = JOB_STATUS_RUNNING
        job["pid"]    = os.getpid()
        job["progress"] = "Starting..."
        _write_job(state_dir, job)

        try:
            rec = archive_dataset(
                source_path=source_path,
                name=name,
                tape_label=tape_label,
                db_path=db_path,
                staging_dir=staging_dir,
                mount_point=mount_point,
                checksum_algo=checksum_algo,
                use_tar=use_tar,
                lab=lab, pi=pi, notes=notes,
                progress_cb=progress_cb,
                dry_run=False,
                cfg=cfg,
                sg_device=sg_device,
                st_device=st_device,
                direct_write=direct_write,
            )
            # Success
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["status"]     = JOB_STATUS_COMPLETE
            j["archive_id"] = rec.archive_id
            j["progress"]   = "Complete — {}".format(rec.archive_id)
            j["percent"]    = 100
            j["finished"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write_job(state_dir, j)
            notify_job_complete(cfg, j)
        except Exception as e:
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["status"]   = JOB_STATUS_FAILED
            j["error"]    = str(e)
            j["progress"] = "Failed: {}".format(str(e)[:80])
            j["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write_job(state_dir, j)
            notify_job_complete(cfg, j)
        finally:
            os._exit(0)
    else:
        # ── Parent process ────────────────────────────────────────────────────
        # Reap child immediately (it daemonized via setsid)
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
        job["pid"] = pid
        _write_job(state_dir, job)
        return job

def submit_restore_job(
    state_dir, db_path, archive_id, dest_path,
    mount_point, checksum_algo="sha256",
    cfg=None, sg_device="", st_device="",
):
    """Submit a restore job to run in the background."""
    job_id = _next_job_id(state_dir)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    job = {
        "job_id":      job_id,
        "type":        "restore",
        "status":      JOB_STATUS_QUEUED,
        "archive_id":  archive_id,
        "dest_path":   dest_path,
        "pid":         None,
        "progress":    "Queued",
        "percent":     0,
        "started":     now,
        "finished":    None,
        "error":       None,
    }
    _write_job(state_dir, job)

    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
        except Exception:
            pass
        devnull = open(os.devnull, "r+")
        os.dup2(devnull.fileno(), 0)
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)

        def progress_cb(msg):
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["progress"] = msg
            j["status"]   = JOB_STATUS_RUNNING
            j["pid"]      = os.getpid()
            _write_job(state_dir, j)

        job["status"] = JOB_STATUS_RUNNING
        job["pid"]    = os.getpid()
        job["progress"] = "Starting restore..."
        _write_job(state_dir, job)

        try:
            ok = restore_dataset(
                archive_id=archive_id,
                dest_path=dest_path,
                db_path=db_path,
                mount_point=mount_point,
                checksum_algo=checksum_algo,
                progress_cb=progress_cb,
                cfg=cfg,
                sg_device=sg_device,
                st_device=st_device,
            )
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["status"]   = JOB_STATUS_COMPLETE if ok else JOB_STATUS_FAILED
            j["progress"] = "Complete — checksum {}".format(
                "verified" if ok else "MISMATCH")
            j["percent"]  = 100
            j["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not ok:
                j["error"] = "Checksum mismatch after restore"
            _write_job(state_dir, j)
            notify_job_complete(cfg, j)
        except Exception as e:
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["status"]   = JOB_STATUS_FAILED
            j["error"]    = str(e)
            j["progress"] = "Failed: {}".format(str(e)[:80])
            j["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write_job(state_dir, j)
            notify_job_complete(cfg, j)
        finally:
            os._exit(0)
    else:
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
        job["pid"] = pid
        _write_job(state_dir, job)
        return job

def submit_verify_job(
    state_dir, db_path, archive_id,
    mount_point, checksum_algo="sha256",
):
    """Submit a verify job to run in the background."""
    job_id = _next_job_id(state_dir)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    job = {
        "job_id":     job_id,
        "type":       "verify",
        "status":     JOB_STATUS_QUEUED,
        "archive_id": archive_id,
        "pid":        None,
        "progress":   "Queued",
        "percent":    0,
        "started":    now,
        "finished":   None,
        "error":      None,
    }
    _write_job(state_dir, job)

    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
        except Exception:
            pass
        devnull = open(os.devnull, "r+")
        os.dup2(devnull.fileno(), 0)
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)

        def progress_cb(msg):
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["progress"] = msg
            j["status"]   = JOB_STATUS_RUNNING
            j["pid"]      = os.getpid()
            _write_job(state_dir, j)

        job["status"] = JOB_STATUS_RUNNING
        job["pid"]    = os.getpid()
        _write_job(state_dir, job)

        try:
            ok = verify_dataset(
                archive_id=archive_id,
                db_path=db_path,
                mount_point=mount_point,
                checksum_algo=checksum_algo,
                progress_cb=progress_cb,
            )
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["status"]   = JOB_STATUS_COMPLETE if ok else JOB_STATUS_FAILED
            j["progress"] = "Verified OK" if ok else "VERIFICATION FAILED"
            j["percent"]  = 100
            j["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not ok:
                j["error"] = "Checksum mismatch"
            _write_job(state_dir, j)
        except Exception as e:
            j = _read_job(_job_file(state_dir, job_id)) or job.copy()
            j["status"]   = JOB_STATUS_FAILED
            j["error"]    = str(e)
            j["progress"] = "Failed: {}".format(str(e)[:80])
            j["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write_job(state_dir, j)
        finally:
            os._exit(0)
    else:
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
        job["pid"] = pid
        _write_job(state_dir, job)
        return job
