"""Temporary, device-specific Linux timing workaround for Scale Ultra.

Observe BlueZ loading the scale's preferred parameters before changing them.
Restore those exact parameters after the session. No global adapter changes.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import socket
import struct
import sys

_LOGGER = logging.getLogger(__name__)
_LOAD = 0x0035
_ORIGINAL = (7, 9, 0, 800)
_ULTRA = (24, 36, 2, 500)


def _open_channel(channel):
    if sys.platform != "linux":
        raise OSError("Experimental Ultra support requires local Linux Bluetooth")
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        address = struct.pack("HHH", socket.AF_BLUETOOTH, 65535, channel)
        libc = ctypes.CDLL(None, use_errno=True)
        libc.bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        libc.bind.restype = ctypes.c_int
        if libc.bind(sock.fileno(), address, len(address)) != 0:
            raise OSError(ctypes.get_errno(), "Cannot bind Ultra Bluetooth channel")
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


class UltraTimingSession:
    """One connection, restricted to the supplied Bluetooth address."""

    def __init__(self, address):
        self._target = bytes.fromhex(address.replace(":", ""))[::-1]
        self._monitor = None
        self._task = None
        self._active = None
        self._original = None
        self._changed = False
        self._index = None

    async def start(self):
        self._monitor = _open_channel(2)
        # Check command-channel access before starting a BLE connection.
        try:
            _open_channel(3).close()
        except BaseException:
            self._monitor.close()
            self._monitor = None
            raise
        self._task = asyncio.create_task(self._observe(), name="wyze-ultra-timing")

    async def _load(self, params):
        body = (
            struct.pack("<H", 1)
            + self._target
            + bytes([1])
            + struct.pack("<HHHH", *params)
        )
        packet = struct.pack("<HHH", _LOAD, self._index, len(body)) + body
        # Fresh socket avoids a full queue of unrelated management broadcasts.
        with _open_channel(3) as control:
            loop = asyncio.get_running_loop()
            async with asyncio.timeout(3):
                await loop.sock_sendall(control, packet)
                while True:
                    data = await loop.sock_recv(control, 65535)
                    if len(data) < 9:
                        continue
                    event, index, size = struct.unpack_from("<HHH", data)
                    if len(data) != size + 6:
                        continue
                    opcode = struct.unpack_from("<H", data, 6)[0]
                    if index == self._index and event in (1, 2) and opcode == _LOAD:
                        if data[8]:
                            raise RuntimeError(
                                f"Ultra timing command rejected: {data[8]}"
                            )
                        return

    async def _observe(self):
        loop = asyncio.get_running_loop()
        while True:
            packet = await loop.sock_recv(self._monitor, 65535)
            await self._process(packet)

    async def _process(self, packet):
        if len(packet) < 6:
            return
        opcode, index, size = struct.unpack_from("<HHH", packet)
        data = packet[6:]
        if len(data) != size:
            return
        if opcode == 3 and len(data) >= 14 and data[0] == 62 and data[2] in (1, 10):
            if data[3] == 0 and data[8:14] == self._target and self._active is None:
                self._active = struct.unpack_from("<H", data, 4)[0]
                self._index = index
                self._original = None
        if self._active is None or index != self._index:
            return
        if opcode == 16 and not self._changed:
            if (
                len(data) == 23
                and struct.unpack_from("<HH", data, 4) == (_LOAD, 1)
                and data[8:14] == self._target
                and data[14] == 1
            ):
                params = struct.unpack_from("<HHHH", data, 15)
                if params == _ORIGINAL:
                    self._original = params
        if opcode != 3:
            return
        if (
            len(data) >= 12
            and data[0] == 62
            and data[2] == 3
            and struct.unpack_from("<H", data, 4)[0] == self._active
        ):
            if data[3] == 0 and struct.unpack_from("<H", data, 6)[0] == 9:
                if self._original is not None and not self._changed:
                    # Command might apply even if acknowledgement fails.
                    self._changed = True
                    await self._load(_ULTRA)
                    _LOGGER.info("Ultra Bluetooth session timing applied")
        if (
            len(data) >= 6
            and data[0] == 5
            and struct.unpack_from("<H", data, 3)[0] == self._active
        ):
            await self._restore()
            self._active = None

    async def _restore(self):
        if self._changed:
            await self._load(self._original)
            self._changed = False
            _LOGGER.debug("Ultra Bluetooth preferred timing restored")

    def check(self):
        if self._task is not None and self._task.done():
            self._task.result()

    async def close(self):
        error = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as err:
                error = err
        try:
            await self._restore()
        finally:
            if self._monitor is not None:
                self._monitor.close()
                self._monitor = None
        if error is not None:
            raise error
