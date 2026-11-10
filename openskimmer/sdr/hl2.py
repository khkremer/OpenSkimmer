"""Hermes-Lite 2 backend: native HPSDR/Metis (protocol 1) UDP client.

Implements discovery, start/stop, command & control, and IQ sample parsing
directly against the open protocol -- no vendor DLLs (requirement 4.1).

Multi-receiver: the HL2 runs several DDCs inside one (single-host) UDP
session. A shared per-host ``_HL2Radio`` owns the socket, C&C, and the
receive loop, demultiplexing the interleaved per-receiver samples into one
queue per receiver; each ``HermesLite2Source`` is a thin per-band view
bound to one receiver slot. Multiple ``--band hl2@host:...`` arguments
therefore share one radio connection transparently. All receivers share
one sample rate (a protocol property).

Protocol references:
  - openhpsdr protocol 1 documentation ("Metis - How it works")
  - Hermes-Lite 2 gateware wiki; Quisk's hermes/quisk_hardware.py for the
    N2ADR filter-board control via the C2[7:1] user-output bits
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from .base import Capabilities, SDRSource

log = logging.getLogger(__name__)

METIS_PORT = 1024
MAX_RECEIVERS = 4           # stock HL2 gateware DDC count

_SAMPLE_RATE_CODES = {48000: 0x00, 96000: 0x01, 192000: 0x02, 384000: 0x03}

_SYNC = b"\x7f\x7f\x7f"


def n2adr_filter_byte(hi_hz: int, lo_hz: Optional[int] = None) -> int:
    """N2ADR filter relay byte. Bits 0-5 one-hot select the six low-pass
    filters; bit 6 inserts the 3 MHz high-pass that rejects AM-broadcast
    energy. With several receivers the LPF follows the highest frequency
    (each LPF passes the bands below it) and the HPF is engaged unless the
    lowest receiver is on 160 m (Quisk's multi-receiver rule)."""
    lo = hi_hz if lo_hz is None else lo_hz
    mhz = hi_hz / 1e6
    if mhz < 2.5:
        lpf = 0x01
    elif mhz < 4.5:
        lpf = 0x02
    elif mhz < 8.0:
        lpf = 0x04
    elif mhz < 16.0:
        lpf = 0x08
    elif mhz < 22.0:
        lpf = 0x10
    else:
        lpf = 0x20
    hpf = 0x00 if lo / 1e6 < 2.5 else 0x40
    return lpf | hpf


def _sign_extend_24(x: np.ndarray) -> np.ndarray:
    return (x ^ 0x800000) - 0x800000


def parse_iq_packet(pkt: bytes, n_rx: int = 1
                    ) -> "Tuple[Optional[List[np.ndarray]], bool]":
    """Parse one 1032-byte Metis IQ packet (endpoint 6).

    Each 512-byte USB frame carries sync(3) + C&C(5) followed by repeating
    sample groups of [I24 Q24 per receiver ..., mic16]. Returns a list of
    complex64 arrays (one per receiver, scaled to +/-1.0) or None if the
    packet is not an IQ packet, plus the PTT flag from the C&C header.
    """
    if len(pkt) < 1032 or pkt[0:3] != b"\xef\xfe\x01" or pkt[3] != 0x06:
        return None, False
    group = 6 * n_rx + 2
    per_frame = 504 // group
    ptt = False
    chunks: List[List[np.ndarray]] = [[] for _ in range(n_rx)]
    for off in (8, 8 + 512):
        frame = pkt[off:off + 512]
        if frame[0:3] != _SYNC:
            log.warning("HL2: lost USB frame sync")
            continue
        ptt = ptt or bool(frame[3] & 0x01)
        raw = np.frombuffer(frame, dtype=np.uint8,
                            count=per_frame * group, offset=8)
        raw = raw.reshape(per_frame, group).astype(np.int32)
        for r in range(n_rx):
            c = 6 * r
            # wire order per receiver: imaginary first, then real
            # (Quisk quisk.c read_rx_udp10: xi from bytes 0-2, xr from 3-5)
            im = _sign_extend_24((raw[:, c] << 16) | (raw[:, c + 1] << 8)
                                 | raw[:, c + 2])
            re = _sign_extend_24((raw[:, c + 3] << 16) | (raw[:, c + 4] << 8)
                                 | raw[:, c + 5])
            chunks[r].append((re + 1j * im).astype(np.complex64))
    if not chunks[0]:
        return None, ptt
    scale = np.float32(1.0 / (1 << 23))
    return [np.concatenate(c) * scale for c in chunks], ptt


class _HL2Radio:
    """One physical HL2: socket, C&C, receive/demux loop (single host)."""

    def __init__(self, host: Optional[str]):
        self.host = host
        self.lna_gain_db = 19
        self.filter_board = "n2adr"
        self.ptt_hang_s = 0.25
        self.receivers: List[dict] = []
        self.rate: Optional[int] = None
        self.ptt = False
        self.running = False
        self._ptt_off_time = 0.0
        self._sock: Optional[socket.socket] = None
        self._radio_addr: Optional[tuple] = None
        self._tx_seq = 0
        self._conn_count = 0
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._filter_sent: Optional[int] = None

    # -- receiver slots ------------------------------------------------------

    def add_receiver(self) -> int:
        if len(self.receivers) >= MAX_RECEIVERS:
            raise ValueError(f"HL2 supports at most {MAX_RECEIVERS} receivers")
        self.receivers.append({"freq": 7_030_000,
                               "q": queue.Queue(maxsize=128)})
        return len(self.receivers) - 1

    def set_rx_freq(self, idx: int, hz: int) -> None:
        self.receivers[idx]["freq"] = int(hz)
        if self.running:
            self._send_config()

    def set_rate(self, hz: int) -> None:
        if hz not in _SAMPLE_RATE_CODES:
            raise ValueError(
                f"HL2 supports {sorted(_SAMPLE_RATE_CODES)}, not {hz}")
        if self.rate is not None and self.rate != hz:
            raise ValueError("all hl2 bands on one radio share the sample "
                             f"rate; {self.rate} already set, got {hz}")
        self.rate = int(hz)

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            self._conn_count += 1
            if self._sock is not None:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
            sock.settimeout(2.0)
            sock.bind(("", 0))
            self._sock = sock
            self._radio_addr = self._discover()
            log.info("HL2: using radio at %s:%d (%d receiver(s))",
                     self._radio_addr[0], self._radio_addr[1],
                     len(self.receivers))

    def release(self) -> None:
        with self._lock:
            self._conn_count -= 1
            if self._conn_count > 0:
                return
            self.running = False
            if self._sock and self._radio_addr:
                try:
                    self._sock.sendto(
                        b"\xef\xfe\x04\x00" + b"\x00" * 60, self._radio_addr)
                except OSError:
                    pass
            if self._sock:
                self._sock.close()
                self._sock = None

    def _discover(self) -> tuple:
        assert self._sock is not None
        probe = b"\xef\xfe\x02" + b"\x00" * 60
        targets = [self.host] if self.host else ["255.255.255.255"]
        for attempt in range(5):
            for t in targets:
                self._sock.sendto(probe, (t, METIS_PORT))
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    data, addr = self._sock.recvfrom(2048)
                except socket.timeout:
                    break
                if (len(data) >= 3 and data[0:2] == b"\xef\xfe"
                        and data[2] in (0x02, 0x03)):
                    mac = ":".join(f"{b:02x}" for b in data[3:9])
                    gw = data[9] if len(data) > 9 else 0
                    log.info("HL2: discovered %s (MAC %s, gateware v%d)",
                             addr[0], mac, gw)
                    return addr
            log.debug("HL2: discovery attempt %d timed out", attempt + 1)
        where = self.host or "broadcast"
        raise ConnectionError(
            f"No Hermes-Lite 2 answered discovery ({where}:{METIS_PORT}). "
            "Check that the radio is powered, reachable, and not in use.")

    def ensure_started(self) -> None:
        with self._lock:
            if self.running:
                return
            if self._sock is None:
                raise RuntimeError("connect() first")
            self.running = True
            self._send_config()
            self._sock.sendto(b"\xef\xfe\x04\x01" + b"\x00" * 60,
                              self._radio_addr)
            self._thread = threading.Thread(target=self._rx_loop,
                                            name="hl2-rx", daemon=True)
            self._thread.start()

    # -- command & control ---------------------------------------------------

    def _cc_frame(self, addr: int, c1: int, c2: int, c3: int, c4: int) -> bytes:
        return _SYNC + bytes([(addr << 1) & 0xFE, c1, c2, c3, c4]) + b"\x00" * 504

    def _config_c0(self) -> tuple:
        rate = _SAMPLE_RATE_CODES[self.rate or 192000]
        c2 = 0x00
        if self.filter_board == "n2adr" and self.receivers:
            freqs = [r["freq"] for r in self.receivers]
            byte = n2adr_filter_byte(max(freqs), min(freqs))
            c2 = (byte << 1) & 0xFE     # user outputs live in C2[7:1]
            if byte != self._filter_sent:
                self._filter_sent = byte
                log.info("HL2: N2ADR filters 0x%02x for %.3f-%.3f MHz",
                         byte, min(freqs) / 1e6, max(freqs) / 1e6)
        # C4: duplex on (bit 2), receiver count in bits [5:3]
        n = max(1, len(self.receivers))
        return (rate, c2, 0x00, 0x04 | ((n - 1) << 3))

    def _config_frames(self) -> List[bytes]:
        frames = [self._cc_frame(0x00, *self._config_c0())]
        for i, r in enumerate(self.receivers):
            f = r["freq"].to_bytes(4, "big")
            frames.append(self._cc_frame(0x02 + i, f[0], f[1], f[2], f[3]))
        tx = self.receivers[0]["freq"].to_bytes(4, "big") if self.receivers \
            else b"\x00" * 4
        frames.append(self._cc_frame(0x01, tx[0], tx[1], tx[2], tx[3]))
        frames.append(self._cc_frame(0x0A, 0x00, 0x00, 0x00,
                                     0x40 | ((self.lna_gain_db + 12) & 0x3F)))
        return frames

    def _send_config(self) -> None:
        frames = self._config_frames()
        assert self._sock is not None and self._radio_addr is not None
        for i in range(0, len(frames), 2):
            pair = frames[i:i + 2]
            if len(pair) == 1:
                pair.append(self._cc_frame(0x00, *self._config_c0()))
            pkt = (b"\xef\xfe\x01\x02" + self._tx_seq.to_bytes(4, "big")
                   + pair[0] + pair[1])
            self._tx_seq += 1
            self._sock.sendto(pkt, self._radio_addr)

    # -- streaming -----------------------------------------------------------

    def _rx_loop(self) -> None:
        n_rx = len(self.receivers)
        last_cc = time.monotonic()
        expected_seq: Optional[int] = None
        try:
            while self.running:
                try:
                    pkt, _ = self._sock.recvfrom(2048)
                except socket.timeout:
                    if not self.running:
                        break
                    log.warning("HL2: no data for 2 s, resending start")
                    self._send_config()
                    self._sock.sendto(b"\xef\xfe\x04\x01" + b"\x00" * 60,
                                      self._radio_addr)
                    continue
                except OSError:
                    break
                per_rx, ptt = parse_iq_packet(pkt, n_rx)
                if per_rx is None:
                    continue
                seq = int.from_bytes(pkt[4:8], "big")
                if expected_seq is not None and seq != expected_seq:
                    log.warning("HL2: sequence gap (%d packets lost)",
                                (seq - expected_seq) & 0xFFFFFFFF)
                expected_seq = (seq + 1) & 0xFFFFFFFF

                now = time.monotonic()
                if ptt:
                    if not self.ptt:
                        log.info("HL2: PTT active, blanking decoder input")
                    self.ptt = True
                    self._ptt_off_time = now
                elif self.ptt and now - self._ptt_off_time > self.ptt_hang_s:
                    self.ptt = False
                    log.info("HL2: PTT released, resuming")

                for r, samples in zip(self.receivers, per_rx):
                    if self.ptt:
                        samples = np.zeros_like(samples)  # self-blanking (F8)
                    try:
                        r["q"].put_nowait(samples)
                    except queue.Full:
                        try:
                            r["q"].get_nowait()   # drop oldest, stay realtime
                            r["q"].put_nowait(samples)
                        except queue.Empty:
                            pass

                if now - last_cc > 0.1:
                    self._send_config()
                    last_cc = now
        except Exception:
            log.exception("HL2: receive loop died")
        finally:
            self.running = False


_radios: Dict[str, _HL2Radio] = {}


class HermesLite2Source(SDRSource):
    """One receiver (band) on a shared Hermes-Lite 2."""

    def __init__(self, host: Optional[str] = None, lna_gain_db: int = 19,
                 ptt_hang_s: float = 0.25, filter_board: str = "n2adr"):
        key = host or "*"
        radio = _radios.get(key)
        if radio is None:
            radio = _radios[key] = _HL2Radio(host)
            radio.lna_gain_db = lna_gain_db
            radio.filter_board = filter_board
            radio.ptt_hang_s = ptt_hang_s
        self._radio = radio
        self._idx = radio.add_receiver()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="hl2",
            max_sample_rate=384000,
            sample_rates=tuple(sorted(_SAMPLE_RATE_CODES)),
            max_channels=MAX_RECEIVERS,
            exclusive=True,
            supports_ptt_sense=True,
        )

    def connect(self) -> None:
        self._radio.connect()

    def disconnect(self) -> None:
        self._radio.release()

    def set_center_frequency(self, hz: int) -> None:
        self._radio.set_rx_freq(self._idx, hz)

    def set_sample_rate(self, hz: int) -> None:
        self._radio.set_rate(hz)

    @property
    def sample_rate(self) -> Optional[int]:
        return self._radio.rate

    def stream_iq(self) -> Iterator[np.ndarray]:
        self._radio.ensure_started()
        q = self._radio.receivers[self._idx]["q"]
        while True:
            try:
                yield q.get(timeout=1.0)
            except queue.Empty:
                if not self._radio.running:
                    return

    @property
    def ptt_active(self) -> bool:
        return self._radio.ptt
