"""ka9q backend RTP/IQ parsing tests (built from the verified wire format)."""

import struct

import numpy as np

from openskimmer.sdr.ka9q import Ka9qSource, parse_rtp_iq


def make_rtp_iq(ssrc, iq_pairs, seq=0, pt=77, ext=False):
    """Build an RTP packet: 12-byte header + interleaved int16 BE I/Q."""
    b0 = 0x80 | (0x10 if ext else 0)
    hdr = bytes([b0, pt]) + struct.pack("!HII", seq, 12345, ssrc)
    if ext:
        hdr += struct.pack("!HH", 0xBEDE, 1) + b"\x00\x00\x00\x00"
    flat = []
    for i, q in iq_pairs:
        flat += [i, q]
    return hdr + struct.pack(f">{len(flat)}h", *flat)


def test_parse_iq_roundtrip():
    pkt = make_rtp_iq(28050, [(16384, -16384), (8192, 0)], seq=1927)
    seq, iq = parse_rtp_iq(pkt, 28050)
    assert seq == 1927
    assert len(iq) == 2
    np.testing.assert_allclose(iq.real, [0.5, 0.25], atol=1e-4)
    np.testing.assert_allclose(iq.imag, [-0.5, 0.0], atol=1e-4)


def test_ssrc_filter():
    pkt = make_rtp_iq(28050, [(1, 2)])
    assert parse_rtp_iq(pkt, 14030) is None      # different channel
    assert parse_rtp_iq(pkt, 28050) is not None


def test_extension_header_offset():
    pkt = make_rtp_iq(28050, [(100, -100)], ext=True)
    seq, iq = parse_rtp_iq(pkt, 28050)
    np.testing.assert_allclose(iq.real, [100 / 32768.0], atol=1e-6)


def test_rejects_short_and_empty():
    assert parse_rtp_iq(b"\x80\x4d\x00\x01", 28050) is None    # too short
    assert parse_rtp_iq(make_rtp_iq(28050, []), 28050) is None  # no samples


def test_ssrc_derived_from_center_khz():
    src = Ka9qSource(group="239.1.2.3")
    src.set_center_frequency(28_050_000)
    assert src._ssrc == 28050                    # ka9q SSRC = freq in kHz
    src2 = Ka9qSource(group="239.1.2.3", ssrc=99)
    src2.set_center_frequency(28_050_000)
    assert src2._ssrc == 99                       # explicit override wins


def make_rtp_f32(ssrc, iq_pairs, seq=0, pt=97):
    """RTP packet with interleaved little-endian float32 I/Q (radiod encoding = f32)."""
    hdr = bytes([0x80, pt]) + struct.pack("!HII", seq, 12345, ssrc)
    flat = [x for pair in iq_pairs for x in pair]
    return hdr + struct.pack(f"<{len(flat)}f", *flat)


def test_parse_f32_roundtrip():
    pkt = make_rtp_f32(7090, [(0.5, -0.25), (1e-3, 0.0)], seq=42)
    seq, iq = parse_rtp_iq(pkt, 7090, "f32le")
    assert seq == 42
    np.testing.assert_allclose(iq.real, [0.5, 1e-3], rtol=1e-6)
    np.testing.assert_allclose(iq.imag, [-0.25, 0.0], atol=1e-7)


def test_f32_read_as_s16_is_not_the_signal():
    # the silent failure the option exists for: same packet, wrong format
    pkt = make_rtp_f32(7090, [(0.5, -0.25)] * 4)
    _, iq = parse_rtp_iq(pkt, 7090)
    assert len(iq) == 8                       # twice the samples, none of them right
    assert not np.allclose(iq[:4], 0.5 - 0.25j)


def test_encoding_is_checked():
    import pytest
    with pytest.raises(ValueError):
        Ka9qSource(group="239.1.2.3", encoding="f32be")


def test_options_reach_the_source():
    from openskimmer.main import build_parser, make_source
    args = build_parser().parse_args(["--rtp-encoding", "f32le", "--rtp-iface", "127.0.0.1"])
    src = make_source(args, "ka9q", "239.101.2.1", 7090000, 192000)
    assert src._encoding == "f32le" and src._iface == "127.0.0.1"
