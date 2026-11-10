"""SoapySDR backend: any receiver with a SoapySDR driver.

Vendor-neutral by design -- the same class drives an SDRplay RSP
(``driver=sdrplay``), a Mirics clone (``driver=miri``), an RTL-SDR, an
Airspy, etc. -- so the project gains those radios without a backend each
(requirement 4: hardware-agnostic). It is a single-tuner source: one
contiguous span, full coverage, no PTT.

SDRplay notes (the initial target, incl. the RSP1 / Mirics clones the
sdrplay_api service accepts as an RSP1): gain is expressed as IF gain
*reduction* (higher = less gain); AGC with a dBFS setpoint is the sane
skimmer default. Sample rates like 192 kHz are native, so no host-side
decimation is needed. The proprietary ``sdrplay_api`` service must be
installed and running for the sdrplay driver; the miri driver needs none.

Requires the ``SoapySDR`` Python module (distro package python3-soapysdr)
plus the driver module for the device.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

import numpy as np

from .base import Capabilities, SDRSource

log = logging.getLogger(__name__)

READ_CHUNK = 1 << 16        # samples requested per readStream call
READ_TIMEOUT_US = 500_000
# A restart can reopen the radio before the previous process has let go of
# it: in one observed case the sdrplay_api service logged the old
# stream's exit 3 s AFTER the new process had already failed with "no
# available RSP devices found", and that band stayed dead until a manual
# stop/start. Keep retrying the open for this long before giving up.
OPEN_RETRY_S = 20.0


def _safe_unmake(SoapySDR, dev) -> None:
    """Release a Soapy device once and only once. The SWIG binding's
    finalizer (__del__ -> close -> unmake) unmakes again at GC, which then
    raises "unknown device"; neutralize that finalizer after our explicit
    unmake so it doesn't print an ignored-exception traceback."""
    try:
        SoapySDR.Device.unmake(dev)
    except Exception:
        pass
    try:
        dev.close = lambda: None
    except Exception:
        pass


