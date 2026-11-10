"""Spotter tests: dedup and self-spot suppression."""

from openskimmer.spotting.spotter import Spot, Spotter


def _spot(call, khz=14030.0, snr=20, wpm=25):
    return Spot(freq_hz=khz * 1000, call=call, snr_db=snr, wpm=wpm)


def test_self_spot_suppressed():
    out = []
    sp = Spotter(out.append, my_call="n0call")  # case-insensitive
    assert sp.submit(_spot("N0CALL")) is False       # own call, any frequency
    assert sp.submit(_spot("N0CALL", khz=7095.0)) is False
    assert sp.submit(_spot("W1AW")) is True         # others still spot
    assert [s.call for s in out] == ["W1AW"]


def test_no_my_call_spots_everything():
    out = []
    sp = Spotter(out.append)                         # my_call=None
    assert sp.submit(_spot("N0CALL")) is True


def test_min_snr_and_wpm_gate():
    out = []
    sp = Spotter(out.append, min_snr_db=5)
    assert sp.submit(_spot("W1AW", snr=3)) is False
    assert sp.submit(_spot("W1AW", wpm=0)) is False


def _at(clock):
    """A Spotter driven by a controllable clock."""
    out = []
    sp = Spotter(out.append, clock=lambda: clock[0], respot_interval_s=600)
    return out, sp


def test_respot_tolerates_channel_recreation_drift():
    # observed live 2026-09-18: IZ2EWR, one continuously-CQing real station,
    # spotted 3 times in ~2 minutes as its channel was retired (20 s idle
    # between CQ cycles in a crowded pileup) and recreated at a drifted bin
    # each time -- 18086.4, 18086.7, 18086.1 kHz here, up to 600 Hz apart.
    # The old default (bucket +/-1, ~100-200 Hz) let all three through as
    # "new" spots; respot_tol_hz must absorb this.
    clock = [1000.0]
    out = []
    sp = Spotter(out.append, clock=lambda: clock[0], respot_interval_s=600)
    assert sp.submit(_spot("IZ2EWR", khz=18086.4, snr=35)) is True
    clock[0] += 57
    assert sp.submit(_spot("IZ2EWR", khz=18086.7, snr=33)) is False
    clock[0] += 50
    assert sp.submit(_spot("IZ2EWR", khz=18086.1, snr=26)) is False
    assert [s.call for s in out] == ["IZ2EWR"]
    # a genuinely different call at a similarly drifted frequency is NOT
    # suppressed -- this only ever matches the SAME call, which can only be
    # the same physical station
    assert sp.submit(_spot("DL1ABC", khz=18086.5, snr=20)) is True
    # and a real, deliberate QSY well outside the tolerance still re-spots
    clock[0] += 10
    assert sp.submit(_spot("IZ2EWR", khz=18093.0, snr=20)) is True


def test_respot_tol_hz_is_configurable():
    clock = [1000.0]
    out = []
    sp = Spotter(out.append, clock=lambda: clock[0], respot_interval_s=600,
                respot_tol_hz=100.0)
    assert sp.submit(_spot("IZ2EWR", khz=18086.4, snr=35)) is True
    # 600 Hz of drift is well outside a tight 100 Hz tolerance
    assert sp.submit(_spot("IZ2EWR", khz=18087.0, snr=26)) is True


def test_a_de_spot_does_not_hold_back_a_cq():
    # CWT 2026-10-21: "QRL? DE W4CMG" went out as DE, then "CQ W4CMG CWT"
    # was suppressed as a repeat, and the Aggregator holds DE, so the runner
    # never reached the RBN
    clock = [1000.0]
    out, sp = _at(clock)
    assert sp.submit(Spot(14047500, "W4CMG", 25, 36, activity="DE")) is True
    clock[0] += 30
    assert sp.submit(Spot(14047500, "W4CMG", 25, 36, activity="CQ")) is True
    clock[0] += 30
    # after the CQ, every repeat is held back, CQ or DE
    assert sp.submit(Spot(14047500, "W4CMG", 25, 36, activity="CQ")) is False
    assert sp.submit(Spot(14047500, "W4CMG", 25, 36, activity="DE")) is False
    # and a DE is never followed by another DE
    assert sp.submit(Spot(14020000, "NY2PO", 9, 29, activity="DE")) is True
    assert sp.submit(Spot(14020000, "NY2PO", 9, 29, activity="DE")) is False
    assert [(s.call, s.activity) for s in out] == [
        ("W4CMG", "DE"), ("W4CMG", "CQ"), ("NY2PO", "DE")]


def test_a_dupe_sent_as_cq_holds_back_a_cq():
    clock = [1000.0]
    out, sp = _at(clock)
    assert sp.submit(Spot(14030000, "K2WK", 20, 25, activity="DUPE")) is True
    assert out[-1].activity == "CQ"
    assert sp.submit(Spot(14030000, "K2WK", 20, 25, activity="CQ")) is False


