"""Tile narrow IQ slices into one wide virtual passband (requirement 4.2).

Each KiwiSDR connection delivers a ~12 kHz IQ slice. N adjacent slices are
combined into a virtual passband of N x slice_rate: every slice is upsampled
by N (zero-stuff + windowed-sinc low-pass), mixed to its offset within the
virtual band, and summed.

Two properties make this simpler than generic stream stitching:
  - all slices share the receiver's ADC clock, so their rates are locked and
    per-slice sample counters stay aligned indefinitely;
  - a CW signal is a few tens of Hz wide and therefore lives entirely inside
    one slice, so cross-slice alignment only matters at the millisecond
    level (per-signal decoders are independent), not per-sample.

Slices that die (shared Kiwi, connection lost) are excluded and the rest of
the band keeps decoding -- graceful degradation per the requirements.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np

log = logging.getLogger(__name__)


def _lowpass(fs: float, cutoff_hz: float, width_hz: float) -> np.ndarray:
    """Windowed-sinc FIR (Hamming), unity DC gain."""
    numtaps = int(np.ceil(3.3 * fs / width_hz)) | 1
    m = np.arange(numtaps) - (numtaps - 1) / 2
    h = 2 * cutoff_hz / fs * np.sinc(2 * cutoff_hz / fs * m)
    h *= np.hamming(numtaps)
    return (h / h.sum()).astype(np.float32)


class _SlicePath:
    """Streaming upsampler + mixer for one slice."""

    def __init__(self, up: int, taps: np.ndarray, offset_hz: float, fs_v: float):
        self.up = up
        self.taps = taps
        self.offset_hz = offset_hz
        self.fs_v = fs_v
        self._state = np.zeros(len(taps) - 1, dtype=np.complex64)
        self.produced = 0                     # virtual samples emitted
        self.chunks: List[np.ndarray] = []    # awaiting alignment/summing
        self.buffered = 0
        self.dead = False

    def push(self, samples: np.ndarray) -> None:
        if self.up == 1:
            y = samples.astype(np.complex64)
        else:
            stuffed = np.zeros(len(samples) * self.up, dtype=np.complex64)
            stuffed[::self.up] = samples * self.up  # gain restores amplitude
            joined = np.concatenate([self._state, stuffed])
            y = np.convolve(joined, self.taps, mode="valid").astype(np.complex64)
            self._state = joined[len(joined) - (len(self.taps) - 1):]
        if self.offset_hz:
            idx = self.produced + np.arange(len(y))
            y = y * np.exp(2j * np.pi * self.offset_hz * idx / self.fs_v
                           ).astype(np.complex64)
        self.produced += len(y)
        self.chunks.append(y)
        self.buffered += len(y)

    def take(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.complex64)
        pos = 0
        while pos < n and self.chunks:
            chunk = self.chunks[0]
            take = min(n - pos, len(chunk))
            out[pos:pos + take] = chunk[:take]
            if take == len(chunk):
                self.chunks.pop(0)
            else:
                self.chunks[0] = chunk[take:]
            pos += take
        self.buffered -= pos
        return out


class SliceStitcher:
    def __init__(self, n_slices: int, slice_rate: int,
                 transition_hz: float = 1000.0):
        self.n_slices = n_slices
        self.slice_rate = slice_rate
        self.sample_rate = n_slices * slice_rate
        # slice k sits at (k - (n-1)/2) * slice_rate from the virtual center
        self.offsets_hz = [(k - (n_slices - 1) / 2) * slice_rate
                           for k in range(n_slices)]
        if n_slices > 1:
            # cutoff at the slice edge; the Kiwi's own passband filter has
            # already rolled off before the transition band's stop edge, so
            # upsampling images from neighbor territory stay suppressed
            taps = _lowpass(self.sample_rate, slice_rate / 2, transition_hz)
        else:
            taps = np.ones(1, dtype=np.float32)
        self._paths = [_SlicePath(n_slices, taps, self.offsets_hz[k],
                                  self.sample_rate)
                       for k in range(n_slices)]

    def push(self, slice_index: int, samples: np.ndarray) -> None:
        path = self._paths[slice_index]
        if not path.dead:
            path.push(samples)

    def mark_dead(self, slice_index: int) -> None:
        path = self._paths[slice_index]
        if not path.dead:
            path.dead = True
            path.chunks.clear()
            path.buffered = 0
            log.warning("Stitcher: slice %d dropped; continuing with %d slice(s)",
                        slice_index, sum(not p.dead for p in self._paths))

    def revive(self, slice_index: int) -> None:
        """Re-admit a previously dead slice (reconnected stream). Its virtual
        sample counter is aligned to the live paths so buffers stay
        comparable; sub-sample phase does not matter for CW decoding."""
        path = self._paths[slice_index]
        if not path.dead:
            return
        ref = max((p.produced for p in self._paths if not p.dead), default=0)
        path.dead = False
        path._state = np.zeros(len(path.taps) - 1, dtype=np.complex64)
        path.chunks = []
        path.buffered = 0
        path.produced = ref
        log.info("Stitcher: slice %d rejoined; %d slice(s) live",
                 slice_index, sum(not p.dead for p in self._paths))

    @property
    def alive_count(self) -> int:
        return sum(not p.dead for p in self._paths)

    @property
    def alive_slices(self) -> List[int]:
        return [k for k, p in enumerate(self._paths) if not p.dead]

    def pull(self) -> Optional[np.ndarray]:
        """Return the next aligned block summed across live slices, or None
        until every live slice has delivered samples for that span."""
        live = [p for p in self._paths if not p.dead]
        if not live:
            return None
        n = min(p.buffered for p in live)
        if n == 0:
            return None
        out = np.zeros(n, dtype=np.complex64)
        for p in live:
            out += p.take(n)
        return out
