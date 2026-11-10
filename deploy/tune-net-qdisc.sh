#!/bin/bash
# Egress tuning for radiod's multicast interface, persisted against
# NetworkManager reapplying the defaults. Two independent settings:
#   1. a plain-FIFO qdisc instead of fq_codel (AQM drops)
#   2. Ethernet flow control OFF (802.3x PAUSE frames stall the sender)
#
# Why this exists: the stock qdisc on a modern kernel is fq_codel, an
# Active Queue Management discipline that deliberately drops packets once
# queueing delay exceeds its `target` (5 ms default) -- that is how it
# makes TCP back off. radiod's multicast IQ is UDP: it never backs off, so
# instead of slowing down, fq_codel just keeps dropping it. Measured on
# Measured 2026-09-03 on the radiod host: 132,777,230 of 348,732,556 packets offered to
# enp1s0 since boot (~38 %) were dropped by fq_codel, while BOTH of the
# socket-level counters stayed clean -- UdpSndbufErrors mostly 0,
# UdpRcvbufErrors permanently 0. Qdisc drops are a THIRD loss point, one
# layer above both socket counters, invisible to `ip -s link` and to
# tools/watch_sndbuf.py's own trigger condition (see there).
#
# This is a return of the mechanism behind the 2026-08-18/21 packet-loss
# incident. That investigation traced it to a marginal cable capping the
# link at 100 Mb/s and fixed the cable -- correctly, but it only removed
# the trigger (link saturation), not the underlying AQM policy that
# punishes non-congestion-aware UDP at ANY line rate. fq_codel was never
# replaced, so a busy band re-created enough queueing delay (radiod plus
# three DSP worker processes contending for 4 cores appears to delay the
# TX softirq past the 5 ms target) to trip the same drop behaviour again,
# this time on a healthy gigabit link.
#
# pfifo has no AQM: it queues up to `limit` packets and only drops when
# genuinely full. That trades TCP-friendliness (irrelevant -- nothing else
# of consequence shares this NIC) for never discarding real-time UDP that
# a plain queue could still have carried.
#
# Usage:  sudo deploy/tune-net-qdisc.sh              # apply + verify
#         deploy/tune-net-qdisc.sh --show            # report only, no root
#
# Linux only. NetworkManager (this host's renderer, see /etc/netplan) does
# not manage qdisc, but it DOES reset the interface -- and with it the
# qdisc, back to the kernel default -- on carrier changes (a cable reseat,
# a link renegotiation), not only on reboot. A one-shot systemd unit alone
# would miss that; installing this script as a NetworkManager dispatcher
# hook (see install_dispatcher() below) re-applies it on every "up" event.

set -u

