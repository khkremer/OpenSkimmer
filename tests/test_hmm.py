"""HMM (fixed-lag Viterbi) decoder tests, including behaviors that must
differ from the classical threshold decoder."""

import numpy as np

from openskimmer.dsp.decoder import CWDecoder
from openskimmer.dsp.hmm_decoder import HMMDecoder
from openskimmer.dsp.morse import CODE
from openskimmer.pipeline import Pipeline
from openskimmer.sdr.synth import SynthSignal, SynthSource

FRAME_DT = 0.005
UNIT_FRAMES = 10  # 50 ms dit ~ 24 WPM


def keying(text, char_gap=3, word_gap=7):
    out = []
    first = True
    for word in text.upper().split():
        if not first:
            out.append((False, word_gap))
        for ci, ch in enumerate(word):
            if ci > 0:
                out.append((False, char_gap))
            for si, sym in enumerate(CODE[ch]):
                if si > 0:
                    out.append((False, 1))
                out.append((True, 1 if sym == "." else 3))
        first = False
    return out


def run_decoder(decoder_cls, frames):
    """frames: iterable of envelope values; returns decoded words."""
    words, chars = [], []

    def on_break():
        if chars:
            words.append("".join(chars))
            chars.clear()

    dec = decoder_cls(FRAME_DT, on_char=chars.append, on_word_break=on_break)
    for env in frames:
        dec.process(float(env), 1.0)
    for _ in range(12 * UNIT_FRAMES):
        dec.process(0.5, 1.0)
    on_break()
    return words, dec


def frames_from_keying(units, on=200.0, off=0.5):
    for down, n in units:
        for _ in range(n * UNIT_FRAMES):
            yield on if down else off


def test_hmm_standard_decode():
    words, _ = run_decoder(HMMDecoder,
                           frames_from_keying(keying("CQ CQ DE K4RLC K")))
    assert words[-4:] == ["CQ", "DE", "K4RLC", "K"]


def test_hmm_bridges_qsb_sag():
    # A dah whose envelope sags to ~8 dB below the carrier for 3 frames:
    # the classical -6 dB threshold splits it in two, the Viterbi path pays
    # less by staying down.  Text: "A A A" with the middle A's dah damaged
    # (dit-dah letters, so the dit estimate can lock).
    units = keying("A A A")
    frames = list(frames_from_keying(units, on=200.0))
    dah_starts = []
    pos = 0
    for down, n in units:
        if down and n == 3:
            dah_starts.append(pos)
        pos += n * UNIT_FRAMES
    mid = dah_starts[1] + 12
    for i in range(mid, mid + 3):
        frames[i] = 30.0     # ~8 dB below the 200.0 carrier, above noise

    words_hmm, _ = run_decoder(HMMDecoder, frames)
    assert words_hmm[-3:] == ["A", "A", "A"]  # middle dah stays one element

    words_cls, _ = run_decoder(CWDecoder, frames)
    assert words_cls[-3:] != ["A", "A", "A"]  # classical splits the dah


def test_hmm_no_chars_from_noise_flares():
    # random 1-2 frame flares over flat noise must not decode as text
    rng = np.random.default_rng(3)
    frames = rng.gamma(3.0, 1.0 / 3.0, 4000)  # unit-mean noise, k=3
    flares = rng.choice(4000, 25, replace=False)
    frames[flares] *= 5.0
    _, dec = run_decoder(HMMDecoder, frames)
    assert dec.chars_total <= 2


def test_hmm_pipeline_end_to_end():
    spots = []
    pipe = Pipeline(sample_rate=48000, center_frequency=14_030_000,
                    on_spot=spots.append, decoder_cls=HMMDecoder)
    src = SynthSource([SynthSignal(1000, 25, 20, "CQ TEST DE W1AW W1AW")],
                      sample_rate=48000, duration_s=25,
                      realtime=False, seed=42)
    for block in src.stream_iq():
        pipe.process(block)
    assert "W1AW" in {s.call for s in spots}


