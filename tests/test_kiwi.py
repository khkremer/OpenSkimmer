import struct

import numpy as np

from openskimmer.sdr.kiwi_protocol import (SND_FLAG_COMPRESSED,
                                           SND_FLAG_STEREO,
                                           client_stream_commands, parse_msg,
                                           parse_snd_iq)
from openskimmer.sdr.stitcher import SliceStitcher


def make_snd_body(seq: int, iq: np.ndarray,
                  flags: int = SND_FLAG_STEREO) -> bytes:
    """Body of an IQ-mode SND frame (after the 3-byte tag)."""
    header = struct.pack("<BI", flags, seq) + struct.pack(">H", 0)
    gps = struct.pack("<BBII", 0, 0, 0, 0)
    inter = np.empty(2 * len(iq), dtype=">i2")
    inter[0::2] = np.round(iq.real * 32768).astype(np.int16)
    inter[1::2] = np.round(iq.imag * 32768).astype(np.int16)
    return header + gps + inter.tobytes()


def test_parse_msg():
    info = parse_msg(b"audio_init=0 audio_rate=12000 sample_rate=12001.135")
    assert info["audio_rate"] == "12000"
    assert parse_msg(b"too_busy=4")["too_busy"] == "4"


def test_parse_snd_iq_roundtrip():
    iq = (np.array([0.25, -0.5, 0.125]) + 1j * np.array([-0.25, 0.5, 0.0]))
    seq, samples = parse_snd_iq(make_snd_body(7, iq))
    assert seq == 7
    np.testing.assert_allclose(samples.real, iq.real, atol=1e-4)
    np.testing.assert_allclose(samples.imag, iq.imag, atol=1e-4)


def test_parse_snd_rejects_short():
    assert parse_snd_iq(b"\x00" * 5) == (-1, None)


def test_parse_snd_rejects_non_iq_streams():
    # A full Kiwi camps extra connections onto another user's channel and
    # streams their mono/ADPCM audio; parsed as raw IQ it is full-scale
    # noise (seen live as solid-white 12 kHz waterfall bands).
    iq = np.full(64, 0.5) + 0j
    compressed = make_snd_body(3, iq, flags=SND_FLAG_STEREO | SND_FLAG_COMPRESSED)
    assert parse_snd_iq(compressed) == (3, None)
    mono = make_snd_body(4, iq, flags=0)
    assert parse_snd_iq(mono) == (4, None)


def test_stream_commands_iq_mode():
    cmds = client_stream_commands(12000, 14_030_000, 11800)
    assert any("compression=0" in c for c in cmds)
    assert any("mod=iq" in c and "freq=14030.000" in c for c in cmds)


def _tone(fs, freq, n, amp=0.5):
    t = np.arange(n) / fs
    return (amp * np.exp(2j * np.pi * freq * t)).astype(np.complex64)


def _spectrum_peak_hz(x, fs):
    spec = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(len(x)))))
    return (np.argmax(spec) - len(x) / 2) * fs / len(x)


def test_stitcher_maps_slice_tones_to_virtual_frequencies():
    st = SliceStitcher(n_slices=3, slice_rate=12000)
    assert st.sample_rate == 36000
    n = 12000  # 1 s per slice
    # slice 0 (offset -12 kHz): tone at +2 kHz -> virtual -10 kHz
    # slice 1 (offset 0):       tone at -3 kHz -> virtual  -3 kHz
    # slice 2 (offset +12 kHz): tone at +4 kHz -> virtual +16 kHz
    for idx, f in ((0, 2000), (1, -3000), (2, 4000)):
        st.push(idx, _tone(12000, f, n))
    out = st.pull()
    assert len(out) == 36000
    settled = out[4000:]  # skip filter warm-up
    spec = np.abs(np.fft.fftshift(np.fft.fft(settled * np.hanning(len(settled)))))
    freqs = (np.arange(len(settled)) - len(settled) / 2) * 36000 / len(settled)
    expected = (-10000, -3000, 16000)
    residual = spec.copy()
    for expect in expected:
        residual[np.abs(freqs - expect) < 50] = 0
    for expect in expected:
        window = np.abs(freqs - expect) < 50
        # each tone present at the mapped frequency, well above anything else
        assert spec[window].max() > 100 * residual.max()
        peak = freqs[np.argmax(np.where(window, spec, 0))]
        assert abs(peak - expect) < 10


def test_stitcher_amplitude_preserved():
    st = SliceStitcher(n_slices=3, slice_rate=12000)
    st.push(1, _tone(12000, -3000, 12000, amp=0.5))
    st.push(0, np.zeros(12000, dtype=np.complex64))
    st.push(2, np.zeros(12000, dtype=np.complex64))
    out = st.pull()
    rms = np.sqrt(np.mean(np.abs(out[4000:]) ** 2))
    assert abs(rms - 0.5) < 0.05


def test_stitcher_survives_dead_slice():
    st = SliceStitcher(n_slices=3, slice_rate=12000)
    st.push(0, _tone(12000, 1000, 6000))
    st.push(1, _tone(12000, 1000, 6000))
    # slice 2 never delivers -> nothing can be emitted yet
    assert st.pull() is None
    st.mark_dead(2)
    out = st.pull()
    assert out is not None and len(out) == 18000
    assert st.alive_count == 2


def test_stitcher_streaming_continuity():
    """Pushing in small blocks must equal one big push (state carry-over)."""
    st_a = SliceStitcher(n_slices=2, slice_rate=12000)
    st_b = SliceStitcher(n_slices=2, slice_rate=12000)
    x = _tone(12000, 1500, 4096)
    zeros = np.zeros(4096, dtype=np.complex64)
    st_a.push(0, x)
    st_a.push(1, zeros)
    out_a = st_a.pull()
    for i in range(0, 4096, 512):
        st_b.push(0, x[i:i + 512])
        st_b.push(1, zeros[:512])
    out_b = st_b.pull()
    np.testing.assert_allclose(out_a, out_b, atol=1e-5)


def test_stream_commands_use_agc():
    # fixed manual gain lets a strong in-slice carrier clip the s16 stream
    cmds = client_stream_commands(12000, 14_030_000, 11800)
    assert any(c.startswith("SET agc=1 hang=1") for c in cmds)


def test_stitcher_revive_after_dead():
    st = SliceStitcher(n_slices=2, slice_rate=12000)
    st.push(0, _tone(12000, 1000, 6000))
    st.mark_dead(1)
    out = st.pull()
    assert out is not None and len(out) == 12000
    assert st.alive_slices == [0]
    # slice 1 reconnects and starts delivering again
    st.revive(1)
    assert st.alive_slices == [0, 1]
    st.push(0, np.zeros(6000, dtype=np.complex64))
    st.push(1, _tone(12000, -2000, 6000))
    out = st.pull()
    assert out is not None and len(out) == 12000
    # the revived slice's tone appears at its virtual frequency (+6k-2k=4k)
    assert abs(_spectrum_peak_hz(out[4000:], 24000) - 4000) < 50
