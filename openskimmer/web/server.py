"""Waterfall web UI server (F5, phase 2).

Serves a self-contained single-page app and streams to it over WebSocket:
  - binary frames: one waterfall row each (uint8 pixels, n_cols wide)
  - JSON frames:   {"type": "config" | "spot" | "channels", ...}

The UI is a consumer of pipeline taps only; decoding never depends on it.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging
from typing import Optional, Set

import numpy as np

log = logging.getLogger(__name__)

MAX_COLS = 2048


def spectrum_to_row(power: np.ndarray, max_cols: int = MAX_COLS) -> bytes:
    """Power spectrum -> uint8 waterfall row.

    Bins are max-pooled down to <= max_cols so narrow CW carriers survive
    downsampling. Brightness maps [floor - 6 dB, floor + 42 dB] onto 0..255,
    with the floor taken as the median of the *covered* bins (zero-power
    bins are masked no-coverage regions and must not drag the floor down).
    """
    db = 10.0 * np.log10(power + 1e-30)
    factor = int(np.ceil(len(db) / max_cols))
    if factor > 1:
        pad = (-len(db)) % factor
        if pad:
            db = np.concatenate([db, np.full(pad, db[-1])])
        db = db.reshape(-1, factor).max(axis=1)
    covered = db[db > -250.0]
    floor = np.median(covered) if len(covered) else 0.0
    return (np.clip((db - (floor - 6.0)) * (255.0 / 48.0), 0, 255)
            .astype(np.uint8).tobytes())


def n_cols_for(fft_size: int, max_cols: int = MAX_COLS) -> int:
    factor = int(np.ceil(fft_size / max_cols))
    return int(np.ceil(fft_size / factor))


class WaterfallServer:
    def __init__(self, center_hz: float, span_hz: float, fft_size: int,
                 my_call: str, host: str = "0.0.0.0", port: int = 7380):
        self.host = host
        self.port = port
        self._config = {
            "type": "config",
            "center_hz": center_hz,
            "span_hz": span_hz,
            "n_cols": n_cols_for(fft_size),
            "my_call": my_call,
        }
        self._clients: Set[asyncio.Queue] = set()
        self._runner = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Waterfall UI on http://localhost:%d", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # -- broadcasting (call on the event loop) --------------------------------

    def _enqueue(self, item) -> None:
        for q in self._clients:
            if q.full():
                try:  # drop the oldest; spectrum rows are expendable
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(item)

    def broadcast_row(self, row: bytes) -> None:
        self._enqueue(row)

    def broadcast_json(self, obj: dict) -> None:
        self._enqueue(json.dumps(obj))

    # -- handlers -------------------------------------------------------------

    async def _index(self, request):
        from aiohttp import web
        html = (importlib.resources.files("openskimmer.web")
                / "static" / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html")

    async def _ws(self, request):
        from aiohttp import web
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._clients.add(q)
        log.info("Waterfall client connected (%d total)", len(self._clients))
        try:
            await ws.send_str(json.dumps(self._config))
            sender = asyncio.ensure_future(self._send_loop(ws, q))
            async for _ in ws:  # drain (and notice disconnect)
                pass
            sender.cancel()
        finally:
            self._clients.discard(q)
            log.info("Waterfall client disconnected (%d left)",
                     len(self._clients))
        return ws

    @staticmethod
    async def _send_loop(ws, q: asyncio.Queue) -> None:
        try:
            while True:
                item = await q.get()
                if isinstance(item, bytes):
                    await ws.send_bytes(item)
                else:
                    await ws.send_str(item)
        except (asyncio.CancelledError, ConnectionError):
            pass
