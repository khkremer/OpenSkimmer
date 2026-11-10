#!/bin/sh
# Start OpenSkimmer with sensible defaults.
#
# Usage:
#   ./start_skimmer.sh                     # bands from skimmer.local.conf,
#                                          # or the single-band kiwi fallback
#   FREQ=7030000 ./start_skimmer.sh        # override the fallback band
#   ./start_skimmer.sh --record x.c64 -v   # extra args pass through
#
# skimmer.local.conf (untracked; sourced first, so values there win) may set:
#   BANDS          space-separated --band specs, [source[@host]:]center[/span]
#                  e.g. "hl2@192.0.2.10:14030000/192000
#                        hl2@192.0.2.10:7030000/192000
#                        kiwi@kiwisdr.local:10110000/24000"
#                  Each band gets a waterfall on web-port, web-port+1, ...
#   SOURCE FREQ SPAN   single-band fallback when BANDS is unset
#                      (SOURCE = kiwi | hl2 | sdrplay | miri | ...; HOST is
#                       used only for kiwi)
#   DECODER        hmm (default) or classical
#   SEGMENTER      timing (default: the decoder's own word breaks) or widest
#                  (also cut a word at a gap clearly wider than the rest
#                  inside it)
#   MAX_CHANNELS   most signals tracked at once per band (default 100)
#   MAX_WPM        never spot a signal decoded faster than this (default 50
#                  in openskimmer itself). The decoder clamps its dit estimate
#                  at 15 ms = 80 wpm, so a "80 wpm" spot is that estimate
#                  railed against its floor by a carrier or noise source
#   BLOCK          callsigns never to spot, space- or comma-separated, for
#                  calls a noise source produces repeatedly
#   LOG_CHANNELS   path to append every decoded word to (one line per word,
#                  with time/frequency/channel/wpm/snr). This is the only
#                  record of the text the extractor actually saw, so it is
#                  what a spot bust has to be diagnosed against -- without it
#                  you can see WHAT was spotted but never WHY. It grows
#                  steadily; rotate or truncate it periodically
#   LOG_CHARS      path to append every decoded character to, with the gap
#                  before it and the decoder's dit and word threshold
#                  (--log-chars; tools/replay_chars.py replays it). Several
#                  times the size of LOG_CHANNELS; for diagnosis, not
#                  for leaving on
#   BAND_OFFSET    CENTER_HZ=OFFSET_HZ, shift one band's reported frequency
#   PPM GAIN       SoapySDR frequency correction / manual gain (sdrplay/miri)
#   MY_CALL OP_NAME GRID QTH HOST KIWI_PASSWORD
#
# e.g. an SDRplay on 20m with a measured clock error:
#   SOURCE="sdrplay"; FREQ=14048000; SPAN=192000
#   MY_CALL="N0CALL"; DECODER="hmm"; PPM="11.6"

cd "$(dirname "$0")" || exit 1
[ -f skimmer.local.conf ] && . ./skimmer.local.conf

SOURCE="${SOURCE:-kiwi}"        # fallback source when BANDS is unset
HOST="${HOST:-kiwisdr.local}"   # kiwi host (fallback)
FREQ="${FREQ:-14030000}"        # 20m CW segment center (fallback band)
SPAN="${SPAN:-24000}"           # sample rate / span in Hz (fallback band)
MY_CALL="${MY_CALL:-SKIMMER}"
KIWI_PASSWORD="${KIWI_PASSWORD:-}"
OP_NAME="${OP_NAME:-}"          # announced in the telnet banner (optional)
GRID="${GRID:-}"                # grid square for the telnet banner (optional)
QTH="${QTH:-}"                  # location text for the telnet banner (optional)
DECODER="${DECODER:-}"          # hmm (default) or classical
SEGMENTER="${SEGMENTER:-}"      # timing (default) or widest
PPM="${PPM:-}"                  # SoapySDR ppm correction (sdrplay/miri)
GAIN="${GAIN:-}"                # SoapySDR manual gain, disables AGC (optional)
BANDS="${BANDS:-}"

