"""Telnet spot server protocol tests (F6): handshake, streaming, sh/dx."""

import asyncio

from openskimmer.spotting.spotter import Spot
from openskimmer.spotting.telnet import TelnetSpotServer, format_spot


def spot(call, khz, mode="CW"):
    return Spot(freq_hz=khz * 1000.0, call=call, snr_db=20, wpm=25,
                activity="CQ", mode=mode, time_utc=1_752_500_000.0)


async def _readfor(r, want, timeout=2.0):
    """Read lines until one contains `want` (skips interleaved prompts and
    streamed spots); returns that line."""
    while True:
        line = (await asyncio.wait_for(r.readline(), timeout)).decode()
        if want in line:
            return line


async def _session():
    srv = TelnetSpotServer("N0CALL", host="127.0.0.1", port=0,
                           op_name="Pat", grid="FN31pr",
                           segments_fn=lambda: [(14_000_000.0, 14_070_000.0)])
    await srv.start()
    for s in (spot("W1AW", 14030.0), spot("K4RLC", 14041.5)):
        srv.broadcast(s)  # pre-login spots land in history only

    r, w = await asyncio.open_connection("127.0.0.1", srv.port)
    banner = await r.readuntil(b"callsign:")
    assert (b"Skimmer Server" in banner and b"N0CALL" in banner
            and b"Pat" in banner)

    # login; CW Skimmer-style prompt reprints after every turn
    w.write(b"N0CALL\r\n")
    assert "N0CALL de SKIMMER" in await _readfor(r, "CwSkimmer >")

    # a live spot streams to the connected client
    srv.broadcast(spot("DL1ABC", 14022.2))
    line = await _readfor(r, "DL1ABC")
    assert "20 dB" in line and "25 WPM" in line

    # sh/dx replays history; mode filter returns only the CW spot
    w.write(b"sh/dx 2\r\n")
    assert "K4RLC" in await _readfor(r, "K4RLC")
    w.write(b"sh/dx 1 cw\r\n")
    assert "DL1ABC" in await _readfor(r, "DX de")

    # SETT reports the skimmed segments, CW Skimmer style
    w.write(b"SKIMMER/SETT\r\n")   # the Aggregator's actual command
    assert "vlNormal 14000.0-14070.0" in await _readfor(r, "SETT: vlNormal")

    # an unknown command (the Aggregator sends PASSWORD) is answered, not
    # ignored, so its command/prompt handshake keeps advancing
    w.write(b"PASSWORD\r\n")
    assert "PASSWORD" in await _readfor(r, "Unknown command")

    w.write(b"bye\r\n")
    await srv.stop()


def test_telnet_session():
    asyncio.run(_session())


def test_open_mode_skips_login():
    async def scenario():
        srv = TelnetSpotServer("N0CALL", host="127.0.0.1", port=0,
                               ask_call=False)
        await srv.start()
        r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        assert "GUEST de SKIMMER" in await _readfor(r, "CwSkimmer >")
        w.close()
        await srv.stop()
    asyncio.run(scenario())


def test_primary_skimmer_handshake():
    # reproduce the RBN Aggregator's PRIMARY sequence: log in with a call,
    # send PASSWORD then SETT, each answered with a reprinted prompt; the
    # SETT reply is required or Aggregator won't forward.
    async def scenario():
        srv = TelnetSpotServer("N0CALL", host="127.0.0.1", port=0,
                               segments_fn=lambda: [(7_000_000.0, 7_070_000.0)])
        await srv.start()
        r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        await r.readuntil(b"callsign:")
        w.write(b"N0CALL\r\n")
        assert "N0CALL de SKIMMER" in await _readfor(r, "CwSkimmer >")
        w.write(b"PASSWORD\r\n")
        assert "Unknown command" in await _readfor(r, "Unknown command")
        assert "CwSkimmer >" in await _readfor(r, "CwSkimmer >")
        w.write(b"SKIMMER/SETT\r\n")
        assert "vlNormal 7000.0-7070.0" in await _readfor(r, "SETT: vlNormal")
        # spots stream to the primary too
        srv.broadcast(spot("W1AW", 7025.0))
        assert "W1AW" in await _readfor(r, "DX de")
        w.close()
        await srv.stop()
    asyncio.run(scenario())


