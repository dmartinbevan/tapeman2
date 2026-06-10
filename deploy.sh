#!/bin/bash
# tapeman2 deploy helper
# Unzips a build, syncs files into the repo by type, commits, and pushes.
#
# Usage:
#   ./deploy.sh                      # uses ~/Downloads/fileslin.zip
#   ./deploy.sh ~/Downloads/fileslin.zip
#   ./deploy.sh -m "commit message"  # skip the prompt
#
# After pushing, run  sudo tapeman2-update  on each server.

set -e

# ── Config (edit these paths if your layout differs) ──────────────────────────
REPO_DIR="$HOME/icloud/programming/tapeman2"
DEFAULT_ZIP="$HOME/Downloads/fileslin.zip"

# ── Parse args ────────────────────────────────────────────────────────────────
ZIP=""
COMMIT_MSG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--message) COMMIT_MSG="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: ./deploy.sh [path-to-zip] [-m \"commit message\"]"
            exit 0 ;;
        *) ZIP="$1"; shift ;;
    esac
done
ZIP="${ZIP:-$DEFAULT_ZIP}"

# ── Sanity checks ─────────────────────────────────────────────────────────────
if [ ! -f "$ZIP" ]; then
    echo "✘ Zip not found: $ZIP"
    echo "  Download the build first, or pass the path: ./deploy.sh path/to/fileslin.zip"
    exit 1
fi
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "✘ Repo not found at $REPO_DIR (no .git directory)."
    echo "  Edit REPO_DIR at the top of this script if your path differs."
    exit 1
fi

echo ""
echo "  tapeman2 deploy"
echo "  ==============="
echo "  Zip:  $ZIP"
echo "  Repo: $REPO_DIR"
echo ""

# ── Unzip to a temp dir ───────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
unzip -o -q "$ZIP" -d "$TMP"

# The zip extracts to a single top-level dir (fileslin/ or filesmac/)
SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)"
if [ -z "$SRC" ]; then
    SRC="$TMP"
fi
echo "  Extracted: $(basename "$SRC")"

# ── Ensure repo subdirs exist ─────────────────────────────────────────────────
mkdir -p "$REPO_DIR/lib" "$REPO_DIR/conf"

# ── Sync files into the repo by type ──────────────────────────────────────────
# Python modules -> lib/
# .conf files    -> conf/
# everything else (executables, install scripts, docs) -> repo root
copied=0
for f in "$SRC"/*; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    case "$base" in
        *.py)
            cp "$f" "$REPO_DIR/lib/$base"
            echo "    lib/$base"
            copied=$((copied+1)) ;;
        *.conf)
            cp "$f" "$REPO_DIR/conf/$base"
            echo "    conf/$base"
            copied=$((copied+1)) ;;
        *)
            cp "$f" "$REPO_DIR/$base"
            echo "    $base"
            copied=$((copied+1)) ;;
    esac
done
echo "  Synced $copied file(s)."
echo ""

# ── Show what changed ─────────────────────────────────────────────────────────
cd "$REPO_DIR"
if git diff --quiet && git diff --cached --quiet; then
    echo "  No changes versus the current repo — nothing to commit."
    echo "  (The build matches what's already committed.)"
    exit 0
fi

echo "  Changed files:"
git -c color.ui=always status --short | sed 's/^/    /'
echo ""

# ── Commit message ────────────────────────────────────────────────────────────
if [ -z "$COMMIT_MSG" ]; then
    printf "  Commit message: "
    read -r COMMIT_MSG
fi
if [ -z "$COMMIT_MSG" ]; then
    echo "✘ Empty commit message — aborting (nothing committed or pushed)."
    exit 1
fi

# ── Commit & push ─────────────────────────────────────────────────────────────
git add -A
git commit -m "$COMMIT_MSG"
echo ""
echo "  Pushing to GitHub..."
if git push; then
    echo ""
    echo "  ✔ Deployed: \"$COMMIT_MSG\""
    echo ""
    echo "  Next: run  sudo tapeman2-update  on each server:"
    echo "        lakota, gcarchive  (and jcarchive once Mac LTFS is sorted)"
    echo ""
else
    echo ""
    echo "  ✘ Push failed. The commit is saved locally — resolve the issue"
    echo "    (e.g. 'git pull --rebase') and run 'git push' again."
    exit 1
fi
