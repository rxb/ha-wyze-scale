"""Detect Scale Ultra activity from its faster, unchanged advertisements."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque

from .ultra_timing import _open_channel

_LOGGER = logging.getLogger(__name__)


def advertisement_indexes(packet: bytes, target: bytes):
    """Return adapters reporting complete target legacy advertisements."""
    if len(packet) < 10:
        return []
    opcode, index, size = struct.unpack_from("<HHH", packet)
    data = packet[6:]
    if opcode != 3 or len(data) != size or len(data) < 4:
        return []
    if data[0] != 0x3E or data[1] != len(data) - 2 or data[2] != 2:
        return []
    pos = 4
    matches = []
    for _ in range(data[3]):
        if pos + 9 > len(data):
            return []
        length = data[pos + 8]
        end = pos + 10 + length
        if end > len(data):
            return []
        # Exclude scan responses; count actual connectable advertisements.
        if data[pos] == 0 and data[pos + 1] == 0 and data[pos + 2 : pos + 8] == target:
            matches.append(index)
        pos = end
    return matches if pos == len(data) else []


class UltraWakeupMonitor:
    """Observe only; never change adapter settings or initiate connections."""

    def __init__(self, address, on_activity):
        self._target = bytes.fromhex(address.replace(":", ""))[::-1]
        self._on_activity = on_activity
        self._monitor = None
        self._task = None
        self._windows = {}
        self._active = set()

    async def start(self):
        self._monitor = _open_channel(2)
        self._task = asyncio.create_task(self._observe(), name="wyze-ultra-wakeup")
        _LOGGER.info("Ultra advertisement activity monitor started")

    def process(self, packet, now):
        for index in advertisement_indexes(packet, self._target):
            window = self._windows.setdefault(index, deque())
            # Idle reports ~1.5s apart; weighing ~8 reports/sec in capture.
            if window and now - window[-1] > 1.5:
                self._active.discard(index)
            while window and now - window[0] > 1.0:
                window.popleft()
            window.append(now)
            if len(window) >= 4 and index not in self._active:
                self._active.add(index)
                self._on_activity()
            while len(window) > 16:
                window.popleft()

    async def _observe(self):
        try:
            loop = asyncio.get_running_loop()
            while True:
                packet = await loop.sock_recv(self._monitor, 65535)
                self.process(packet, time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Ultra advertisement activity monitor failed")
        finally:
            self._monitor.close()
            self._monitor = None

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._monitor is not None:
            self._monitor.close()
            self._monitor = None
