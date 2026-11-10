"""SDR backends. New receivers are added by implementing SDRSource and
registering here -- nothing in dsp/ or spotting/ may import from backends.
"""

from .base import Capabilities, SDRSource


def create_source(name: str, **kwargs) -> SDRSource:
    """Instantiate a backend by name. Imports lazily so that optional
    backend dependencies are only needed when actually used."""
    if name == "hl2":
        from .hl2 import HermesLite2Source
        return HermesLite2Source(**kwargs)
    if name == "synth":
        from .synth import SynthSource
        return SynthSource(**kwargs)
    if name == "file":
        from .fileiq import FileIQSource
        return FileIQSource(**kwargs)
    if name == "kiwi":
        from .kiwi import KiwiSDRSource
        return KiwiSDRSource(**kwargs)
    if name in ("soapy", "sdrplay", "miri", "rtlsdr", "airspy"):
        from .soapy import SoapySource
        if name != "soapy":
            kwargs.setdefault("driver", name)
        return SoapySource(**kwargs)
    if name == "ka9q":
        from .ka9q import Ka9qSource
        return Ka9qSource(**kwargs)
    raise ValueError(
        f"Unknown SDR backend: {name!r} "
        "(available: hl2, kiwi, sdrplay, ka9q, soapy, synth, file)")


__all__ = ["SDRSource", "Capabilities", "create_source"]
