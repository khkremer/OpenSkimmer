#!/bin/bash
# Rotate the Mac skimmer's stdout log.
#
# Why this exists: org.openskimmer.skimmer sends StandardOut/StandardError to
# ~/openskimmer/skimmer.log, and launchd never rotates it. A multicast
# packet-loss episode can write millions of "RTP packet(s) lost" warnings
# (one such log reached 2.1 GB, 27 million lines, in four days).
#
# Rotation is copy-truncate, NOT rename: launchd holds the file open on fd 1
# and 2, so renaming or deleting it would leave the writer appending to an
# unlinked inode and free no space at all. Truncating in place works because
# launchd opens the path with O_APPEND.
#
# Rotated copies go to ~/Library/Logs/openskimmer/ (override with DEST),
# outside the checkout, so a synced or backed-up folder doesn't carry them.
#
# Install (no sudo needed):
#   cp deploy/org.openskimmer.log-rotate.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/org.openskimmer.log-rotate.plist

set -u
LOG="${LOG:-$HOME/openskimmer/skimmer.log}"
DEST="${DEST:-$HOME/Library/Logs/openskimmer}"
MAX_BYTES="${MAX_BYTES:-209715200}"     # 200 MB
KEEP="${KEEP:-5}"

[ -f "$LOG" ] || exit 0
size=$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG" 2>/dev/null || echo 0)
[ "$size" -lt "$MAX_BYTES" ] && exit 0

mkdir -p "$DEST"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
# Keep a distilled daily loss summary rather than the raw millions of lines:
# the per-day aggregate is the part with any diagnostic value.
{
  echo "# skimmer.log rotated $stamp (was $size bytes)"
  echo
  grep -a "RTP packet(s) lost" "$LOG" 2>/dev/null \
    | awk '{d[$1]++; p[$1]+=$6} END{for(k in d) printf "%s  %d events  %d packets\n", k, d[k], p[k]}' \
    | sort
} > "$DEST/skimmer-$stamp.summary.txt"

gzip -c "$LOG" > "$DEST/skimmer-$stamp.log.gz" && : > "$LOG"

# Thin old generations.
ls -1t "$DEST"/skimmer-*.log.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; done
ls -1t "$DEST"/skimmer-*.summary.txt 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; done