def test_lag_is_a_duration_not_a_frame_count():
    """The look-ahead is tuned as ~170 ms. It used to be hardcoded as
    `LAG = 32` frames, which only equals 170 ms because Channelizer scales
    fft_size with the sample rate, holding frame_dt at 5.33 ms from 24 kHz
    to 3.072 MHz -- nothing enforced it. Off that grid it silently drifted:
    8 kHz audio (frame_dt 8 ms) gave 32 frames = 256 ms, 50 % long, and the
    debounce fell from 2 frames to 1. Surfaced by a Swift port that
    feeds this decoder 8 kHz audio.

    Deriving lag from frame_dt must stay a NO-OP at OpenSkimmer's own rate:
    the 5.33 ms grid must still give exactly 32.
    """
    def mk(frame_dt):
        return HMMDecoder(frame_dt, lambda c: None, lambda: None)

    # the rate OpenSkimmer actually runs -- must be unchanged at 32
    assert mk(1 / 187.5).lag == 32

    # off-grid rates now hold the DURATION instead of the frame count
    for frame_dt in (1 / 187.5, 0.0058, 0.008):
        lag = mk(frame_dt).lag
        assert abs(lag * frame_dt - HMMDecoder.LAG_S) < 0.006, frame_dt

    # and it can never round to a nonsensical zero-frame look-ahead
    assert mk(10.0).lag >= 1


class _TracebackHMM(HMMDecoder):
    """The pre-2026-10-13 fixed-lag decision: walk `lag` back-pointers every
    frame. HMMDecoder now uses register exchange instead; both must decide
    identically, ties included."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        from collections import deque
        self._bp = deque(maxlen=self.lag)

    def process(self, envelope, noise):
        import math
        self._frame_idx += 1
        self._noise = (0.995 * self._noise + 0.005 * noise
                       if self._noise > 1e-29 else noise)
        n = max(self._noise, 1e-30)
        self._on_power *= 0.9995
        s = max(self._on_power - n, 3.0 * n)
        mu_up = n + self.EDGE_LEAK * s
        mu_dn = s + n
        llr = self.GAMMA_K * (math.log(mu_up / mu_dn)
                              + envelope * (1.0 / mu_up - 1.0 / mu_dn))
        llr = max(-self.LLR_CLAMP, min(self.LLR_CLAMP, llr))
        up_from_dn = self._metric_dn - self.SWITCH_COST
        dn_from_up = self._metric_up - self.SWITCH_COST
        if self._metric_up >= up_from_dn:
            new_up, prev_up = self._metric_up, False
        else:
            new_up, prev_up = up_from_dn, True
        if self._metric_dn >= dn_from_up:
            new_dn, prev_dn = self._metric_dn, True
        else:
            new_dn, prev_dn = dn_from_up, False
        new_dn += llr
        norm = max(new_up, new_dn)
        self._metric_up = new_up - norm
        self._metric_dn = new_dn - norm
        self._bp.append((prev_up, prev_dn))
        self._lag_env.append(envelope)
        if len(self._bp) < self.lag:
            return
        down = self._metric_dn > self._metric_up
        for prev_up, prev_dn in reversed(self._bp):
            down = prev_dn if down else prev_up
        self._emit(down, self._lag_env[0])


def test_register_exchange_matches_traceback():
    # weak, fading, noisy keying so the survivor paths genuinely disagree
    # and the decision depends on the full lag window
    rng = np.random.default_rng(7)
    text = "CQ CQ DE N0CALL N0CALL TEST 5NN TU"
    frames = []
    for down, units in keying(text):
        frames += [down] * (units * UNIT_FRAMES)
    frames += [False] * 400
    for amp in (2.0, 4.0, 12.0):
        out = {}
        for cls in (HMMDecoder, _TracebackHMM):
            got = []
            d = cls(FRAME_DT, on_char=got.append,
                    on_word_break=lambda: got.append(" "))
            r = np.random.default_rng(int(amp * 100))
            keyed = []
            for i, k in enumerate(frames):
                fade = 0.6 + 0.4 * np.sin(i / 90.0)
                env = r.exponential(1.0) + (amp * fade if k else 0.0)
                d.process(env, 1.0)
                keyed.append(d.key_down)
            out[cls] = ("".join(got), keyed)
        assert out[HMMDecoder] == out[_TracebackHMM], amp
    assert "N0CALL" in out[HMMDecoder][0]
