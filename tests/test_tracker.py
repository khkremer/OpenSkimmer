"""Tracker channel-lifecycle tests: persistence, retirement, splatter guard.

These drive SignalTracker directly with synthetic power frames (no IQ), so
each lifecycle rule can be pinned down in isolation.
"""

import numpy as np
import pytest

from openskimmer.dsp.tracker import SignalTracker


N_BINS = 512
FRAME_DT = 0.005


class _NullSink:
    def add_char(self, ch):
        pass

    def add_word_break(self):
        pass


def make_tracker(**kw):
    return SignalTracker(n_bins=N_BINS, frame_dt=FRAME_DT,
                         sink_factory=lambda ch: _NullSink(), **kw)


def run_frames(tracker, n, hot_bins=(), rng=None, hot_power=100.0):
    """Feed n frames of flat noise (power 1.0) with optional hot bins."""
    for _ in range(n):
        frame = np.ones(N_BINS, dtype=np.float32)
        if rng is not None:
            frame *= rng.uniform(0.5, 1.5, N_BINS).astype(np.float32)
        for b in hot_bins:
            frame[b] = hot_power
        tracker.process_frame(frame)


def test_one_scan_flare_does_not_create_channel():
    tr = make_tracker()
    run_frames(tr, tr.detect_interval * 3)          # settle the baseline
    run_frames(tr, tr.detect_interval, hot_bins=(100,))  # one hot scan
    run_frames(tr, tr.detect_interval * 3)          # flare gone
    assert not tr.channels


def test_persistent_peak_creates_channel():
    tr = make_tracker()
    run_frames(tr, tr.detect_interval * 3)
    run_frames(tr, tr.detect_interval * 3, hot_bins=(100,))
    assert len(tr.channels) == 1
    assert next(iter(tr.channels.values())).bin_index == 100


def test_steady_carrier_retires_on_idle():
    # A constant (never-keyed) carrier produces no marks, so the idle
    # timeout must retire it even though it stays above threshold.
    tr = make_tracker(channel_timeout_s=2.0)
    run_frames(tr, tr.detect_interval * 3)
    run_frames(tr, tr.detect_interval * 3, hot_bins=(100,))
    assert len(tr.channels) == 1
    run_frames(tr, round(3.0 / FRAME_DT), hot_bins=(100,))
    assert not tr.channels


def test_noise_blip_channel_retires_as_implausible():
    # Blips of 2 frames every 20 decode as an endless "E E E ..." stream at
    # the WPM clamp; the plausibility gate must retire and blacklist it.
    tr = make_tracker(implausible_after_s=3.0, blacklist_s=5.0)
    run_frames(tr, tr.detect_interval * 3)

    def blip_frames(n):
        for i in range(n):
            frame = np.ones(N_BINS, dtype=np.float32)
            if i % 20 < 2:
                frame[100] = 100.0
            tr.process_frame(frame)

    blip_frames(tr.detect_interval * 6)
    assert len(tr.channels) == 1
    blip_frames(round(5.0 / FRAME_DT))
    assert not tr.channels
    assert 100 in tr._blacklist
    # while blacklisted, the same energy must not seed a new channel
    blip_frames(tr.detect_interval * 6)
    assert not tr.channels


def test_strong_channel_widens_exclusion_zone():
    tr = make_tracker()
    run_frames(tr, tr.detect_interval * 3)
    # keyed strong carrier at bin 100: alternate on/off so marks complete
    # and the decoder learns a high SNR
    for i in range(tr.detect_interval * 12):
        frame = np.ones(N_BINS, dtype=np.float32)
        if (i // 10) % 2 == 0:
            frame[100] = 10000.0
        tr.process_frame(frame)
    assert len(tr.channels) == 1
    main = next(iter(tr.channels.values()))
    assert main.decoder.snr_db > 20
    margin = tr._occupied_margin(main)
    assert margin > tr.min_sep

    # a moderate peak inside the widened margin must be ignored ...
    inside = 100 + margin
    run_frames(tr, tr.detect_interval * 4, hot_bins=(100, inside),
               hot_power=100.0)
    assert len(tr.channels) == 1
    # ... while the same peak well outside it gets a channel
    outside = 100 + margin + tr.min_sep + 2
    run_frames(tr, tr.detect_interval * 4, hot_bins=(100, outside),
               hot_power=100.0)
    assert {ch.bin_index for ch in tr.channels.values()} == {100, outside}


def test_decoder_fed_three_bin_sums():
    # process_frame vectorizes the per-channel envelope; each decoder must
    # still receive exactly power[b-1:b+2].sum() and the same sum of the
    # noise baseline, including channels at the lowest and highest bins a
    # channel can occupy (2 and n-3).
    fed = []

    class Recorder:
        def __init__(self, *a, **kw):
            self.snr_db = 30.0
            self._frame_idx = 0
            self.last_activity_frame = 0

        def process(self, env, noise):
            self._frame_idx += 1
            self.last_activity_frame = self._frame_idx
            fed.append((env, noise))

    tr = make_tracker(decoder_cls=Recorder)
    rng = np.random.default_rng(1)
    run_frames(tr, tr.detect_interval * 4, hot_bins=(2, 200, N_BINS - 3),
               rng=rng)
    assert {ch.bin_index for ch in tr.channels.values()} == {2, 200, N_BINS - 3}

    fed.clear()
    frame = rng.uniform(0.5, 1.5, N_BINS).astype(np.float32)
    baseline = tr._baseline.copy()      # before this frame can update it
    tr.process_frame(frame)
    want = sorted((float(frame[b - 1:b + 2].sum()),
                   float(baseline[b - 1:b + 2].sum()))
                  for b in (2, 200, N_BINS - 3))
    assert sorted(fed) == want
