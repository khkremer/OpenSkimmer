from types import SimpleNamespace

from openskimmer.main import _band_offset


def test_band_offset_applies_only_to_its_band():
    args = SimpleNamespace(band_offset=["144075000=2330", "18110000=-500"])
    assert _band_offset(args, 144075000) == 2330
    assert _band_offset(args, 18110000) == -500
    assert _band_offset(args, 28050000) == 0


def test_band_offset_default_is_zero():
    assert _band_offset(SimpleNamespace(band_offset=[]), 144075000) == 0