def test_format_spot_fields():
    line = format_spot(spot("W1AW", 14030.0), "N0CALL")
    assert line.startswith("DX de N0CALL-#:")
    assert "14030.0" in line and "W1AW" in line
    assert "20 dB" in line and "25 WPM" in line and line.endswith("Z")


def test_format_matches_rbn_aggregator():
    # byte-identical to lines the RBN Aggregator accepted in a live traffic
    # log (it rejects anything else: "Not in Skimmer spot format", e.g. a
    # mode column between call and SNR)
    s = Spot(freq_hz=14028_900.0, call="K6AR", snr_db=6, wpm=37,
             activity="CQ", time_utc=1752586500.0)
    assert format_spot(s, "N0XYZ") == (
        "DX de N0XYZ-#:   14028.9  K6AR            6 dB  37 WPM"
        "  CQ            1335Z")
    s = Spot(freq_hz=14040_400.0, call="N1LN", snr_db=5, wpm=34,
             activity="", time_utc=1752586560.0)
    assert format_spot(s, "N0XYZ") == (
        "DX de N0XYZ-#:   14040.4  N1LN            5 dB  34 WPM"
        "                1336Z")


def test_repetition_only_spots_are_not_published_as_cq():
    # A DUPE is a call seen repeating with no CQ/DE grammar -- in a contest
    # that is an S&P station answering someone else's CQ, not a CQ caller.
    # Publishing these as CQ put 44.8% of a day's spots beyond confirmation
    # by any other RBN skimmer (last of 175; median 1.6%), with 97.5% of them
    # landing within 0.5 kHz of a station others had spotted running there.
    # They must reach the local feed but never the RBN as CQ.
    s = Spot(freq_hz=14028_900.0, call="K6AR", snr_db=6, wpm=37,
             activity="DUPE", time_utc=1752586500.0)
    line = format_spot(s, "N0CALL")
    assert "  CQ  " not in line
    assert "DE" in line
    # genuine CQ and DE spots are untouched
    for act in ("CQ", "DE"):
        s2 = Spot(freq_hz=14028_900.0, call="K6AR", snr_db=6, wpm=37,
                  activity=act, time_utc=1752586500.0)
        assert f" {act} " in format_spot(s2, "N0CALL")


def test_banner_matches_cw_skimmer_format():
    from openskimmer.spotting.telnet import SKIMMER_VERSION
    srv = TelnetSpotServer("N0CALL", op_name="Pat", grid="FN31PR",
                           qth="Newington, CT - USA")
    assert srv._banner().decode() == (
        "Welcome to the Skimmer Server Telnet cluster port!\r\n"
        f"Skimmer Server v.{SKIMMER_VERSION} is operated by Pat, "
        "N0CALL\r\n"
        "in Newington, CT - USA (FN31PR)\r\n"
        "Please enter your callsign:\r\n")


def test_stop_returns_with_a_client_still_connected():
    # Observed live: the host rebooted, every band failed because radiod's mDNS
    # names did not resolve yet, and the skimmer should have exited non-zero
    # so systemd restarts it. It hung in stop() instead: since Python
    # 3.12.1, Server.wait_closed() waits for every client connection, and
    # the RBN Aggregator never disconnects. 13 hours with no spots while
    # systemd reported the unit active.
    async def go():
        srv = TelnetSpotServer("N0CALL", host="127.0.0.1", port=0)
        await srv.start()
        r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        await r.readuntil(b"callsign:")
        w.write(b"N0CALL\r\n")
        await _readfor(r, "CwSkimmer >")
        await asyncio.wait_for(srv.stop(), 3.0)
        w.close()

    asyncio.run(go())
