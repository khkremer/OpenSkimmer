#!/bin/sh
# Build a deployment ZIP of OpenSkimmer for copying to another machine.
#
# Contents: the committed source tree (via git archive, so only tracked
# files -- never .venv, recordings, or scratch) PLUS, unless --no-local is
# given, the local call databases and skimmer.local.conf so it runs out of
# the box on the target.
#
# WARNING: skimmer.local.conf holds your KiwiSDR password, so a ZIP built
# with local files is sensitive -- transfer it privately (scp/USB), don't
# post it. Use --no-local for a shareable source-only ZIP.
#
# Usage:  deploy/make-deploy-zip.sh [--no-local]

set -e
cd "$(dirname "$0")/.."          # repo root

include_local=1
[ "$1" = "--no-local" ] && include_local=0

command -v git >/dev/null || { echo "git not found" >&2; exit 1; }
command -v zip >/dev/null || { echo "zip not found (apt install zip)" >&2; exit 1; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

git archive --prefix=openskimmer/ HEAD | tar -x -C "$stage"

if [ "$include_local" = 1 ]; then
    for f in MASTER.SCP patt3ch.lst combined.txt pota_activators.txt \
             skimmer.local.conf; do
        if [ -f "$f" ]; then cp "$f" "$stage/openskimmer/"; fi
    done
fi

out="$(pwd)/openskimmer-$(date +%Y%m%d).zip"
rm -f "$out"
( cd "$stage" && zip -qr "$out" openskimmer )

echo "wrote $out"
if [ "$include_local" = 1 ] && [ -f skimmer.local.conf ]; then
    echo "NOTE: this ZIP includes skimmer.local.conf (KiwiSDR password) --" \
         "keep it private"
fi
