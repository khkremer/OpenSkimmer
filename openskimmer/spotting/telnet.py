"""Telnet spot server speaking DX-cluster syntax (F6).

Clients (N1MM+, DXLab SpotCollector, plain telnet) connect, optionally
answer the login prompt with their call ("ask for call" behavior, on by
default), and then receive skimmer spots in the RBN/AR-Cluster line format,
extended with per-spot SNR and WPM:

DX de KH6XYZ-#:  14026.0  W1AW           24 dB  29 WPM  CQ            1516Z

The connect banner supplies the operator's callsign, name, and grid square
so clients need no separate configuration. Standard cluster queries are
answered on the same connection:

  sh/dx [n] [mode]   last n spots (default 10), optionally "cw" filtered
  SETT               skimmed segments, CW Skimmer style
                     ("SETT: vlNormal 14000.0-14070.0"); reflects live
                     slice coverage, so dead KiwiSDR slices drop out
  bye / quit / exit  disconnect
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional, Set

from .spotter import Spot

log = logging.getLogger(__name__)


def format_spot(spot: Spot, de_call: str) -> str:
    """Native CW Skimmer telnet spot line.

    No mode column: the RBN Aggregator parses "<snr> dB" directly after the
    call and rejects lines with a mode field ("Not in Skimmer spot format").
    The "CW" seen on RBN feeds is added by the RBN server, not the skimmer.
    """
    tm = time.gmtime(spot.time_utc)
    khz = spot.freq_hz / 1000.0
    # By the time a spot reaches here, Spotter has already decided what a
    # repetition-only sighting was: it promotes one on a clear frequency to
    # CQ (a CQ we failed to copy) and leaves one sitting on another station's
    # run frequency as DUPE (an S&P caller answering someone). Anything still
    # marked DUPE is therefore a caller, and is reported as DE so it stays in
    # the local telnet feed and the waterfall while the RBN Aggregator holds
    # it, exactly as it holds every other non-CQ spot. Publishing callers as
    # CQ left 44.8% of a day's spots unconfirmable by any other RBN skimmer
    # (last of 175, against a 1.6% median).
    activity = "DE" if spot.activity == "DUPE" else spot.activity
    return (f"DX de {de_call + '-#:':<9} {khz:8.1f}  {spot.call:<15}"
            f"{spot.snr_db:2d} dB  {spot.wpm:2d} WPM  {activity:<14}"
            f"{tm.tm_hour:02d}{tm.tm_min:02d}Z")


# The RBN Aggregator identifies and registers a primary skimmer by parsing
# this greeting, so it must match Skimmer Server's banner shape. This is a
# protocol-compatibility emulation for a documented ham data feed -- the spots
# are genuine and under the operator's own call.
SKIMMER_VERSION = "1.6.0.145"


class TelnetSpotServer:
    def __init__(self, de_call: str, host: str = "0.0.0.0", port: int = 7300,
                 op_name: str = "", grid: str = "", ask_call: bool = True,
                 history: int = 500, segments_fn=None, qth: str = "",
                 version: str = SKIMMER_VERSION):
        """segments_fn, when given, returns the currently skimmed absolute
        frequency ranges as [(low_hz, high_hz), ...] for the SETT reply."""
        self.de_call = de_call.upper()
        self.host = host
        self.port = port
        self.op_name = op_name
        self.grid = grid.upper()
        self.qth = qth
        self.version = version
        self.ask_call = ask_call
        self.segments_fn = segments_fn
        self._history: deque = deque(maxlen=history)
        self._writers: Set[asyncio.StreamWriter] = set()
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("Telnet spot server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        # Close the client connections BEFORE waiting on the server: since
        # Python 3.12.1 wait_closed() waits for every connection, and the RBN
        # Aggregator never disconnects, so the old order hung forever. That
        # hang is what kept a skimmer whose bands had all failed at boot
        # alive and spot-less instead of exiting for systemd to restart it.
        if self._server:
            self._server.close()
        for w in list(self._writers):
            w.close()
        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5.0)
            except asyncio.TimeoutError:
                log.warning("telnet server: clients still open after 5 s")

    def broadcast(self, spot: Spot) -> None:
        """Thread-safe entry is via loop.call_soon_threadsafe(broadcast, spot)."""
        self._history.append(spot)
        line = (format_spot(spot, self.de_call) + "\r\n").encode("ascii", "replace")
        for w in list(self._writers):
            try:
                w.write(line)
            except (ConnectionError, RuntimeError):
                self._writers.discard(w)

    # -- per-connection ------------------------------------------------------

    def _banner(self) -> bytes:
        # match Skimmer Server's greeting so the RBN Aggregator's primary
        # handshake recognizes and registers us (it parses the operator/QTH/
        # grid from these lines)
        op = self.op_name or self.de_call
        lines = ["Welcome to the Skimmer Server Telnet cluster port!",
                 f"Skimmer Server v.{self.version} is operated by "
                 f"{op}, {self.de_call}"]
        if self.qth and self.grid:
            lines.append(f"in {self.qth} ({self.grid})")
        elif self.grid:
            lines.append(f"in {self.grid}")
        elif self.qth:
            lines.append(f"in {self.qth}")
        if self.ask_call:
            lines.append("Please enter your callsign:")
        return ("\r\n".join(lines) + "\r\n").encode("ascii", "replace")

    def _prompt(self, user: str) -> bytes:
        # CW Skimmer's per-command prompt; the Aggregator's primary handshake
        # sends a command, waits for this prompt, then sends the next
        ts = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime())
        return f"{user} de SKIMMER {ts} CwSkimmer >\r\n".encode("ascii",
                                                                "replace")

    def _show_dx(self, args: list) -> bytes:
        count, mode = 10, None
        for a in args:
            if a.isdigit():
                count = max(1, min(int(a), 100))
            else:
                mode = a.upper()
        spots = [s for s in self._history if mode is None or s.mode == mode]
        spots = spots[-count:]
        if not spots:
            return b"No spots\r\n"
        return ("\r\n".join(format_spot(s, self.de_call) for s in spots)
                + "\r\n").encode("ascii", "replace")

    def _sett(self) -> bytes:
        """CW Skimmer's SETT reply: validation level + skimmed segments in
        kHz (the RBN Aggregator queries this to learn segment coverage)."""
        segs = self.segments_fn() if self.segments_fn else []
        body = ",".join(f"{lo / 1000:.1f}-{hi / 1000:.1f}" for lo, hi in segs)
        return f"SETT: vlNormal {body}\r\n".encode("ascii", "replace")

    @staticmethod
    def _command_head(line: str) -> str:
        """First token, lowercased, with the Aggregator's ``SKIMMER/`` command
        prefix stripped (it sends ``SKIMMER/SETT``, not ``SETT``)."""
        head = line.strip().lower().split()[:1]
        h = head[0] if head else ""
        return h[len("skimmer/"):] if h.startswith("skimmer/") else h

    def _is_command(self, line: str) -> bool:
        return self._command_head(line) in (
            "sett", "sh/dx", "show/dx", "bye", "quit", "exit")

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        # None means "still waiting for the login callsign"
        user = None if self.ask_call else "GUEST"
        try:
            writer.write(self._banner())
            if not self.ask_call:
                writer.write(self._prompt(user))
            await writer.drain()
            # stream spots from the moment we connect
            self._writers.add(writer)
            log.info("Telnet client %s connected", peer)
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("ascii", "replace").strip()
                if not text:
                    continue
                low = text.lower()
                if low in ("bye", "quit", "exit"):
                    break
                if user is None:
                    # the login line: the Aggregator's primary sends its
                    # callsign here (a plain token), then drives command/
                    # prompt turns; commands sent before login are answered.
                    if not self._is_command(low):
                        user = text.upper()
                        writer.write(self._prompt(user))
                        await writer.drain()
                        log.info("Telnet client %s is %s", peer, user)
                        continue
                # a command line: respond, then reprint the prompt (the turn
                # signal the Aggregator's primary handshake waits for)
                parts = low.split()
                head = self._command_head(low)
                if head in ("sh/dx", "show/dx"):
                    writer.write(self._show_dx(parts[1:]))
                elif head == "sett":
                    writer.write(self._sett())
                else:
                    writer.write(f'Unknown command: "{text}"\r\n'
                                 .encode("ascii", "replace"))
                writer.write(self._prompt(user or "GUEST"))
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError,
                asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            log.info("Telnet client %s disconnected", peer)
