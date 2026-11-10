import numpy as np
import pytest

from openskimmer.sdr.hl2 import (MAX_RECEIVERS, HermesLite2Source, _radios,
                                 n2adr_filter_byte, parse_iq_packet)


def _make_packet(rx_iq, ptt=False, endpoint=6, seq=0):
    """rx_iq: list of (real, imag) per receiver; wire order is imag first
    (HPSDR convention per Quisk's read_rx_udp10)."""
    n = len(rx_iq)
    group = b"".join(im.to_bytes(3, "big", signed=True)
                     + re.to_bytes(3, "big", signed=True)
                     for re, im in rx_iq) + b"\x00\x00"
    per_frame = 504 // len(group)

    def frame():
        c0 = 0x01 if ptt else 0x00
        body = group * per_frame
        return (b"\x7f\x7f\x7f" + bytes([c0, 0, 0, 0, 0]) + body
                + b"\x00" * (504 - len(body)))
    return (b"\xef\xfe\x01" + bytes([endpoint]) + seq.to_bytes(4, "big")
            + frame() + frame())


def test_parse_valid_packet():
    pkt = _make_packet([(1 << 22, -(1 << 22))])
    per_rx, ptt = parse_iq_packet(pkt)
    assert per_rx is not None and len(per_rx) == 1
    assert len(per_rx[0]) == 126
    assert not ptt
    np.testing.assert_allclose(per_rx[0].real, 0.5, rtol=1e-6)
    np.testing.assert_allclose(per_rx[0].imag, -0.5, rtol=1e-6)


def test_parse_two_receivers():
    pkt = _make_packet([(1 << 22, 0), (-(1 << 21), 1 << 20)])
    per_rx, _ = parse_iq_packet(pkt, n_rx=2)
    assert len(per_rx) == 2
    assert len(per_rx[0]) == len(per_rx[1]) == 72   # 2 frames x 36 samples
    np.testing.assert_allclose(per_rx[0].real, 0.5, rtol=1e-6)
    np.testing.assert_allclose(per_rx[1].real, -0.25, rtol=1e-6)
    np.testing.assert_allclose(per_rx[1].imag, 0.125, rtol=1e-6)


def test_parse_ptt_flag():
    _, ptt = parse_iq_packet(_make_packet([(0, 0)], ptt=True))
    assert ptt


def test_reject_wrong_endpoint():
    per_rx, _ = parse_iq_packet(_make_packet([(0, 0)], endpoint=4))
    assert per_rx is None


def test_reject_short_packet():
    per_rx, _ = parse_iq_packet(b"\xef\xfe\x01\x06")
    assert per_rx is None


def test_sign_extension_negative_full_scale():
    pkt = _make_packet([(-(1 << 23), (1 << 23) - 1)])
    per_rx, _ = parse_iq_packet(pkt)
    np.testing.assert_allclose(per_rx[0].real, -1.0, rtol=1e-6)
    np.testing.assert_allclose(per_rx[0].imag, 1.0, rtol=1e-3)


def test_n2adr_filter_bytes():
    assert n2adr_filter_byte(1_830_000) == 0x01          # 160m: LPF only
    assert n2adr_filter_byte(3_550_000) == 0x02 | 0x40   # 80m + HPF
    assert n2adr_filter_byte(7_030_000) == 0x04 | 0x40   # 40m + HPF
    assert n2adr_filter_byte(14_030_000) == 0x08 | 0x40  # 20m + HPF
    assert n2adr_filter_byte(21_030_000) == 0x10 | 0x40  # 15m + HPF
    assert n2adr_filter_byte(28_030_000) == 0x20 | 0x40  # 10m + HPF
    # multi-receiver: LPF for the highest band, HPF per the lowest
    assert n2adr_filter_byte(14_030_000, 7_030_000) == 0x08 | 0x40
    assert n2adr_filter_byte(14_030_000, 1_830_000) == 0x08


def test_shared_radio_and_config():
    _radios.clear()
    a = HermesLite2Source(host="127.0.0.1")
    b = HermesLite2Source(host="127.0.0.1")
    assert a._radio is b._radio
    a.set_center_frequency(14_030_000)
    b.set_center_frequency(7_030_000)
    a.set_sample_rate(192000)
    rate, c2, c3, c4 = a._radio._config_c0()
    assert c2 == (0x48 << 1)              # 20m LPF + HPF in C2[7:1]
    assert c4 == 0x04 | (1 << 3)          # duplex + 2 receivers
    frames = a._radio._config_frames()
    assert frames[1][3] == 0x02 << 1      # RX1 NCO register
    assert frames[2][3] == 0x03 << 1      # RX2 NCO register
    assert frames[1][4:8] == (14_030_000).to_bytes(4, "big")
    assert frames[2][4:8] == (7_030_000).to_bytes(4, "big")
    with pytest.raises(ValueError):
        b.set_sample_rate(96000)          # rate is shared
    _radios.clear()


def test_receiver_cap():
    _radios.clear()
    for _ in range(MAX_RECEIVERS):
        HermesLite2Source(host="127.0.0.2")
    with pytest.raises(ValueError):
        HermesLite2Source(host="127.0.0.2")
    _radios.clear()