# Auto-detect the interface carrying the default route rather than assuming a
# name. enp1s0 on one host, enp3s0f0 on another, and running this there with
# the old hardcoded default produced only 'Cannot find device "enp1s0"'.
# On a host where the default route is via br0 (which bridges enp1s0 and the Home
# Assistant VM's tap), and pause/qdisc settings must be applied to the PHYSICAL
# port, not the bridge -- so unwrap a bridge to its non-virtual member.
#
# Note $IFACE cannot be passed through plain `sudo IFACE=... script`: sudoers
# here sets env_reset, which drops inline assignments. Use `sudo env IFACE=...`
# if you need to override the detection.
detect_iface() {
    local i
    i=$(ip route get 1.1.1.1 2>/dev/null | grep -o 'dev [^ ]*' | awk '{print $2}')
    if [ -d "/sys/class/net/$i/brif" ]; then
        for m in /sys/class/net/"$i"/brif/*; do
            m=$(basename "$m")
            case "$m" in vnet*|veth*|tap*) continue ;; esac
            i=$m; break
        done
    fi
    echo "$i"
}
IFACE=${IFACE:-$(detect_iface)}
if [ -z "$IFACE" ] || [ ! -d "/sys/class/net/$IFACE" ]; then
    echo "error: could not determine a network interface (got '${IFACE:-}')" >&2
    echo "       pass one explicitly:  sudo env IFACE=eth0 $0" >&2
    exit 1
fi
echo "using interface: $IFACE"
LIMIT=${LIMIT:-10240}

show() {
    echo "-- tc -s qdisc show dev $IFACE --"
    tc -s qdisc show dev "$IFACE" 2>&1
}

if [ "${1:-}" = "--show" ]; then
    show
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (sudo) to change the qdisc" >&2
    exit 1
fi

echo "before:"
show
echo
tc qdisc replace dev "$IFACE" root pfifo limit "$LIMIT"

# Ethernet flow control OFF. Root-caused 2026-09-04 after a
# multi-day hunt: radiod generates ~49 Mbit/s of multicast, the UNMANAGED
# switch floods it to every port (no IGMP snooping), and when a PC on that
# switch sleeps its NIC drops to 10baseT for Wake-on-LAN. The switch cannot
# push 49 Mbit/s at a 10 Mbit port, so it sends PAUSE frames back -- and
# the host, obeying them, throttled its whole transmit path to ~10 Mbit/s.
# Effect: 80 % of radiod's sendto() calls failed with ENOBUFS (3,515/s),
# its 8 MB send buffer sat pinned for HOURS, and every subscriber saw RTP
# gaps. Measured the moment pause was disabled: 855 -> 4,370 sends/s,
# 3,515 -> 0 failures/s, 9 -> 49 Mbit/s, socket drained instantly.
#
# Turning pause off is correct for real-time UDP: PAUSE has no notion of
# which traffic matters, so one sleeping PC stalls the SOURCE for every
# subscriber. Better to let the switch drop what a slow port cannot take.
# The diagnostic trap: nothing anywhere reports an error -- no NIC errors,
# no qdisc drops, no RcvbufErrors, CPU 64 % idle, wire 99 % idle. The host
# is not failing, it is being politely asked to stop and complying.
ethtool -A "$IFACE" tx off rx off 2>/dev/null \
  && echo "flow control disabled on $IFACE" \
  || echo "warning: could not disable flow control on $IFACE" >&2

echo "after:"
show
echo "-- ethtool -a $IFACE --"
ethtool -a "$IFACE" 2>&1 | grep -E "^RX|^TX|negotiated"

DISPATCHER=/etc/NetworkManager/dispatcher.d/99-openskimmer-qdisc
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
# Rewrite the hook when it names a DIFFERENT interface, rather than skipping
# because a file is merely present. A hook written by a failed run on the wrong
# interface is worse than none: it exists, looks installed, and exits on its
# first line forever because NetworkManager never brings up the device it
# names. That happened 2026-09-08 -- a hook pinned to enp1s0 on a host
# whose NIC is enp3s0f0 -- and the later successful run left it in place.
NEEDS_HOOK=1
if [ -e "$DISPATCHER" ] && grep -q "\"\$1\" = \"$IFACE\"" "$DISPATCHER" 2>/dev/null; then
    NEEDS_HOOK=0
elif [ -e "$DISPATCHER" ]; then
    echo
    echo "existing dispatcher hook names a different interface; rewriting it:"
    grep -m1 '\[ "\$1" =' "$DISPATCHER" | sed 's/^/  was: /'
fi

if [ -d /etc/NetworkManager/dispatcher.d ] && [ "$NEEDS_HOOK" = 1 ]; then
    cat > "$DISPATCHER" <<EOF
#!/bin/sh
# Installed by $SCRIPT_PATH -- re-applies the pfifo qdisc every time
# NetworkManager brings $IFACE up, not only at boot. NM args: \$1=iface \$2=event.
[ "\$1" = "$IFACE" ] || exit 0
case "\$2" in up|dhcp4-change|dhcp6-change) ;; *) exit 0 ;; esac
IFACE=$IFACE LIMIT=$LIMIT "$SCRIPT_PATH" >/dev/null 2>&1
EOF
    chmod 755 "$DISPATCHER"
    echo
    echo "installed NetworkManager dispatcher hook: $DISPATCHER"
elif [ "$NEEDS_HOOK" = 0 ]; then
    echo
    echo "dispatcher hook already correct for $IFACE: $DISPATCHER"
else
    echo
    echo "warning: /etc/NetworkManager/dispatcher.d missing; settings will NOT"
    echo "         survive a reboot or carrier change on this host." >&2
fi