class SoapySource(SDRSource):
    def __init__(self, driver: str = "sdrplay", device_args: str = "",
                 gain_db: Optional[float] = None, agc: bool = True,
                 agc_setpoint_dbfs: int = -30, antenna: Optional[str] = None,
                 ppm: float = 0.0, bias_t: bool = False):
        self._driver = driver
        self._device_args = device_args
        self._gain_db = gain_db          # None -> use AGC / driver default
        self._agc = agc and gain_db is None
        self._agc_setpoint = agc_setpoint_dbfs
        self._antenna = antenna
        self._ppm = ppm
        self._bias_t = bias_t            # DC power on the antenna port
        self._center_frequency = 14_030_000
        self._sample_rate = 192000
        self._dev = None
        self._SoapySDR = None
        self._running = False
        self._streamed = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=f"soapy:{self._driver}",
            max_sample_rate=self._sample_rate,
            sample_rates=(self._sample_rate,),
            max_channels=1,
            exclusive=True,
        )

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        try:
            import SoapySDR
        except ImportError as e:
            raise RuntimeError(
                "SoapySDR backend requires the 'SoapySDR' Python module "
                "(apt install python3-soapysdr -- it is a compiled system "
                "package, not pip-installable) plus the driver module for "
                f"'{self._driver}'. In a virtualenv it is not visible unless "
                "the venv was created with --system-site-packages or the "
                "SoapySDR.py and _SoapySDR*.so files are symlinked in "
                "(see INSTALL-linux.md).") from e
        self._SoapySDR = SoapySDR
        args = dict(driver=self._driver)
        for part in self._device_args.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                args[k.strip()] = v.strip()
            elif part.strip():
                args["serial"] = part.strip()   # bare token = serial
        try:
            self._dev = self._open_retrying(SoapySDR, args)
        except Exception as e:
            raise ConnectionError(
                f"SoapySDR could not open driver={self._driver} ({args}): {e}. "
                "Check the device is attached and its driver/service is up "
                "(SoapySDRUtil --find).") from e
        self._configure()
        info = self._dev.getHardwareKey()
        log.info("SoapySDR: opened %s (driver=%s)", info, self._driver)

    @staticmethod
    def _open_retrying(SoapySDR, args, timeout_s: Optional[float] = None):
        deadline = time.monotonic() + (OPEN_RETRY_S if timeout_s is None
                                       else timeout_s)
        delay = 1.0
        while True:
            try:
                return SoapySDR.Device(args)
            except Exception as e:
                if time.monotonic() + delay > deadline:
                    raise
                log.warning("SoapySDR: open %s failed (%s); retrying in %.0f s",
                            args, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    def _configure(self) -> None:
        S = self._SoapySDR
        dev = self._dev
        RX = S.SOAPY_SDR_RX
        dev.setSampleRate(RX, 0, float(self._sample_rate))
        # some drivers (RTL-SDR: valid rates ~250k-3.2M) clamp the requested
        # rate; read back the actual one so the pipeline's frequency mapping
        # matches the device instead of what we asked for
        try:
            actual = int(round(dev.getSampleRate(RX, 0)))
            if actual and actual != self._sample_rate:
                log.info("SoapySDR: device using %d Hz (requested %d)",
                         actual, self._sample_rate)
                self._sample_rate = actual
        except Exception:
            pass
        # IF filter just wide enough for the span (avoids aliasing without
        # pulling in extra adjacent-band energy)
        try:
            bws = [bw for bw in dev.listBandwidths(RX, 0)
                   if bw >= self._sample_rate * 0.8]
            dev.setBandwidth(RX, 0, min(bws) if bws else self._sample_rate)
        except Exception:
            pass
        if self._ppm:
            try:
                dev.setFrequencyCorrection(RX, 0, self._ppm)
            except Exception:
                log.debug("SoapySDR: driver has no frequency correction")
        if self._antenna:
            dev.setAntenna(RX, 0, self._antenna)
        if dev.hasGainMode(RX, 0):
            dev.setGainMode(RX, 0, self._agc)   # True = hardware AGC
        if self._agc:
            try:
                dev.writeSetting("agc_setpoint", str(self._agc_setpoint))
            except Exception:
                pass
        elif self._gain_db is not None:
            dev.setGain(RX, 0, float(self._gain_db))
        # Force the antenna-port bias-T to the requested state on every
        # connect (default off) so sticky state left by other software can't
        # put DC on the antenna. Only devices that expose the setting have
        # the hardware; the RSP1 does not, so this is a no-op there.
        try:
            keys = {s.key for s in dev.getSettingInfo()}
        except Exception:
            keys = set()
        if "biasT_ctrl" in keys:
            dev.writeSetting("biasT_ctrl", "true" if self._bias_t else "false")
            log.info("SoapySDR: bias-T %s", "ON" if self._bias_t else "off")
        elif self._bias_t:
            log.warning("SoapySDR: --bias-t requested but this device has no "
                        "bias-T control")
        dev.setFrequency(RX, 0, float(self._center_frequency))

    def disconnect(self) -> None:
        # Signal the reader to stop; the streaming thread tears the device
        # down in its finally so we never unmake() a device mid-readStream
        # (that can crash the native driver). Only unmake here if the stream
        # never ran.
        self._running = False
        if not self._streamed and self._dev is not None:
            self._unmake()

    def _unmake(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None and self._SoapySDR is not None:
            _safe_unmake(self._SoapySDR, dev)

    # -- configuration -------------------------------------------------------

    def set_center_frequency(self, hz: int) -> None:
        self._center_frequency = int(hz)
        if self._dev is not None:
            self._dev.setFrequency(self._SoapySDR.SOAPY_SDR_RX, 0, float(hz))

    def set_sample_rate(self, hz: int) -> None:
        self._sample_rate = int(hz)
        if self._dev is not None:
            self._dev.setSampleRate(self._SoapySDR.SOAPY_SDR_RX, 0, float(hz))

    # -- streaming -----------------------------------------------------------

    def stream_iq(self) -> Iterator[np.ndarray]:
        if self._dev is None:
            raise RuntimeError("connect() first")
        S = self._SoapySDR
        dev = self._dev
        stream = dev.setupStream(S.SOAPY_SDR_RX, S.SOAPY_SDR_CF32)
        dev.activateStream(stream)
        self._running = True
        self._streamed = True
        overflows = 0
        try:
            while self._running:
                buf = np.empty(READ_CHUNK, dtype=np.complex64)
                sr = dev.readStream(stream, [buf], READ_CHUNK,
                                    timeoutUs=READ_TIMEOUT_US)
                n = sr.ret
                if n > 0:
                    yield buf[:n]
                elif n == S.SOAPY_SDR_OVERFLOW:
                    overflows += 1
                    if overflows % 50 == 1:
                        log.warning("SoapySDR: stream overflow (host too slow "
                                    "or USB saturated); dropping samples")
                elif n == S.SOAPY_SDR_TIMEOUT:
                    continue
                elif n < 0:
                    log.warning("SoapySDR: readStream error %d (%s)",
                                n, S.errToStr(n))
                    break
        finally:
            self._running = False
            try:
                dev.deactivateStream(stream)
                dev.closeStream(stream)
            except Exception:
                pass
            self._unmake()   # same thread that used the device tears it down
