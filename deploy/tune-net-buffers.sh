#!/bin/bash
# Set, persist and verify the socket-buffer sysctls that radiod and OpenSkimmer
# need for high-rate multicast IQ, then report what actually took effect.
#
# Why this exists: `sysctl -w` only changes the running kernel, so the values
# are lost on the next boot -- and even while they are set, a socket keeps
# whatever buffer it was given when it was created. A running radiod or
# skimmer therefore does NOT pick up a new limit until it is restarted.
# This script separates those two failure modes and tells you which one you
# are looking at.
#
# Usage:  sudo deploy/tune-net-buffers.sh          # apply + verify
#         deploy/tune-net-buffers.sh --show        # report only, no root
#
# Linux only (reads /proc/sys and writes /etc/sysctl.d).

set -u

CONF=/etc/sysctl.d/99-openskimmer.conf
RTP_PORT=${RTP_PORT:-5004}

# The buffer the ka9q backend requests (openskimmer/sdr/ka9q.py: 1 << 21).
# The kernel reports back double the requested size, for bookkeeping.
WANT_SO_RCVBUF=$((1 << 21))
EXPECT_RB=$((WANT_SO_RCVBUF * 2))

# key=value, in the order they should be reported
# Targets are MINIMA. Never lower a value that is already higher -- see
# the clamp in the writer below. This script previously set wmem_default to
# 1 MB unconditionally, which on one host LOWERED it from the 5 MB another
# sysctl file had set. radiod does not call setsockopt(SO_SNDBUF), so it
# inherits wmem_default for the socket it sends ten multicast channels on;
# its send buffer went from 5 MB to 1 MB and it began discarding ~27 % of
# every band at the source, with the RTP sequence numbers already consumed.
# Subscribers saw gaps for packets that never reached the wire, and the
# symptom looked exactly like network loss for days.
SETTINGS="
net.core.rmem_max=33554432
net.core.wmem_max=33554432
net.core.rmem_default=1048576
net.core.wmem_default=8388608
"

SHOW_ONLY=0
[ "${1:-}" = "--show" ] && SHOW_ONLY=1

red()   { printf '\033[31m%s\033[0m' "$1"; }
green() { printf '\033[32m%s\033[0m' "$1"; }
bold()  { printf '\033[1m%s\033[0m\n' "$1"; }

get() { cat "/proc/sys/$(echo "$1" | tr . /)" 2>/dev/null || echo "?"; }

report() {
    printf '%-28s %14s\n' "SETTING" "VALUE"
    for kv in $SETTINGS; do
        k=${kv%%=*}
        printf '%-28s %14s\n' "$k" "$(get "$k")"
    done
}

bold "== current =="
report

