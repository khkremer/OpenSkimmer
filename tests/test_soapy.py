"""SoapySource tests using a fake SoapySDR module (no hardware needed)."""

import sys
import types

import numpy as np
import pytest


def _fake_soapy(read_script, setting_keys=("agc_setpoint", "biasT_ctrl")):
    """Build a fake SoapySDR module. read_script: list of (ret, samples)."""
    mod = types.ModuleType("SoapySDR")
    mod.SOAPY_SDR_RX = 1
    mod.SOAPY_SDR_CF32 = "CF32"
    mod.SOAPY_SDR_OVERFLOW = -4
    mod.SOAPY_SDR_TIMEOUT = -1
    mod.errToStr = lambda n: f"err{n}"

    class _Ret:
        def __init__(self, ret):
            self.ret = ret
            self.flags = 0

    class _Dev:
        instances = []

        def __init__(self, args):
            self.args = args
            self.calls = {}
            self._script = list(read_script)
            _Dev.instances.append(self)

        def _rec(self, name, *a):
            self.calls.setdefault(name, []).append(a)

        _rate = [0]
        def setSampleRate(self, d, ch, rate):
            self._rec("setSampleRate", d, ch, rate); _Dev._rate[0] = rate
        def getSampleRate(self, *a): return _Dev._rate[0]
        def setFrequency(self, *a): self._rec("setFrequency", *a)
        def setBandwidth(self, *a): self._rec("setBandwidth", *a)
        def setGain(self, *a): self._rec("setGain", *a)
        def setGainMode(self, *a): self._rec("setGainMode", *a)
        def setAntenna(self, *a): self._rec("setAntenna", *a)
        def setFrequencyCorrection(self, *a): self._rec("setFreqCorr", *a)
        def writeSetting(self, *a): self._rec("writeSetting", *a)
        def listBandwidths(self, *a): return [200000.0, 300000.0, 600000.0]
        def hasGainMode(self, *a): return True
        def getHardwareKey(self): return "RSP1"

        def getSettingInfo(self):
            return [types.SimpleNamespace(key=k) for k in setting_keys]
        def setupStream(self, *a): return "STREAM"
        def activateStream(self, *a): self._rec("activate", *a)
        def deactivateStream(self, *a): self._rec("deactivate", *a)
        def closeStream(self, *a): self._rec("close", *a)

        def readStream(self, stream, buffs, n, timeoutUs=0):
            if not self._script:
                return _Ret(mod.SOAPY_SDR_TIMEOUT)
            ret, samples = self._script.pop(0)
            if samples is not None:
                buffs[0][:len(samples)] = samples
            return _Ret(ret)

    mod.Device = _Dev
    mod.Device.unmake = staticmethod(lambda d: None)
    return mod


@pytest.fixture
def install_fake(monkeypatch):
    def _install(read_script, setting_keys=("agc_setpoint", "biasT_ctrl")):
        mod = _fake_soapy(read_script, setting_keys)
        monkeypatch.setitem(sys.modules, "SoapySDR", mod)
        return mod
    return _install


def test_configure_agc_defaults(install_fake):
    from openskimmer.sdr.soapy import SoapySource
    mod = install_fake([])
    src = SoapySource(driver="sdrplay", agc_setpoint_dbfs=-30)
    src.set_sample_rate(192000)
    src.set_center_frequency(14_030_000)
    src.connect()
    dev = mod.Device.instances[-1]
    assert dev.args["driver"] == "sdrplay"
    assert dev.calls["setSampleRate"][0][2] == 192000.0
    assert dev.calls["setFrequency"][-1][2] == 14_030_000.0
    assert dev.calls["setGainMode"][0][2] is True          # AGC on
    assert ("agc_setpoint", "-30") in dev.calls["writeSetting"]
    assert dev.calls["setBandwidth"][0][2] == 200000.0     # smallest >= 0.8*sr


def test_sample_rate_readback_on_clamp(install_fake):
    # RTL-SDR-style: device clamps 192k to its nearest valid rate; the source
    # must report the actual rate so the pipeline's freq mapping is right
    from openskimmer.sdr.soapy import SoapySource
    mod = install_fake([])
    mod.Device.getSampleRate = lambda self, *a: 250000.0   # device forces this
    src = SoapySource(driver="rtlsdr")
    src.set_sample_rate(192000)
    src.connect()
    assert src.sample_rate == 250000


def test_manual_gain_disables_agc(install_fake):
    from openskimmer.sdr.soapy import SoapySource
    mod = install_fake([])
    src = SoapySource(driver="sdrplay", gain_db=40)
    src.connect()
    dev = mod.Device.instances[-1]
    assert dev.calls["setGainMode"][0][2] is False
    assert dev.calls["setGain"][0][2] == 40.0