# prepend optional args safely (names may contain spaces)
[ -n "$GAIN" ] && set -- --gain "$GAIN" "$@"
[ -n "$PPM" ] && set -- --ppm "$PPM" "$@"
# BAND_OFFSET="CENTER_HZ=OFFSET_HZ" shifts the reported frequency of one band
[ -n "$BAND_OFFSET" ] && set -- --band-offset "$BAND_OFFSET" "$@"
[ -n "$QTH" ] && set -- --qth "$QTH" "$@"
[ -n "$GRID" ] && set -- --grid "$GRID" "$@"
[ -n "$OP_NAME" ] && set -- --op-name "$OP_NAME" "$@"
[ -n "$DECODER" ] && set -- --decoder "$DECODER" "$@"
[ -n "$SEGMENTER" ] && set -- --segmenter "$SEGMENTER" "$@"
[ -n "$MAX_WPM" ] && set -- --max-wpm "$MAX_WPM" "$@"
[ -n "$MAX_CHANNELS" ] && set -- --max-channels "$MAX_CHANNELS" "$@"
[ -n "$LOG_CHANNELS" ] && set -- --log-channels "$LOG_CHANNELS" "$@"
[ -n "$LOG_CHARS" ] && set -- --log-chars "$LOG_CHARS" "$@"
# BLOCK is a space- or comma-separated list of callsigns never to spot
for _c in $(echo "${BLOCK:-}" | tr ',' ' '); do
    set -- --block "$_c" "$@"
done

if [ -n "$BANDS" ]; then
    for b in $BANDS; do
        set -- --band "$b" "$@"
    done
else
    set -- --source "$SOURCE" --freq "$FREQ" --sample-rate "$SPAN" "$@"
    [ "$SOURCE" = "kiwi" ] && set -- --host "$HOST" "$@"
fi

# Use the callsign databases if present
# (https://www.supercheckpartial.com/MASTER.SCP and
#  https://data.reversebeacon.net/downloads/patt3ch/patt3ch.lst)
#
# The two lists do different jobs. The curated MASTER.SCP both BLESSES (a
# listed call spots on a single copy) and VETOES (a listed call seen on this
# signal discredits an overlapping garble). The broader combined.txt only
# vetoes: calls listed there and not in MASTER.SCP were ~2x over-represented
# among spots no other RBN skimmer confirmed, so they should not buy a
# single-copy spot -- but they are still useful for discrediting garbles.
# With both files present we therefore pass both roles separately; with only
# one, it does both jobs as before.
SCP_ARGS=""
if [ -f MASTER.SCP ]; then
    SCP_ARGS="--scp MASTER.SCP"
    [ -f combined.txt ] && SCP_ARGS="$SCP_ARGS --scp-weak combined.txt"
elif [ -f combined.txt ]; then
    SCP_ARGS="--scp combined.txt"
fi
[ -f patt3ch.lst ] && SCP_ARGS="$SCP_ARGS --patterns patt3ch.lst"

if [ ! -x .venv/bin/openskimmer ]; then
    echo "error: .venv/bin/openskimmer not found -- install first:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/pip install --upgrade pip" >&2
    echo "  .venv/bin/pip install -e '.[dev]'" >&2
    exit 1
fi

if [ -n "$BANDS" ]; then
    echo "Skimming bands: $BANDS"
    echo "Waterfalls: http://localhost:7380 (then 7381, ... per band)"
else
    dev="$SOURCE"
    [ "$SOURCE" = "kiwi" ] && dev="$HOST"
    echo "Skimming $dev: center $((FREQ / 1000)) kHz, span $((SPAN / 1000)) kHz"
    echo "Waterfall: http://localhost:7380"
fi
echo "Spots: telnet localhost 7300"

# Exported, not passed as an argument: argv is world-readable through `ps`,
# whereas a running process's environment is not. main.py falls back to
# $KIWI_PASSWORD when --kiwi-password is absent.
export KIWI_PASSWORD

exec .venv/bin/openskimmer \
    $SCP_ARGS \
    --my-call "$MY_CALL" \
    "$@"
