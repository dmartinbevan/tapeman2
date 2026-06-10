# Tape Archival — User Guide
**gcarchive server  •  Quantum LTO-9  •  LTFS**

---

## Overview

The tape system uses **LTFS** (Linear Tape File System), which makes a tape behave like a USB drive — you can browse it, drag files to it, and copy data on and off using normal Linux commands.

The tape management tool on this server is **`tapeman`** — a custom interactive script written specifically for gcarchive by your system administrator. It is not a standard system utility and will not be available on other Linux systems. It wraps the standard LTFS tools (`mkltfs`, `ltfs`, `ltfsck`) in a user-friendly menu so you don't need to remember any commands.

---

## Quick Reference

| Task | Command |
|------|---------|
| Open tape manager | `tapeman` |
| Stage data to local disk | `rsync -avh --progress source /data/staging/` |
| Copy files TO tape | `rsync -avh --progress /data/staging/yourdata /mnt/tape/` |
| Copy files FROM tape | `cp -r /mnt/tape/folder /data/restore/` |
| Check what's on tape | `ls -lh /mnt/tape/` |
| Check space remaining | `df -h /mnt/tape` |
| Check staging space | `df -h /data` |

---

## Step 1 — Log Into the Server

```bash
ssh root@gcarchive
```

---

## Step 2 — Load a Tape

Open the tape drive door and insert a cartridge. The tape label faces up, arrow pointing in.

**⚠️ Important: New tapes take time to initialize.**

When you insert a **brand new, never-used tape**, the drive will run an initialization/calibration pass before it accepts any commands. This is normal and required.

- The drive's front panel will show a **"C"** (Calibrating) with a blinking green LED
- This can take **15–30 minutes** for a new LTO-9 cartridge
- **Do not try to format or mount the tape until the display clears**
- When the tape is ready the blinking will settle and the letter will disappear

Previously used tapes load in about **30–60 seconds**.

---

## Step 3 — Open the Tape Manager

```bash
tapeman
```

You will see a menu like this:

```
╔══════════════════════════════════════════╗
║         LTFS Tape Manager                ║
║         Quantum LTO-9  •  Rocky Linux    ║
╚══════════════════════════════════════════╝

  Status: ○ No tape mounted

  What would you like to do?

  1)  Format a NEW tape (erases everything)
  2)  Mount an EXISTING tape (already formatted)
  3)  Unmount and eject tape
  4)  Show tape / drive status
  5)  Check tape filesystem (ltfsck)
  6)  View activity log
  q)  Quit
```

---

## Step 4 — Format a New Tape (First Time Only)

If the tape is **brand new and has never been formatted**, choose **Option 1**.

- You will be asked for a **label** — use something descriptive like `CRYO2024A` or `ARCH001`
- You will be asked to type `YES` to confirm — this erases everything on the tape
- Formatting takes **a few minutes**
- After formatting you will be offered the option to mount immediately — say yes

**If the tape has been used before, skip to Step 5.**

---

## Step 5 — Mount an Existing Tape

If the tape is already formatted, choose **Option 2**.

The tape will be mounted at `/mnt/tape` and you will see:

```
✔ Tape mounted successfully at /mnt/tape
```

The tape is now accessible like any other directory.

---

## Step 6 — Stage Your Data First (Important!)

**Do not copy directly from the network to the tape.** Tape drives need a continuous, fast stream of data to operate efficiently. If the data source is too slow (e.g. copying over the network from another server), the drive will repeatedly stop and rewind — called **"shoe-shining"** — which is slow, wastes tape life, and can cause errors.

### Always stage to `/data` first

`/data` is a local **8 TB disk volume** on gcarchive. Copy your data there first, then copy from `/data` to the tape. The local disk is fast enough to keep the tape drive fed continuously.

**Workflow:**

```
Your data source  →  /data  →  /mnt/tape
  (network/NFS)     (local)    (tape)
```

**Step 1 — Copy your data to the local staging area:**
```bash
rsync -avh --progress user@sourceserver:/path/to/data /data/staging/
```

**Step 2 — Verify it landed correctly:**
```bash
ls -lh /data/staging/
df -h /data
```

**Step 3 — Then copy from staging to tape (see below)**

> `/data` is shared staging space. Clean up your staged files after confirming the tape copy succeeded — don't leave large datasets sitting there indefinitely.

---

## Step 7 — Copy Files To or From the Tape

Once the tape is mounted and your data is staged in `/data`, use standard Linux copy commands.

**Copy data TO the tape (from local staging):**
```bash
rsync -avh --progress /data/staging/yourdata /mnt/tape/
```

**Copy data FROM the tape:**
```bash
cp -r /mnt/tape/foldername /data/restore/
```

Then move it onward to its final destination from `/data`.

**See what's on the tape:**
```bash
ls -lh /mnt/tape/
```

**Check remaining space:**
```bash
df -h /mnt/tape
```

### ⚠️ Important Notes About Tape I/O

- Tape is **sequential storage** — large sequential writes are fast, lots of small random files are slow
- Write speed is roughly **300–400 MB/s** for large files sustained from local disk
- Copying directly from a slow network source will degrade performance significantly
- Avoid modifying files on the tape after writing — write once, read many is the ideal workflow
- The tape index is written when you **unmount** — always unmount cleanly

---

## Step 8 — Unmount and Eject

**Always unmount before physically removing the tape.** Removing a mounted tape can corrupt the filesystem.

Choose **Option 3** from the tape manager.

- tapeman will flush the LTFS index to tape (this takes a moment)
- It will then offer to eject — say yes
- Wait for the tape to physically eject before removing it

Or from the command line:
```bash
fusermount -u /mnt/tape
mt -f /dev/nst0 offline
```

---

## Troubleshooting

**Tape won't mount — "device not ready"**
The tape may still be loading. Wait 60 seconds and try again via Option 2.

**New tape won't format**
Wait for the drive's calibration to finish (blinking green LED + "C" on display). This can take 15–30 minutes on a brand new cartridge. Do not rush it.

**Mount failed with errors**
Try Option 5 (ltfsck) to check and repair the tape filesystem. Run it with repair mode enabled.

**Drive not detected**
Make sure the drive is powered on before the server boots, or check with:
```bash
lsscsi
```

**Files copied but tape seems full too fast**
Check actual usage with `df -h /mnt/tape`. Remember LTFS shows native capacity (~17.5 TB) — compression ratios vary by data type.

---

## Tape Capacity Reference

| Data Type | Approximate Capacity |
|-----------|---------------------|
| Raw uncompressed data | ~17.5 TB native |
| Typical research data (mixed) | 20–30 TB |
| Already-compressed files | ~17.5 TB (no compression gain) |

---

## Tape Labeling Convention

Use a consistent naming scheme for your tapes and keep a log. Suggested format:

```
[PROJECT]-[YEAR]-[SEQUENCE]
Example: CRYO-2024-A, CRYO-2024-B, ARCH-2025-A
```

Write the label on the tape cartridge's physical label as well.

---

*For help contact your system administrator.*