def test_repetition_is_a_cq_unless_someone_is_running_there():
    # "DUPE" (a call repeating with no CQ/DE grammar) is BOTH a CQ caller
    # whose "CQ" we missed and an S&P station answering someone else. What
    # separates them is not the repetition, it is whether another station is
    # already running on that frequency.
    clock = [1000.0]
    out, sp = _at(clock)

    # nobody running here -> treat the repeat as the CQ we failed to copy
    sp.submit(Spot(freq_hz=14030_000.0, call="W1AW", snr_db=20, wpm=25,
                   activity="DUPE"))
    assert [(s.call, s.activity) for s in out] == [("W1AW", "CQ")]
    out.clear()

    # a runner calls CQ, then a caller repeats 100 Hz away -> held as DE,
    # which the RBN Aggregator does not forward
    clock[0] += 10
    sp.submit(Spot(freq_hz=14040_000.0, call="K1ABC", snr_db=20, wpm=25,
                   activity="CQ"))
    clock[0] += 10
    sp.submit(Spot(freq_hz=14040_100.0, call="W2XYZ", snr_db=20, wpm=25,
                   activity="DUPE"))
    assert [(s.call, s.activity) for s in out] == [("K1ABC", "CQ"),
                                                   ("W2XYZ", "DUPE")]
    out.clear()

    # 3 kHz away is a different station, not one of its callers
    clock[0] += 10
    sp.submit(Spot(freq_hz=14043_000.0, call="W3QRS", snr_db=20, wpm=25,
                   activity="DUPE"))
    assert [(s.call, s.activity) for s in out] == [("W3QRS", "CQ")]
    out.clear()

    # the runner's own later repeat is not one of its own callers
    clock[0] += 700
    sp.submit(Spot(freq_hz=14040_000.0, call="K1ABC", snr_db=20, wpm=25,
                   activity="DUPE"))
    assert [(s.call, s.activity) for s in out] == [("K1ABC", "CQ")]
    out.clear()

    # and once the runner is long gone the frequency is clear again
    clock[0] += 1000
    sp.submit(Spot(freq_hz=14040_000.0, call="W4NEW", snr_db=20, wpm=25,
                   activity="DUPE"))
    assert [(s.call, s.activity) for s in out] == [("W4NEW", "CQ")]


def test_implausible_speed_is_not_spotted():
    """A railed timing estimate must not reach the RBN.

    CWDecoder clamps dit_ms to [15, 400] ms and 15 ms IS 80 wpm, so a spot
    at 80 means the estimate hit its floor -- what a steady carrier does to
    it. This is the E4IK case: 27 spots over 8 days, always 10100.8 kHz,
    almost always 80 wpm, and E4 is Palestine, so each one sent DXers
    chasing an all-time-new-one after a noise source.
    """
    spots = []
    s = Spotter(spots.append, min_snr_db=3, max_wpm=50)
    assert s.submit(Spot(freq_hz=10_100_800, call="E4IK", snr_db=20, wpm=80)) is False
    assert s.submit(Spot(freq_hz=10_100_800, call="E4IK", snr_db=20, wpm=60)) is False
    assert spots == []
    # a fast but believable operator still gets through
    assert s.submit(Spot(freq_hz=14_030_000, call="K1ABC", snr_db=20, wpm=45)) is True
    assert s.submit(Spot(freq_hz=14_031_000, call="K2ABC", snr_db=20, wpm=50)) is True
    assert [x.call for x in spots] == ["K1ABC", "K2ABC"]


def test_max_wpm_can_be_disabled():
    spots = []
    s = Spotter(spots.append, min_snr_db=3, max_wpm=0)
    assert s.submit(Spot(freq_hz=14_030_000, call="K1ABC", snr_db=20, wpm=99)) is True


def test_blocked_calls_are_never_spotted():
    """Blocking is deliberately absolute -- no evidence overrides it."""
    spots = []
    s = Spotter(spots.append, min_snr_db=3, blocked={"E4IK"})
    for wpm, act in ((25, "CQ"), (25, "DE"), (30, "DUPE")):
        assert s.submit(Spot(freq_hz=10_100_800, call="E4IK",
                             snr_db=40, wpm=wpm, activity=act)) is False
    assert s.submit(Spot(freq_hz=10_100_800, call="e4ik",
                         snr_db=40, wpm=25)) is False        # case-insensitive
    assert spots == []
    assert s.submit(Spot(freq_hz=10_100_800, call="E4IL", snr_db=40, wpm=25)) is True


def test_cq_runner_guard_reports_a_second_cq_on_a_run_frequency_as_de():
    # experimental, off by default: on the Sep 16 CWT replays, 100 Hz /
    # 300 s took the caller-as-runner rate from 19-25 % to 12-13 %
    # for one lost runner in each session
    clock = [0.0]
    out = []
    sp = Spotter(out.append, clock=lambda: clock[0], cq_runner_guard=True,
                 runner_tol_hz=100, runner_memory_s=300)
    assert sp.submit(_spot("N4BP", khz=14032.3))
    clock[0] = 60
    assert sp.submit(_spot("N1EN", khz=14032.3))       # still reported...
    assert out[-1].activity == "DE"                     # ...but not as CQ
    clock[0] = 120
    assert sp.submit(_spot("KW1X", khz=14032.6))       # 300 Hz away: its own
    assert out[-1].activity == "CQ"
    clock[0] = 500
    assert sp.submit(_spot("W8FN", khz=14032.3))       # N4BP gone 440 s
    assert out[-1].activity == "CQ"


def test_cq_runner_guard_is_off_by_default():
    out = []
    sp = Spotter(out.append, clock=lambda: 0.0)
    sp.submit(_spot("N4BP"))
    sp.submit(_spot("N1EN"))
    assert [s.activity for s in out] == ["CQ", "CQ"]
