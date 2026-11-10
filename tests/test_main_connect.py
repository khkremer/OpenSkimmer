"""Band connect retries while radiod's mDNS names are not up yet."""

import socket

from openskimmer import main as main_mod


class _FlakySource:
    def __init__(self, fail_times, exc=None):
        self.calls = 0
        self.fail_times = fail_times
        self.exc = exc or socket.gaierror(-2, "Name or service not known")

    def connect(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc


def test_connect_retries_until_name_resolves(monkeypatch):
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)
    src = _FlakySource(fail_times=3)
    main_mod._connect_retrying_dns(src, "ka9q/7050000", timeout_s=60)
    assert src.calls == 4


def test_connect_gives_up_after_timeout(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main_mod.time, "sleep",
                        lambda s: clock.__setitem__(0, clock[0] + s))
    src = _FlakySource(fail_times=10**6)
    try:
        main_mod._connect_retrying_dns(src, "ka9q/7050000", timeout_s=30)
    except socket.gaierror:
        pass
    else:
        raise AssertionError("expected gaierror")
    assert clock[0] <= 30 and src.calls > 1


def test_connect_other_errors_fail_at_once(monkeypatch):
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)
    src = _FlakySource(fail_times=1, exc=ConnectionRefusedError())
    try:
        main_mod._connect_retrying_dns(src, "kiwi/7030000", timeout_s=60)
    except ConnectionRefusedError:
        pass
    assert src.calls == 1
