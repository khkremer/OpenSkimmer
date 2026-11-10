import numpy as np

from openskimmer.pipeline import Pipeline, TextTap
from openskimmer.sdr.synth import SynthSignal, SynthSource
from openskimmer.spotting.callsign import CallsignExtractor
from openskimmer.web.server import n_cols_for, spectrum_to_row


def test_spectrum_row_size_and_range():
    power = np.full(8192, 1e-6)
    power[4096] = 1.0  # strong carrier, 60 dB over the floor
    row = np.frombuffer(spectrum_to_row(power), dtype=np.uint8)
    assert len(row) == n_cols_for(8192) <= 2048
    assert row.max() == 255          # carrier saturates
    assert np.median(row) < 60       # floor sits low in the ramp


def test_spectrum_row_small_fft_not_padded():
    row = np.frombuffer(spectrum_to_row(np.ones(1024)), dtype=np.uint8)
    assert len(row) == 1024


def test_text_tap_rolls_and_forwards():
    calls = []
    tap = TextTap(CallsignExtractor(lambda c, a: calls.append(c)), maxlen=16)
    for word in "CQ DE W1AW W1AW".split():
        for ch in word:
            tap.add_char(ch)
        tap.add_word_break()
    assert calls == ["W1AW"]  # second copy confirms, then spots once
    assert tap.text.endswith("W1AW W1AW ")
    assert len(tap.text) <= 16


def test_pipeline_ui_taps():
    rows = []
    snapshots = []
    pipe = Pipeline(sample_rate=48000, center_frequency=14_030_000,
                    on_spot=lambda s: None,
                    on_spectrum=rows.append, on_channels=snapshots.append)
    src = SynthSource([SynthSignal(1000, 25, 20, "CQ TEST DE W1AW W1AW")],
                      sample_rate=48000, duration_s=10, realtime=False, seed=1)
    for block in src.stream_iq():
        pipe.process(block)
    # ~15 rows/s and ~1 snapshot/s
    assert 100 <= len(rows) <= 170
    assert 8 <= len(snapshots) <= 12
    assert all(len(r) == pipe.channelizer.fft_size for r in rows)
    final = snapshots[-1]
    assert len(final) == 1
    assert abs(final[0]["freq_hz"] - 14_031_000) < 60
    assert "W1AW" in final[0]["text"]


def test_spectrum_row_with_masked_coverage_gap():
    # Half the band has no slice coverage (masked to exactly 0 power). The
    # gap must render black and must not drag the dB floor down (which
    # would blast the covered half to white).
    power = np.full(2048, 1e-6)
    power[:1024] = 0.0            # uncovered region
    power[1500] = 1e-0            # a 60 dB signal in the covered half
    row = np.frombuffer(spectrum_to_row(power), dtype=np.uint8)
    assert row[:1024].max() == 0             # gap is black
    assert 20 < np.median(row[1024:]) < 60   # covered floor sits low
    assert row[1500] == 255                  # signal still saturates


def test_pipeline_word_tap():
    words = []
    pipe = Pipeline(sample_rate=48000, center_frequency=14_030_000,
                    on_spot=lambda s: None, on_word=words.append)
    src = SynthSource([SynthSignal(1000, 25, 20, "CQ TEST DE W1AW W1AW")],
                      sample_rate=48000, duration_s=12, realtime=False, seed=1)
    for block in src.stream_iq():
        pipe.process(block)
    texts = [w["word"] for w in words]
    assert "W1AW" in texts
    w = next(w for w in words if w["word"] == "W1AW")
    assert abs(w["freq_hz"] - 14_031_000) < 60
    assert w["wpm"] > 0 and w["snr_db"] > 10
