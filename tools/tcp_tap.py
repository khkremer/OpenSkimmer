#!/usr/bin/env python3
"""Logging TCP proxy: sit between two programs and record both directions.

Insert this between the RBN Aggregator and a real skimmer (or OpenSkimmer)
to capture the exact handshake. It listens locally, forwards every
connection to a target host:port, and logs each line each side sends,
tagged with direction and a timestamp -- so we can see precisely what the
Aggregator asks for and how CW Skimmer Server replies.

  connecting client (Aggregator)  --> [ this tap ] -->  target (skimmer)
                                  <--              <--

Typical use (Aggregator points its skimmer at 127.0.0.1:7300; real CW
Skimmer Server runs on another host/port):

  python3 tools/tcp_tap.py --listen 0.0.0.0:7300 --target 192.0.2.30:7300 \
      --log handshake.log

Forwarding is never delayed; a line with no newline yet (a prompt) is
logged after a short idle so it still shows up. Stdlib only, no venv.
"""

import argparse
import asyncio
import sys
import time

_log_file = None


def _hostport(s: str, default_port: int) -> tuple:
    if ":" in s:
        host, port = s.rsplit(":", 1)
        return host or "0.0.0.0", int(port)
    return s, default_port


def _render(data: bytes) -> str:
    text = data.rstrip(b"\r\n").decode("latin-1")
    return "".join(c if 32 <= ord(c) < 127 else f"\\x{ord(c):02x}"
                   for c in text)


def _emit(conn: int, arrow: str, data: bytes, partial: bool = False) -> None:
    ms = int(time.time() * 1000)
    ts = time.strftime("%H:%M:%S", time.localtime(ms / 1000)) + f".{ms % 1000:03d}"
    tag = " (partial)" if partial else ""
    msg = f"{ts} #{conn} {arrow} {_render(data)}{tag}"
    print(msg, flush=True)
    if _log_file:
        _log_file.write(msg + "\n")
        _log_file.flush()


async def _pump(reader, writer, conn: int, arrow: str) -> None:
    buf = b""
    try:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=0.25)
            except asyncio.TimeoutError:
                if buf:                      # a prompt with no trailing newline
                    _emit(conn, arrow, buf, partial=True)
                    buf = b""
                continue
            if not data:
                break
            writer.write(data)               # forward immediately
            await writer.drain()
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                _emit(conn, arrow, line)
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        if buf:
            _emit(conn, arrow, buf, partial=True)
        try:
            writer.close()
        except Exception:
            pass


def make_handler(target_host: str, target_port: int):
    counter = {"n": 0}

    async def handle(creader, cwriter):
        counter["n"] += 1
        conn = counter["n"]
        peer = cwriter.get_extra_info("peername")
        try:
            sreader, swriter = await asyncio.open_connection(
                target_host, target_port)
        except Exception as e:
            print(f"#{conn} target connect failed: {e}", file=sys.stderr)
            cwriter.close()
            return
        print(f"#{conn} open: {peer} <-> {target_host}:{target_port}",
              flush=True)
        await asyncio.gather(
            _pump(creader, swriter, conn, "-->"),   # client -> target
            _pump(sreader, cwriter, conn, "<--"),   # target -> client
        )
        print(f"#{conn} closed", flush=True)

    return handle


async def main() -> int:
    global _log_file
    ap = argparse.ArgumentParser(description="logging TCP proxy / tap")
    ap.add_argument("--listen", default="0.0.0.0:7300",
                    help="local HOST:PORT to accept on (default 0.0.0.0:7300)")
    ap.add_argument("--target", default="192.0.2.30:7300",
                    help="HOST:PORT to forward to (default 192.0.2.30:7300)")
    ap.add_argument("--log", help="also append the transcript to this file")
    args = ap.parse_args()

    lhost, lport = _hostport(args.listen, 7300)
    thost, tport = _hostport(args.target, 7300)
    if args.log:
        _log_file = open(args.log, "a")

    server = await asyncio.start_server(
        make_handler(thost, tport), lhost, lport)
    print(f"tap: listening on {lhost}:{lport} -> {thost}:{tport}  "
          f"(--> to target, <-- from target)", flush=True)
    async with server:
        await server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
