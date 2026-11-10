"""Multi-band CLI plumbing tests."""

import pytest

from openskimmer.main import build_parser, parse_band_spec


def test_band_spec_forms():
    assert parse_band_spec("14030000", "hl2", 192000) == \
        ("hl2", None, 14030000, 192000)
    assert parse_band_spec("7030000/36000", "hl2", 192000) == \
        ("hl2", None, 7030000, 36000)
    assert parse_band_spec("kiwi:7030000/36000", "hl2", 192000) == \
        ("kiwi", None, 7030000, 36000)
    assert parse_band_spec("kiwi@kiwisdr.local:7030000", "hl2", 48000) == \
        ("kiwi", "kiwisdr.local", 7030000, 48000)
    assert parse_band_spec("hl2@192.0.2.10:14030000/192000", "kiwi", 48000) == \
        ("hl2", "192.0.2.10", 14030000, 192000)


def test_band_spec_rejects_garbage():
    with pytest.raises(SystemExit):
        parse_band_spec("kiwi:sevenmegs", "hl2", 48000)


def test_band_args_parse():
    args = build_parser().parse_args(
        ["--band", "hl2:14030000/192000", "--band", "kiwi:7030000/36000"])
    assert args.band == ["hl2:14030000/192000", "kiwi:7030000/36000"]


def test_worker_mode_auto_uses_processes_beyond_one_band():
    from openskimmer.main import worker_mode
    one = [("ka9q", "g1", 14050000, 192000)]
    two = one + [("ka9q", "g2", 7050000, 192000)]
    # the decoders are pure Python, so band threads share one core; auto
    # keeps a lone band on a thread (replay/--record unchanged) and puts
    # 2+ bands in their own processes
    assert worker_mode("auto", one) == "thread"
    assert worker_mode("auto", two) == "process"
    # an explicit choice otherwise wins
    assert worker_mode("thread", two) == "thread"
    assert worker_mode("process", one) == "process"


def test_worker_mode_keeps_shared_hl2_bands_in_one_process():
    from openskimmer.main import worker_mode, shared_radio_conflict
    # several hl2 bands on one host share a single UDP session through a
    # per-process registry, so they must not be split across processes
    shared = [("hl2", "192.0.2.10", 14030000, 192000),
              ("hl2", "192.0.2.10", 7030000, 192000)]
    assert shared_radio_conflict(shared)
    assert worker_mode("auto", shared) == "thread"
    assert worker_mode("process", shared) == "thread"   # correctness wins
    # distinct radios, and non-hl2 sources, are free to split
    assert shared_radio_conflict(
        [("hl2", "192.0.2.10", 14030000, 192000),
         ("hl2", "192.0.2.20", 7030000, 192000)]) is None
    assert shared_radio_conflict(
        [("kiwi", "k.local", 14030000, 24000),
         ("kiwi", "k.local", 7030000, 24000)]) is None


def test_merge_ranges_bridges_kiwi_slices():
    from openskimmer.main import merge_ranges
    # adjacent kiwi slices (~200 Hz gaps) -> one contiguous range
    frag = [(21021100, 21032900), (21033100, 21044900),
            (21045100, 21056900), (21057100, 21068900)]
    assert merge_ranges(frag) == [(21021100, 21068900)]
    # distinct bands stay separate
    two = [(7000000, 7096000), (14000000, 14096000)]
    assert merge_ranges(two) == two
    # a real dead-slice gap (12 kHz > tol) is preserved
    gap = [(7000000, 7011800), (7024000, 7035800)]
    assert len(merge_ranges(gap)) == 2
    # overlapping ranges merge to the union
    assert merge_ranges([(100, 500), (400, 900)]) == [(100, 900)]


def test_start_time_parsing_and_filename_recovery():
    import time
    from openskimmer.main import parse_start_time, start_time_from_name

    def utc(e):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e))

    assert utc(parse_start_time("2026-08-12T19:00:00Z")) == "2026-08-12 19:00:00"
    assert utc(parse_start_time("2026-08-12 19:00")) == "2026-08-12 19:00:00"
    # the compact minute form must not be read as H:MM:SS -- "1902" is 19:02,
    # not 19:00:02, and strptime matches %H%M%S loosely enough to get it wrong
    assert utc(parse_start_time("20260812T1902Z")) == "2026-08-12 19:02:00"
    assert utc(parse_start_time("20260812T190245Z")) == "2026-08-12 19:02:45"
    # tools/record_iq.py names captures so they carry their own start time
    assert utc(start_time_from_name(
        "recordings/cwt_20m_20260715T1902Z.c64")) == "2026-07-15 19:02:00"
    assert start_time_from_name("recordings/no_stamp_here.c64") is None
    assert start_time_from_name(None) is None
