"""End-to-end: three 12 kHz Kiwi-style slices, each carrying a CW station,
stitched into a 36 kHz virtual passband and decoded to spots at the correct
absolute frequencies."""

import numpy as np

from openskimmer.pipeline import Pipeline
from openskimmer.sdr.stitcher import SliceStitcher
from openskimmer.sdr.synth import SynthSignal, SynthSource

SLICE_RATE = 12000
CENTER = 14_040_000  # virtual passband center


def _slice_sources(duration_s):
    """One SynthSource per slice; signal offsets are slice-local."""
    specs = [
        [SynthSignal(2000, 25, 20, "CQ TEST DE W1AW W1AW")],       # slice 0
        [SynthSignal(-3000, 30, 18, "CQ DE K1ABC K1ABC")],         # slice 1
        [SynthSignal(4000, 22, 15, "DL1XYZ DL1XYZ TEST")],         # slice 2
    ]
    return [SynthSource(sig, sample_rate=SLICE_RATE, duration_s=duration_s,
                        realtime=False, seed=100 + i, block_size=1024)
            for i, sig in enumerate(specs)]


def test_stitched_slices_decode_to_absolute_frequencies():
    stitcher = SliceStitcher(n_slices=3, slice_rate=SLICE_RATE)
    spots = []
    pipe = Pipeline(sample_rate=stitcher.sample_rate, center_frequency=CENTER,
                    on_spot=spots.append, min_snr_db=3)
    streams = list(enumerate(src.stream_iq()
                             for src in _slice_sources(duration_s=25)))
    while streams:
        for pair in list(streams):
            idx, stream = pair
            block = next(stream, None)
            if block is None:
                streams.remove(pair)
                continue
            stitcher.push(idx, block)
        out = stitcher.pull()
        if out is not None:
            pipe.process(out)

    by_call = {s.call: s for s in spots}
    # slice 0 center = CENTER - 12 kHz; station at +2 kHz -> 14030.0 kHz
    assert "W1AW" in by_call
    assert abs(by_call["W1AW"].freq_hz - 14_030_000) < 60
    # slice 1 center = CENTER; station at -3 kHz -> 14037.0 kHz
    assert "K1ABC" in by_call
    assert abs(by_call["K1ABC"].freq_hz - 14_037_000) < 60
    # slice 2 center = CENTER + 12 kHz; station at +4 kHz -> 14056.0 kHz
    assert "DL1XYZ" in by_call
    assert abs(by_call["DL1XYZ"].freq_hz - 14_056_000) < 60


def _coverage(stitcher):
    return [(stitcher.offsets_hz[k] - 5900, stitcher.offsets_hz[k] + 5900)
            for k in stitcher.alive_slices]


def test_degraded_band_still_decodes():
    """If one slice is dead from the start, the others still produce spots."""
    stitcher = SliceStitcher(n_slices=3, slice_rate=SLICE_RATE)
    stitcher.mark_dead(2)
    spots = []
    pipe = Pipeline(sample_rate=stitcher.sample_rate, center_frequency=CENTER,
                    on_spot=spots.append, min_snr_db=3,
                    coverage_fn=lambda: _coverage(stitcher))
    sources = _slice_sources(duration_s=25)[:2]
    streams = list(enumerate(src.stream_iq() for src in sources))
    while streams:
        for pair in list(streams):
            idx, stream = pair
            block = next(stream, None)
            if block is None:
                streams.remove(pair)
                continue
            stitcher.push(idx, block)
        out = stitcher.pull()
        if out is not None:
            pipe.process(out)
    calls = {s.call for s in spots}
    assert {"W1AW", "K1ABC"} <= calls


def test_no_image_spots_in_dead_slice_region():
    """Regression for a live-Kiwi finding: with slice 1 dead, the upsampling
    image of a strong slice-2 station lands 12 kHz below the true frequency,
    inside slice 1's uncovered region. Its relative SNR there is high (that
    region has no real noise floor), so without coverage masking it was
    spotted as a duplicate call. Coverage masking must suppress it."""
    stitcher = SliceStitcher(n_slices=3, slice_rate=SLICE_RATE)
    stitcher.mark_dead(1)
    spots = []
    pipe = Pipeline(sample_rate=stitcher.sample_rate, center_frequency=CENTER,
                    on_spot=spots.append, min_snr_db=3,
                    coverage_fn=lambda: _coverage(stitcher))
    # strong station in slice 2 at slice-local -4.5 kHz -> 14047.5 kHz; its
    # x3-upsampling images fall at 14035.5 kHz (dead slice 1: no noise floor,
    # must be coverage-masked) and 14023.5 kHz (slice 0: buried in that
    # slice's own noise floor by the stitcher's stopband)
    src = SynthSource([SynthSignal(-4500, 28, 35, "CQ DE KU8E KU8E")],
                      sample_rate=SLICE_RATE, duration_s=25,
                      realtime=False, seed=5, block_size=1024)
    noise_only = SynthSource([], sample_rate=SLICE_RATE, duration_s=25,
                             realtime=False, seed=6, block_size=1024)
    for block, noise in zip(src.stream_iq(), noise_only.stream_iq()):
        stitcher.push(0, noise[:len(block)])
        stitcher.push(2, block)
        out = stitcher.pull()
        if out is not None:
            pipe.process(out)
    ku8e = [s for s in spots if s.call == "KU8E"]
    assert ku8e, "the real station must still be spotted"
    for s in ku8e:
        assert abs(s.freq_hz - 14_047_500) < 60, \
            f"image spot at {s.freq_hz:.0f} Hz should have been masked"
