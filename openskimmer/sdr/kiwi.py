"""KiwiSDR backend: WebSocket IQ slices tiled into a wide virtual passband.

A single KiwiSDR connection is limited to a ~12 kHz IQ slice, so this backend
opens one connection per slice and feeds them through SliceStitcher
(requirement 4.2). Access may be shared (public Kiwi): slices that cannot
connect or die mid-run are dropped and skimming continues on the remainder.

Requires the ``websocket-client`` package.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Iterator, List, Optional

import numpy as np

from .base import Capabilities, SDRSource
from .kiwi_protocol import (client_setup_commands, client_stream_commands,
                            parse_msg, parse_snd_iq)
from .stitcher import SliceStitcher

log = logging.getLogger(__name__)

SLICE_RATE = 12000          # Hz of IQ per Kiwi connection (standard firmware)
PASSBAND_HZ = 11800         # requested IQ passband (slightly inside the rate)
CONNECT_TIMEOUT_S = 20.0    # no samples within this -> slice is dead
KEEPALIVE_S = 5.0
RECONNECT_DELAY_S = 30.0    # retry dead slices (kiwi channels free up)


class _KiwiSlice(threading.Thread):
    """One WebSocket connection streaming one IQ slice."""

    def __init__(self, index: int, host: str, port: int, password: str,
                 freq_hz: float, out: "queue.Queue", ident: str):
        super().__init__(name=f"kiwi-slice-{index}", daemon=True)
        self.index = index
        self.host = host
        self.port = port
        self.password = password
        self.freq_hz = freq_hz
        self.out = out
        self.user_ident = ident  # Thread.ident is taken (thread id)
        self._stop = threading.Event()
        self._ws = None

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def retune(self, freq_hz: float) -> None:
        self.freq_hz = freq_hz
        ws = self._ws
        if ws is not None:
            try:
                ws.send(f"SET mod=iq low_cut={-PASSBAND_HZ / 2:.0f} "
                        f"high_cut={PASSBAND_HZ / 2:.0f} "
                        f"freq={freq_hz / 1000.0:.3f}")
            except Exception:
                pass

    # -- connection ----------------------------------------------------------

    def _connect(self):
        import websocket
        # the timestamp doubles as the kiwi's client-session id, so it must
        # be unique across our simultaneous slice connections
        ts = int(time.time() * 1000) + self.index
        last_err: Optional[Exception] = None
        for path in (f"/kiwi/{ts}/SND", f"/{ts}/SND"):
            try:
                ws = websocket.create_connection(
                    f"ws://{self.host}:{self.port}{path}",
                    timeout=10, enable_multithread=True)
                return ws
            except Exception as e:  # try the older URL form
                last_err = e
        raise ConnectionError(
            f"KiwiSDR slice {self.index}: cannot connect to "
            f"{self.host}:{self.port} ({last_err})")

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            if not self._stop.is_set():
                log.warning("KiwiSDR slice %d died: %s", self.index, e)
        finally:
            self.out.put(("dead", self.index, None))
            ws = self._ws
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def _run(self) -> None:
        import websocket
        ws = self._connect()
        self._ws = ws
        for cmd in client_setup_commands(self.password, self.user_ident):
            ws.send(cmd)

        configured = False
        expected_seq: Optional[int] = None
        started = time.monotonic()
        last_keepalive = started
        got_samples = False
        bad_snd = 0

        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_keepalive > KEEPALIVE_S:
                ws.send("SET keepalive")
                last_keepalive = now
            if not got_samples and now - started > CONNECT_TIMEOUT_S:
                raise ConnectionError("no IQ samples (busy or unsupported?)")
            try:
                opcode, frame = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                continue
            if opcode in (8,):  # close
                raise ConnectionError("server closed connection")
            if not isinstance(frame, (bytes, bytearray)) or len(frame) < 3:
                continue
            tag, body = bytes(frame[:3]), bytes(frame[3:])

            if tag == b"MSG":
                info = parse_msg(body)
                if info.get("badp") == "1":
                    raise ConnectionError("password rejected")
                if "too_busy" in info:
                    raise ConnectionError(
                        f"kiwi busy ({info.get('too_busy')} rx in use)")
                if "camp" in info:
                    # a full kiwi parked us on another user's channel; that
                    # stream is their audio, never our IQ slice
                    raise ConnectionError("kiwi full: connection was camped")
                if "audio_rate" in info and not configured:
                    rate = int(float(info["audio_rate"]))
                    if rate != SLICE_RATE:
                        log.warning("KiwiSDR slice %d: audio_rate %d != %d",
                                    self.index, rate, SLICE_RATE)
                    for cmd in client_stream_commands(rate, self.freq_hz,
                                                      PASSBAND_HZ):
                        ws.send(cmd)
                    configured = True
            elif tag == b"SND":
                seq, samples = parse_snd_iq(body)
                if samples is None:
                    # compressed / mono / malformed -- not our IQ stream
                    bad_snd += 1
                    if bad_snd > 200:  # ~10 s of continuous non-IQ frames
                        raise ConnectionError(
                            "stream is not raw IQ (compressed or camped)")
                    continue
                bad_snd = 0
                if not configured:
                    continue  # the kiwi's default-mode audio, not our slice
                if expected_seq is not None and seq != expected_seq:
                    lost = (seq - expected_seq) & 0xFFFFFFFF
                    if 0 < lost < 64:
                        # keep the sample counter roughly aligned
                        self.out.put(("iq", self.index,
                                      np.zeros(lost * len(samples),
                                               dtype=np.complex64)))
                        log.debug("KiwiSDR slice %d: %d frames lost",
                                  self.index, lost)
                expected_seq = (seq + 1) & 0xFFFFFFFF
                got_samples = True
                self.out.put(("iq", self.index, samples))


class KiwiSDRSource(SDRSource):
    def __init__(self, host: str, port: int = 8073, password: str = "",
                 ident: str = "openskimmer"):
        if not host:
            raise ValueError("KiwiSDR backend needs a host")
        self._host = host
        self._port = port
        self._password = password
        self._ident = ident
        self._center_frequency = 7_030_000
        self._n_slices = 4
        self._slices: List[_KiwiSlice] = []
        self._stitcher: Optional[SliceStitcher] = None
        self._running = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="kiwi",
            max_sample_rate=8 * SLICE_RATE,
            sample_rates=tuple(n * SLICE_RATE for n in range(1, 9)),
            max_channels=8,
            exclusive=False,   # public Kiwis are shared; degrade gracefully
        )

    def connect(self) -> None:
        try:
            import websocket  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "KiwiSDR backend requires the 'websocket-client' package "
                "(pip install websocket-client)") from e

    def disconnect(self) -> None:
        self._running = False
        for s in self._slices:
            s.stop()

    def set_center_frequency(self, hz: int) -> None:
        self._center_frequency = int(hz)
        if self._slices:
            offsets = self._offsets()
            for s in self._slices:
                s.retune(self._center_frequency + offsets[s.index])

    def set_sample_rate(self, hz: int) -> None:
        """Total virtual passband; rounded to a whole number of slices."""
        self._n_slices = min(max(1, round(hz / SLICE_RATE)), 8)

    @property
    def sample_rate(self) -> int:
        return self._n_slices * SLICE_RATE

    def _offsets(self) -> List[float]:
        n = self._n_slices
        return [(k - (n - 1) / 2) * SLICE_RATE for k in range(n)]

    @property
    def coverage_hz(self):
        """Only regions backed by a live slice; the decoder must ignore the
        rest (dead-slice gaps contain only filter-image leakage)."""
        if self._stitcher is None:
            return None
        offsets = self._offsets()
        return [(offsets[k] - PASSBAND_HZ / 2, offsets[k] + PASSBAND_HZ / 2)
                for k in self._stitcher.alive_slices]

    def stream_iq(self) -> Iterator[np.ndarray]:
        stitcher = SliceStitcher(self._n_slices, SLICE_RATE)
        self._stitcher = stitcher
        out: "queue.Queue" = queue.Queue(maxsize=1024)
        offsets = self._offsets()
        self._slices = [
            _KiwiSlice(k, self._host, self._port, self._password,
                       self._center_frequency + offsets[k], out, self._ident)
            for k in range(self._n_slices)
        ]
        log.info("KiwiSDR: opening %d slice(s) on %s:%d covering %.1f-%.1f kHz",
                 self._n_slices, self._host, self._port,
                 (self._center_frequency + offsets[0] - SLICE_RATE / 2) / 1000,
                 (self._center_frequency + offsets[-1] + SLICE_RATE / 2) / 1000)
        for s in self._slices:
            s.start()
        self._running = True
        ever_streamed = False
        retry_at = {}  # slice index -> monotonic time of next attempt
        try:
            while self._running:
                now = time.monotonic()
                for idx in [i for i, t in retry_at.items() if now >= t]:
                    del retry_at[idx]
                    log.info("KiwiSDR: retrying slice %d", idx)
                    s = _KiwiSlice(idx, self._host, self._port, self._password,
                                   self._center_frequency + offsets[idx],
                                   out, self._ident)
                    self._slices[idx] = s
                    s.start()
                try:
                    kind, idx, samples = out.get(timeout=5.0)
                except queue.Empty:
                    continue
                if kind == "dead":
                    stitcher.mark_dead(idx)
                    if stitcher.alive_count == 0 and not ever_streamed:
                        # nothing ever worked: bad host/password, not a
                        # transient -- fail fast instead of retrying forever
                        raise ConnectionError(
                            "KiwiSDR: no slice delivered IQ "
                            "(kiwi busy, offline, or wrong password?)")
                    if self._running:
                        retry_at[idx] = time.monotonic() + RECONNECT_DELAY_S
                    continue
                ever_streamed = True
                stitcher.revive(idx)  # no-op unless the slice was dead
                stitcher.push(idx, samples)
                block = stitcher.pull()
                if block is not None and len(block):
                    yield block
        finally:
            self._running = False
            for s in self._slices:
                s.stop()
