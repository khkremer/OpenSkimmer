"""Synthetic multi-signal CW source.

Generates a passband of independently keyed CW carriers in Gaussian noise.
Used for end-to-end tests and for demoing the pipeline without hardware
(`openskimmer --source synth`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

import numpy as np

from ..dsp.morse import text_to_keying
from .base import Capabilities, SDRSource


@dataclass
class SynthSignal:
    offset_hz: float          # relative to passband center
    wpm: float
    snr_db: float             # carrier power over noise power in 500 Hz
    text: str                 # message, keyed in a loop
    start_s: float = 0.0      # delay before first transmission
    repeat_gap_s: float = 3.0


class SynthSource(SDRSource):
    def __init__(self, signals: Sequence[SynthSignal],
                 sample_rate: int = 48000,
                 center_frequency: int = 14_030_000,
                 duration_s: Optional[float] = None,
                 realtime: bool = False,
                 seed: Optional[int] = None,
                 block_size: int = 4096):
        self._signals = list(signals)
        self._sample_rate = sample_rate
        self._center_frequency = center_frequency
        self._duration_s = duration_s
        self._realtime = realtime
        self._rng = np.random.default_rng(seed)
        self._block = block_size
        self._running = False

    def capabilities(self) -> Capabilities:
        return Capabilities(name="synth", max_sample_rate=384000,
                            sample_rates=(48000, 96000, 192000, 384000))

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        self._running = False

    def set_center_frequency(self, hz: int) -> None:
        self._center_frequency = int(hz)

    def set_sample_rate(self, hz: int) -> None:
        self._sample_rate = int(hz)

    def _keying_envelope(self, sig: SynthSignal, n_total: int) -> np.ndarray:
        """Full on/off envelope for one signal, with 4 ms raised-cosine edges."""
        fs = self._sample_rate
        env = np.zeros(n_total, dtype=np.float32)
        unit_s = 1.2 / sig.wpm
        pos = sig.start_s
        keying = text_to_keying(sig.text)  # [(on?, units), ...]
        while pos < n_total / fs:
            for on, units in keying:
                a = int(pos * fs)
                b = int((pos + units * unit_s) * fs)
                if on and a < n_total:
                    env[a:min(b, n_total)] = 1.0
                pos += units * unit_s
            pos += sig.repeat_gap_s
        edge = max(int(0.004 * fs), 3)
        win = np.hanning(2 * edge).astype(np.float32)
        win /= win.sum()
        return np.convolve(env, win, mode="same")

    def stream_iq(self) -> Iterator[np.ndarray]:
        fs = self._sample_rate
        dur = self._duration_s if self._duration_s is not None else 3600.0
        n_total = int(dur * fs)
        noise_sigma = 1e-3  # arbitrary noise floor amplitude
        envelopes = []
        for sig in self._signals:
            # SNR defined against noise power in a 500 Hz bandwidth
            noise_pwr_500 = noise_sigma ** 2 * (500.0 / fs)
            amp = np.sqrt(10 ** (sig.snr_db / 10.0) * noise_pwr_500)
            envelopes.append((sig, amp, self._keying_envelope(sig, n_total)))

        self._running = True
        t0 = time.monotonic()
        for start in range(0, n_total, self._block):
            if not self._running:
                return
            n = min(self._block, n_total - start)
            t = (start + np.arange(n)) / fs
            block = (self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n))
            block = block.astype(np.complex64) * np.float32(noise_sigma / np.sqrt(2))
            for sig, amp, env in envelopes:
                ph = 2 * np.pi * sig.offset_hz * t
                block += (amp * env[start:start + n]
                          * np.exp(1j * ph)).astype(np.complex64)
            if self._realtime:
                target = t0 + (start + n) / fs
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            yield block