def test_device_args_serial_and_kv(install_fake):
    from openskimmer.sdr.soapy import SoapySource
    mod = install_fake([])
    SoapySource(device_args="1234ABCD").connect()
    assert mod.Device.instances[-1].args["serial"] == "1234ABCD"
    mod = install_fake([])
    SoapySource(device_args="serial=X,rfnotch_ctrl=false").connect()
    a = mod.Device.instances[-1].args
    assert a["serial"] == "X" and a["rfnotch_ctrl"] == "false"


def test_stream_yields_and_handles_status(install_fake):
    from openskimmer.sdr.soapy import SoapySource
    tone = np.ones(8, dtype=np.complex64)
    script = [(8, tone),                       # good read
              (-4, None),                      # overflow (ignored)
              (-1, None),                      # timeout (continue)
              (4, tone[:4]),                   # good read, short
              (-99, None)]                     # fatal -> stop
    mod = install_fake(script)
    src = SoapySource(driver="sdrplay")
    src.connect()
    blocks = list(src.stream_iq())
    assert [len(b) for b in blocks] == [8, 4]
    dev = mod.Device.instances[-1]
    assert "deactivate" in dev.calls and "close" in dev.calls


def test_bias_t_forced_off_by_default(install_fake):
    from openskimmer.sdr.soapy import SoapySource
    mod = install_fake([])
    SoapySource(driver="sdrplay").connect()          # bias_t default False
    dev = mod.Device.instances[-1]
    assert ("biasT_ctrl", "false") in dev.calls["writeSetting"]


def test_bias_t_enabled_when_requested(install_fake):
    from openskimmer.sdr.soapy import SoapySource
    mod = install_fake([])
    SoapySource(driver="sdrplay", bias_t=True).connect()
    dev = mod.Device.instances[-1]
    assert ("biasT_ctrl", "true") in dev.calls["writeSetting"]


def test_bias_t_absent_is_noop(install_fake):
    # a device without the setting (RSP1) gets no biasT_ctrl write
    from openskimmer.sdr.soapy import SoapySource
    mod = install_fake([], setting_keys=("agc_setpoint",))
    SoapySource(driver="sdrplay").connect()
    dev = mod.Device.instances[-1]
    assert not any(a[0] == "biasT_ctrl"
                   for a in dev.calls.get("writeSetting", []))


def test_teardown_neutralizes_finalizer(install_fake):
    # after our explicit unmake, the object's __del__/close must be a no-op
    # so the SWIG binding doesn't unmake twice ("unknown device")
    from openskimmer.sdr.soapy import _safe_unmake
    mod = install_fake([])
    calls = {"unmake": 0}
    mod.Device.unmake = staticmethod(
        lambda d: calls.__setitem__("unmake", calls["unmake"] + 1))
    dev = mod.Device({})
    _safe_unmake(mod, dev)
    assert calls["unmake"] == 1
    dev.close()                 # simulates the finalizer
    assert calls["unmake"] == 1  # not called again


def test_missing_module_message(monkeypatch):
    from openskimmer.sdr.soapy import SoapySource
    monkeypatch.setitem(sys.modules, "SoapySDR", None)  # force ImportError
    with pytest.raises(RuntimeError, match="SoapySDR"):
        SoapySource().connect()


def test_open_retries_while_previous_owner_releases(install_fake, monkeypatch):
    # Observed live: a restart reopened the RSP1 before the old process
    # had released it; the first opens fail, a later one succeeds.
    from openskimmer.sdr import soapy
    mod = install_fake([])
    real = mod.Device
    fails = [2]

    def flaky(args):
        if fails[0]:
            fails[0] -= 1
            raise RuntimeError("no available RSP devices found")
        return real(args)
    flaky.unmake = real.unmake
    mod.Device = flaky
    monkeypatch.setattr(soapy.time, "sleep", lambda s: None)
    src = soapy.SoapySource(driver="sdrplay")
    src.connect()
    assert fails[0] == 0 and src._dev is real.instances[-1]


def test_open_gives_up_after_timeout(install_fake, monkeypatch):
    from openskimmer.sdr import soapy
    mod = install_fake([])
    calls = [0]

    def never(args):
        calls[0] += 1
        raise RuntimeError("no available RSP devices found")
    never.unmake = mod.Device.unmake
    mod.Device = never
    clock = [0.0]
    monkeypatch.setattr(soapy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(soapy.time, "sleep",
                        lambda s: clock.__setitem__(0, clock[0] + s))
    with pytest.raises(ConnectionError, match="no available RSP"):
        soapy.SoapySource(driver="sdrplay").connect()
    assert 1 < calls[0] and clock[0] <= soapy.OPEN_RETRY_S
