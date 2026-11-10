"""End-to-end: synthetic multi-signal IQ passband -> validated spots."""

from openskimmer.pipeline import Pipeline
from openskimmer.sdr.synth import SynthSignal, SynthSource


def run_scenario(signals, duration_s, sample_rate=48000, center=14_030_000):
    src = SynthSource(signals, sample_rate=sample_rate,
                      center_frequency=center, duration_s=duration_s,
                      realtime=False, seed=42)
    spots = []
    pipe = Pipeline(sample_rate=sample_rate, center_frequency=center,
                    on_spot=spots.append, min_snr_db=3)
    src.connect()
    for block in src.stream_iq():
        pipe.process(block)
    return spots


def test_single_signal_cq():
    spots = run_scenario(
        [SynthSignal(offset_hz=1000, wpm=25, snr_db=20,
                     text="CQ TEST DE W1AW W1AW")],
        duration_s=25,
    )
    calls = {s.call for s in spots}
    assert "W1AW" in calls
    spot = next(s for s in spots if s.call == "W1AW")
    assert abs(spot.freq_hz - 14_031_000) < 60
    assert 20 <= spot.wpm <= 30
    assert spot.snr_db >= 10


def test_three_simultaneous_signals():
    spots = run_scenario(
        [
            SynthSignal(-8000, 30, 25, "CQ DE K1ABC K1ABC"),
            SynthSignal(1000, 22, 18, "CQ TEST DE W1AW W1AW"),
            SynthSignal(9000, 26, 12, "DJ2XYZ DJ2XYZ TEST"),
        ],
        duration_s=30,
    )
    calls = {s.call for s in spots}
    assert {"K1ABC", "W1AW", "DJ2XYZ"} <= calls
    by_call = {s.call: s for s in spots}
    assert abs(by_call["K1ABC"].freq_hz - 14_022_000) < 60
    assert abs(by_call["W1AW"].freq_hz - 14_031_000) < 60
    assert abs(by_call["DJ2XYZ"].freq_hz - 14_039_000) < 60


def test_dedup_no_spot_flood():
    spots = run_scenario(
        [SynthSignal(2000, 28, 20, "CQ DE W1AW W1AW", repeat_gap_s=2.0)],
        duration_s=40,
    )
    w1aw = [s.activity for s in spots if s.call == "W1AW"]
    # the respot interval far exceeds this run, so exactly one CQ spot; the
    # first cycle's CQ is lost while the decoder acquires, so a DE may come
    # first, and the DE must not hold back the CQ
    assert w1aw.count("CQ") == 1
    assert w1aw in (["CQ"], ["DE", "CQ"])


def test_no_spots_in_pure_noise():
    spots = run_scenario([], duration_s=15)
    assert spots == []


def test_channel_log_words_carry_the_media_clock():
    """Diagnostic words must be stamped on the same clock as the spots.

    On replay that is audio time. Stamping instead at the point the log line
    is written yielded replay wall-clock, which silently made the channel log
    uncorrelatable with the spot log and with an RBN archive -- the exact
    pairing the log exists to support.
    """
    epoch = 1_754_000_000.0          # arbitrary fixed UTC epoch
    sample_rate, center, duration = 48000, 14_030_000, 25
    src = SynthSource([SynthSignal(offset_hz=1000, wpm=25, snr_db=20,
                                   text="CQ TEST DE W1AW W1AW")],
                      sample_rate=sample_rate, center_frequency=center,
                      duration_s=duration, realtime=False, seed=42)
    words, spots = [], []
    pipe = Pipeline(sample_rate=sample_rate, center_frequency=center,
                    on_spot=spots.append, min_snr_db=3,
                    on_word=words.append, media_clock_epoch=epoch)
    src.connect()
    for block in src.stream_iq():
        pipe.process(block)

    assert words, "no diagnostic words produced"
    times = [w["t"] for w in words]
    # inside the recording, not near now
    assert all(epoch <= t <= epoch + duration + 1 for t in times), times[:5]
    assert times == sorted(times), "media time must advance monotonically"
    # and it must agree with the clock the spots were stamped on
    assert abs(spots[0].time_utc - epoch) <= duration + 1


def test_char_log_reports_the_gap_before_each_character():
    """--log-chars: every character arrives with the silence before it, and
    the word breaks are in the same stream. Report-only: the spots are the
    same with the log on as off."""
    sample_rate, center = 48000, 14_030_000
    sig = [SynthSignal(offset_hz=1000, wpm=25, snr_db=25,
                       text="CQ TEST DE W1AW W1AW")]

    def run(on_char):
        src = SynthSource(sig, sample_rate=sample_rate,
                          center_frequency=center, duration_s=25,
                          realtime=False, seed=42)
        spots = []
        pipe = Pipeline(sample_rate=sample_rate, center_frequency=center,
                        on_spot=spots.append, min_snr_db=3, on_char=on_char)
        src.connect()
        for block in src.stream_iq():
            pipe.process(block)
        return [(s.call, s.activity, s.freq_hz) for s in spots]

    chars = []
    assert run(chars.append) == run(None)
    text = "".join(c["char"].replace("|", " ") for c in chars)
    assert "CQ TEST DE W1AW W1AW" in text
    # inside a word the gap before a character is ~3 dits, and the first
    # character of a word follows a word gap, ~7 dits
    inside, first = [], []
    after_break = True
    for c in chars:
        if c["char"] == "|":
            after_break = True
            continue
        gap_ms, dit_ms, thr = c["gap"]
        (first if after_break else inside).append(gap_ms / dit_ms)
        after_break = False
    inside, first = sorted(inside), sorted(first)
    assert 2.5 < inside[len(inside) // 2] < 3.6
    assert 6.0 < first[len(first) // 2]