# -- look for anything that would override us -------------------------------
# This runs BEFORE the --show exit on purpose: it is pure grep over
# world-readable files, and "which file is winning?" is the whole question
# when the live values disagree with ours. Gating it behind the apply path
# (which needs root) meant --show reported the mismatch and then refused to
# say where it came from -- observed 2026-08-12, when the live rmem_max read
# 16777216 against our 33554432 and --show gave no hint why.
bold ""
bold "== existing definitions elsewhere =="
found=0
for f in /etc/sysctl.conf /etc/sysctl.d/*.conf /usr/lib/sysctl.d/*.conf \
         /lib/sysctl.d/*.conf /run/sysctl.d/*.conf; do
    [ -f "$f" ] || continue
    [ "$f" = "$CONF" ] && continue
    if grep -qE '^[[:space:]]*net\.core\.(r|w)mem_(max|default)' "$f" 2>/dev/null; then
        echo "  $f:"
        grep -nE '^[[:space:]]*net\.core\.(r|w)mem_(max|default)' "$f" | sed 's/^/    /'
        found=1
    fi
done
if [ "$found" -eq 0 ]; then
    echo "  (no FILE sets these)"
    if [ -f "$CONF" ]; then
        echo
        echo "  Ours exists and nothing else defines them, yet the values above"
        echo "  disagree -- so something is setting them at RUNTIME, after"
        echo "  systemd-sysctl ran. Usual suspects: tailscaled, docker,"
        echo "  libvirt, a NetworkManager/networkd dispatcher hook, rc.local."
        echo "  Find it with:  sudo grep -rn 'net\.core\.[rw]mem' /etc /usr/lib/systemd 2>/dev/null"
    else
        echo "  ...and $CONF does not exist either, so these are somebody"
        echo "  else's values entirely. Run this script with sudo to install ours."
    fi
fi
echo
echo "  NOTE: files are applied in lexical order across these directories,"
echo "  so a later file wins. Ours is 99- so it should apply last."

if [ "$SHOW_ONLY" -eq 1 ]; then
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo
    echo "Applying changes needs root: sudo $0"
    exit 1
fi

# -- write the persistent file ----------------------------------------------
bold ""
bold "== writing $CONF =="
{
    echo "# Installed by deploy/tune-net-buffers.sh"
    echo "# Socket buffers for ka9q-radio multicast IQ (radiod send, skimmer receive)."
    for kv in $SETTINGS; do
        k=${kv%%=*}; want=${kv#*=}
        cur=$(get "$k")
        # a tuning script that LOWERS a limit is a footgun: keep whichever
        # is larger, and say so rather than silently shrinking a buffer
        case "$cur" in
            ''|*[!0-9]*) use=$want ;;
            *) if [ "$cur" -gt "$want" ]; then
                   use=$cur
                   echo "# $k: keeping existing $cur (target was $want)" >&2
               else
                   use=$want
               fi ;;
        esac
        echo "$k = $use"
    done
} > "$CONF"
chmod 644 "$CONF"
cat "$CONF" | sed 's/^/  /'

# -- apply -------------------------------------------------------------------
bold ""
bold "== applying =="
sysctl --system >/dev/null 2>&1 || sysctl -p "$CONF" >/dev/null
echo "  sysctl --system done"

# -- verify ------------------------------------------------------------------
bold ""
bold "== after =="
fail=0
printf '%-28s %14s %14s   %s\n' "SETTING" "WANTED" "ACTUAL" "RESULT"
for kv in $SETTINGS; do
    k=${kv%%=*}; want=${kv#*=}; have=$(get "$k")
    if [ "$have" = "$want" ]; then
        printf '%-28s %14s %14s   %s\n' "$k" "$want" "$have" "$(green OK)"
    else
        printf '%-28s %14s %14s   %s\n' "$k" "$want" "$have" "$(red MISMATCH)"
        fail=1
    fi
done

# -- what the live sockets actually got --------------------------------------
bold ""
bold "== live sockets on UDP :$RTP_PORT =="
if ! command -v ss >/dev/null 2>&1; then
    echo "  (ss not installed -- skipping; apt install iproute2)"
else
    sock=$(ss -uanm 2>/dev/null | grep -A1 ":$RTP_PORT[[:space:]]")
    if [ -z "$sock" ]; then
        echo "  no socket bound -- is anything subscribed right now?"
    else
        echo "$sock" | sed 's/^/  /'
        rb=$(echo "$sock" | grep -o 'rb[0-9]*' | head -1 | tr -d 'rb')
        echo
        if [ -n "$rb" ]; then
            if [ "$rb" -ge "$EXPECT_RB" ]; then
                echo "  rb=$rb -- $(green 'the 2 MB request landed'); this socket is fine."
            else
                echo "  rb=$rb, expected >= $EXPECT_RB."
                echo "  $(red 'This socket predates the new limit.') The sysctl is set but"
                echo "  the process must be RESTARTED to get a bigger buffer."
            fi
        fi
    fi
fi

# -- drop counters -----------------------------------------------------------
bold ""
bold "== drop counters =="
if command -v nstat >/dev/null 2>&1; then
    nstat -az 2>/dev/null | grep -E 'UdpRcvbufErrors|UdpSndbufErrors|UdpInErrors' \
        | sed 's/^/  /'
    echo
    echo "  These are cumulative since boot. Re-run after a few minutes of"
    echo "  traffic; what matters is whether they still CLIMB, not their size."
else
    grep -E 'Udp:' -A1 /proc/net/snmp | sed 's/^/  /'
fi

# -- conclusion --------------------------------------------------------------
bold ""
bold "== next =="
if [ "$fail" -ne 0 ]; then
    echo "  Some values did not take. Check the override list above -- another"
    echo "  file in /etc/sysctl.d may be setting them after ours."
    exit 1
fi
cat <<EOF
  Limits are set and will survive reboot.

  Sockets are given their buffer at creation, so restart both ends now:
      sudo systemctl restart radiod@skimmer
      sudo systemctl restart openskimmer

  Then watch for a few minutes:
      journalctl -u openskimmer -f | grep -i 'packet.*lost'
      nstat -az UdpRcvbufErrors        # run twice, minutes apart

  If losses continue at a similar rate with rb=$EXPECT_RB, the problem is NOT
  buffering -- the pipeline is slower than real time and no buffer will fix
  it. Check with:  top -H -p \$(pgrep -f openskimmer)
EOF
