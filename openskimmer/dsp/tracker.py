"""Signal tracker: finds CW carriers in the spectrum and owns one decoder each.

Noise estimation uses a sliding median across frequency of the time-averaged
spectrum: as long as signals occupy a minority of any ~3 kHz stretch, the
median tracks the noise floor (including passband slope) and is immune to the
keyed carriers themselves. Detection then looks for local maxima that stand
above that baseline. Every ``detect_interval`` frames the tracker scans for
new peaks and retires channels that have gone quiet.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .decoder import CWDecoder

log = logging.getLogger(__name__)


class Channel:
    def __init__(self, chan_id: int, bin_index: int, decoder: CWDecoder):
        self.id = chan_id
        self.bin_index = bin_index
        self.bin_frac = 0.0     # sub-bin frequency refinement
        self.decoder = decoder
        self.sink = None        # opaque per-channel object owned by the pipeline

    @property
    def bin_position(self) -> float:
        return self.bin_index + self.bin_frac


class SignalTracker:
    def __init__(self, n_bins: int, frame_dt: float,
                 sink_factory: Callable[["Channel"], object],
                 detect_snr: float = 4.0,        # power ratio (~6 dB)
                 detect_interval_s: float = 0.25,
                 min_separation_bins: int = 2,
                 channel_timeout_s: float = 20.0,
                 max_channels: int = 200,
                 baseline_window_bins: int = 65,
                 implausible_after_s: float = 15.0,
                 blacklist_s: float = 30.0,
                 decoder_cls: type = CWDecoder):
        self.n_bins = n_bins
        self.frame_dt = frame_dt
        self.sink_factory = sink_factory
        self.detect_snr = detect_snr
        self.detect_interval = max(1, round(detect_interval_s / frame_dt))
        self.min_sep = min_separation_bins
        self.timeout_frames = round(channel_timeout_s / frame_dt)
        self.max_channels = max_channels
        self._saturated_frames = 0
        self.baseline_window = baseline_window_bins | 1  # force odd
        self.implausible_frames = round(implausible_after_s / frame_dt)
        self.blacklist_frames = round(blacklist_s / frame_dt)
        self.decoder_cls = decoder_cls

        # sampled bin indices for the strided baseline median (see below)
        stride = max(1, baseline_window_bins // 8)
        self._all_bins = np.arange(n_bins)
        idx = np.arange(0, n_bins, stride)
        if idx[-1] != n_bins - 1:
            idx = np.append(idx, n_bins - 1)
        self._baseline_idx = idx

        self.channels: Dict[int, Channel] = {}
        self._next_id = 0
        self._baseline: Optional[np.ndarray] = None
        self._baseline3: List[float] = []   # 3-bin sums of _baseline
        self._accum = np.zeros(n_bins, dtype=np.float64)
        self._accum_count = 0
        self._frame_total = 0
        self._pending: set = set()          # candidate bins from last scan
        self._blacklist: Dict[int, int] = {}  # bin -> frame it expires
        self.coverage_mask: Optional[np.ndarray] = None  # True = usable bin

    def process_frame(self, power: np.ndarray) -> None:
        self._frame_total += 1
        if self._baseline is not None and self.channels:
            # Each channel reads the power summed over its bin and both
            # neighbours. Form that 3-bin sum for the whole spectrum in one
            # vector op and index it per channel, instead of two tiny numpy
            # slice+sum calls per channel per frame: that per-call overhead
            # was the single largest cost on slow CPUs (2.3x faster end to
            # end on an Atom x5-Z8350, decoded text byte-identical). p3[b-1]
            # == power[b-1:b+2].sum(); channels only exist at 2 <= b < n-2.
            p3 = (power[:-2] + power[1:-1] + power[2:]).tolist()
            n3 = self._baseline3
            for ch in self.channels.values():
                i = ch.bin_index - 1
                ch.decoder.process(p3[i], n3[i])

        self._accum += power
        self._accum_count += 1
        if self._accum_count >= self.detect_interval:
            avg = self._accum / self._accum_count
            self._update_baseline(avg)
            self._detect(avg)
            self._refine_and_retire(avg)
            self._accum[:] = 0.0
            self._accum_count = 0

    def _update_baseline(self, avg: np.ndarray) -> None:
        pad = self.baseline_window // 2
        padded = np.pad(avg, pad, mode="reflect")
        windows = sliding_window_view(padded, self.baseline_window)
        # The noise floor is smooth over the ~65-bin window, so evaluate the
        # (expensive) sliding median only every BASELINE_STRIDE bins from the
        # full-resolution windows -- which still reject narrow carriers -- and
        # linearly interpolate between. ~stride-fold fewer partial sorts, the
        # dominant per-scan cost, with no measurable detection change.
        idx = self._baseline_idx
        med = np.median(windows[idx], axis=1)
        baseline = np.interp(self._all_bins, idx, med)
        np.maximum(baseline, 1e-30, out=baseline)
        if self._baseline is None:
            self._baseline = baseline
        else:
            # light temporal smoothing so keying doesn't modulate the floor
            self._baseline = 0.7 * self._baseline + 0.3 * baseline
        b = self._baseline
        self._baseline3 = (b[:-2] + b[1:-1] + b[2:]).tolist()

    # -- detection -----------------------------------------------------------

    def _occupied_margin(self, ch: Channel) -> int:
        """Strong carriers splatter keying sidebands well beyond min_sep;
        widen their exclusion zone ~1 bin per 6 dB of SNR so the sidebands
        can't seed satellite channels."""
        return self.min_sep + max(0, int(ch.decoder.snr_db / 6.0))

    def _detect(self, avg: np.ndarray) -> None:
        if len(self.channels) >= self.max_channels:
            # Saturation is silent otherwise: arriving signals are simply
            # never given a channel, so they are never decoded, and every
            # symptom appears downstream in the spotting rules. Measured on
            # a CWT hour, the default cap of 100 bound for 61 % of the
            # session and cost 73 real stations that another skimmer heard.
            self._saturated_frames += 1
            if self._saturated_frames % 20000 == 1:
                log.warning("signal tracker is FULL (%d channels): new "
                            "signals are being ignored. Raise "
                            "--max-channels if CPU allows.",
                            self.max_channels)
            return
        ratio = avg / self._baseline
        if self.coverage_mask is not None:
            ratio = np.where(self.coverage_mask, ratio, 0.0)
        if self._blacklist:
            self._blacklist = {b: t for b, t in self._blacklist.items()
                               if t > self._frame_total}
        occupied = np.zeros(self.n_bins, dtype=bool)
        for ch in self.channels.values():
            m = self._occupied_margin(ch)
            lo = max(ch.bin_index - m, 0)
            occupied[lo:ch.bin_index + m + 1] = True
        for b in self._blacklist:
            occupied[max(b - 1, 0):b + 2] = True

        candidates = np.flatnonzero(ratio > self.detect_snr)
        pending = set()
        # strongest first, so a strong carrier claims its bins before sidelobes
        for b in candidates[np.argsort(-ratio[candidates])]:
            b = int(b)
            if b < 2 or b >= self.n_bins - 2 or occupied[b]:
                continue
            if ratio[b] < ratio[b - 1] or ratio[b] < ratio[b + 1]:
                continue
            # a peak must persist across two consecutive scans before it
            # earns a channel, so a single noise flare can't seed one
            if not self._pending & {b - 1, b, b + 1}:
                pending.add(b)
                continue
            self._create_channel(b, avg)
            lo = max(b - self.min_sep, 0)
            occupied[lo:b + self.min_sep + 1] = True
            if len(self.channels) >= self.max_channels:
                break
        self._pending = pending

    def _create_channel(self, b: int, avg: np.ndarray) -> None:
        chan_id = self._next_id
        self._next_id += 1
        # decoder callbacks are bound once the sink exists
        decoder = self.decoder_cls(self.frame_dt, on_char=lambda c: None,
                                   on_word_break=lambda: None)
        ch = Channel(chan_id, b, decoder)
        ch.bin_frac = self._parabolic(avg, b)
        ch.sink = self.sink_factory(ch)
        decoder.on_char = ch.sink.add_char
        decoder.on_word_break = ch.sink.add_word_break
        decoder.on_gap = getattr(ch.sink, "add_gap", None)
        self.channels[chan_id] = ch

    @staticmethod
    def _parabolic(avg: np.ndarray, b: int) -> float:
        a, c = avg[b - 1], avg[b + 1]
        denom = a - 2 * avg[b] + c
        if denom >= 0:
            return 0.0
        return float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5))

    # -- maintenance ---------------------------------------------------------

    @staticmethod
    def _implausible(d: CWDecoder) -> bool:
        """Noise channels decode almost nothing but E/T (random blips), and
        their dit estimate collapses onto the clamp (~80 WPM). Real CW text
        never sustains >90% E/T over a dozen characters."""
        if d.chars_total >= 12 and d.et_fraction > 0.9:
            return True
        return (d.chars_total >= 6 and d.et_fraction > 0.8
                and d.dit_ms is not None and d.dit_ms <= 16.0)

    def _refine_and_retire(self, avg: np.ndarray) -> None:
        dead: List[int] = []
        for cid, ch in self.channels.items():
            idle = ch.decoder._frame_idx - ch.decoder.last_activity_frame
            if idle > self.timeout_frames:
                dead.append(cid)
                b = ch.bin_index
                if avg[b] > self.detect_snr * self._baseline[b]:
                    # still hot but never keying: a birdie/constant carrier;
                    # blacklist it or it would be re-detected immediately
                    self._blacklist[b] = (self._frame_total
                                          + self.blacklist_frames)
                continue
            if (self.coverage_mask is not None
                    and not self.coverage_mask[ch.bin_index]):
                dead.append(cid)  # the slice backing this region went away
                continue
            if (ch.decoder._frame_idx > self.implausible_frames
                    and self._implausible(ch.decoder)):
                dead.append(cid)
                # don't immediately re-detect the same junk energy
                self._blacklist[ch.bin_index] = (self._frame_total
                                                 + self.blacklist_frames)
                continue
            # refine sub-bin frequency only while the carrier is clearly on,
            # otherwise noise in the neighbor bins would jitter the estimate
            b = ch.bin_index
            if avg[b] > 4.0 * self._baseline[b]:
                ch.bin_frac = self._parabolic(avg, b)
        for cid in dead:
            del self.channels[cid]
