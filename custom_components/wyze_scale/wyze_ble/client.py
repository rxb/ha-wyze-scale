"""Async BLE client for the Wyze Scale X.

Owns one connection/session: key exchange, command send/reply matching, and
demultiplexing of unsolicited messages (live weight, history records). The
session key and frame counter are per-connection; a new connection performs
a fresh handshake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from . import protocol
from .protocol import (
    CHAR_UUID,
    CMD_CUR_WEIGHT_DATA,
    CMD_HEART_RESULT,
    CMD_HISTORY_WEIGHT_DATA,
    CMD_USER_LIST_NEW,
    FRAME_ENC_REPLY,
    FRAME_KEX_REPLY,
    HeartResult,
    Measurement,
    Message,
    ProtocolError,
    UserRecord,
)

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 5.0
REPLY_TIMEOUT = 3.0
# Gap that ends a multi-message stream (user list, history records)
STREAM_GAP_TIMEOUT = 2.5


class WyzeScaleError(Exception):
    """Communication or protocol failure talking to the scale."""


class WyzeScaleClient:
    """One BLE session with the scale."""

    def __init__(
        self,
        on_live_weight: Callable[[Measurement], None] | None = None,
        on_message: Callable[[Message], None] | None = None,
        on_heart_result: "Callable[[HeartResult], None] | None" = None,
    ) -> None:
        self._client: BleakClient | None = None
        self._key: bytes | None = None
        self._counter = 0
        self._on_live_weight = on_live_weight
        self._on_heart_result = on_heart_result
        # Catch-all observer: called for every decrypted message before
        # normal dispatch. Used for protocol exploration.
        self._on_message = on_message
        self._kex_future: asyncio.Future[int] | None = None
        self._ack_futures: dict[int, asyncio.Future[int]] = {}
        self._user_list_queue: asyncio.Queue[Message] = asyncio.Queue()
        self._history_queue: asyncio.Queue[Measurement | None] = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, ble_device: BLEDevice) -> None:
        """Connect, subscribe to notifications, and perform the key exchange."""
        self._counter = 0
        self._key = None
        self._user_list_queue = asyncio.Queue()
        self._history_queue = asyncio.Queue()
        self._client = await establish_connection(
            BleakClient,
            ble_device,
            ble_device.name or ble_device.address,
            disconnected_callback=self._handle_disconnect,
        )
        try:
            # On BlueZ the negotiated MTU is not exposed and bleak reports
            # the 23-byte default even though larger notifications arrive
            # fine, so this is informational only.
            _LOGGER.debug("Reported MTU: %d", self._client.mtu_size)
            await self._client.start_notify(CHAR_UUID, self._handle_notification)
            await self._handshake()
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        self._key = None
        self._fail_pending(WyzeScaleError("disconnected"))
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort teardown
                _LOGGER.debug("Error during disconnect", exc_info=True)

    def _handle_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("Scale disconnected")
        self._fail_pending(WyzeScaleError("scale disconnected"))

    def _fail_pending(self, exc: Exception) -> None:
        if self._kex_future is not None and not self._kex_future.done():
            self._kex_future.set_exception(exc)
            self._kex_future.exception()  # mark retrieved: waiter may be gone
        for fut in self._ack_futures.values():
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()
        self._ack_futures.clear()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        private = protocol.generate_private_key()
        loop = asyncio.get_running_loop()
        self._kex_future = loop.create_future()
        frame = protocol.build_kex_frame(self._next_counter(), protocol.public_key(private))
        await self._write(frame)
        try:
            scale_public = await asyncio.wait_for(self._kex_future, HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise WyzeScaleError("timeout waiting for key-exchange reply") from err
        finally:
            self._kex_future = None
        secret = protocol.shared_secret(scale_public, private)
        self._key = protocol.derive_session_key(secret)
        _LOGGER.debug("Handshake complete")

    # ------------------------------------------------------------------
    # I/O plumbing
    # ------------------------------------------------------------------

    def _next_counter(self) -> int:
        counter = self._counter
        self._counter = (self._counter + 1) % 16
        return counter

    async def _write(self, data: bytes) -> None:
        if self._client is None:
            raise WyzeScaleError("not connected")
        try:
            await self._client.write_gatt_char(CHAR_UUID, data, response=True)
        except WyzeScaleError:
            raise
        except Exception as err:
            raise WyzeScaleError(f"BLE write failed: {err}") from err

    def _handle_notification(self, _sender, data: bytearray) -> None:
        frame = bytes(data)
        if not frame:
            return
        frame_type = frame[0] >> 4
        if frame_type == FRAME_KEX_REPLY:
            if self._kex_future is not None and not self._kex_future.done():
                try:
                    self._kex_future.set_result(protocol.parse_kex_reply(frame))
                except ProtocolError as err:
                    self._kex_future.set_exception(err)
            return
        if frame_type != FRAME_ENC_REPLY or self._key is None:
            _LOGGER.debug("Ignoring frame: %s", frame.hex())
            return
        try:
            plain = protocol.decrypt_frame(frame, self._key)
            msg = protocol.parse_message(plain)
        except ProtocolError as err:
            _LOGGER.debug("Undecodable frame (%s): %s", err, frame.hex())
            return
        self._dispatch(msg)

    def _dispatch(self, msg: Message) -> None:
        _LOGGER.debug("<- cmd=0x%02x status=%s len=%d", msg.cmd, msg.status, len(msg.raw))
        if self._on_message is not None:
            self._on_message(msg)
        if msg.cmd == CMD_CUR_WEIGHT_DATA:
            try:
                measurement = protocol.parse_live_weight(msg)
            except ProtocolError as err:
                _LOGGER.debug("Bad live weight message: %s", err)
                return
            if self._on_live_weight is not None:
                self._on_live_weight(measurement)
            return
        if msg.cmd == CMD_HISTORY_WEIGHT_DATA:
            self._history_queue.put_nowait(protocol.parse_history_record(msg))
            return
        if msg.cmd == CMD_HEART_RESULT:
            if self._on_heart_result is not None:
                try:
                    self._on_heart_result(protocol.parse_heart_result(msg))
                except ProtocolError as err:
                    _LOGGER.debug("Bad heart result message: %s", err)
            return
        if msg.cmd == CMD_USER_LIST_NEW:
            self._user_list_queue.put_nowait(msg)
            return
        fut = self._ack_futures.pop(msg.cmd, None)
        if fut is not None and not fut.done():
            fut.set_result(msg.status if msg.status is not None else -1)
        else:
            _LOGGER.debug("Unhandled message cmd=0x%02x: %s", msg.cmd, msg.raw.hex())

    async def _send(self, payload: bytes) -> None:
        if self._key is None:
            raise WyzeScaleError("not connected / no session key")
        await self._write(protocol.build_encrypted_frame(self._next_counter(), payload, self._key))

    async def _send_expect_ack(self, payload: bytes, cmd: int) -> None:
        async with self._cmd_lock:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[int] = loop.create_future()
            self._ack_futures[cmd] = fut
            try:
                await self._send(payload)
                status = await asyncio.wait_for(fut, REPLY_TIMEOUT)
            except asyncio.TimeoutError as err:
                raise WyzeScaleError(f"timeout waiting for reply to 0x{cmd:02x}") from err
            finally:
                self._ack_futures.pop(cmd, None)
        if status != 0:
            raise WyzeScaleError(f"command 0x{cmd:02x} failed with status {status}")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def send_raw(self, cmd: int, args: bytes = b"") -> None:
        """Send an arbitrary command without waiting for a reply.

        For protocol exploration; replies (if any) arrive via on_message.
        """
        await self._send(protocol.build_request(cmd, args))

    async def enter_heart_mode(self) -> None:
        """Put the scale into heart-rate measurement mode (fire-and-forget).

        HEART_RESULT frames arrive via the on_heart_result callback. The
        official app re-sends this periodically while measuring.
        """
        await self._send(protocol.build_heart_mode())

    async def enter_weight_mode(self) -> None:
        """Return the scale to normal weighing mode."""
        await self._send(protocol.build_weight_mode())

    async def sync_time(self, timestamp: int) -> None:
        await self._send_expect_ack(protocol.build_sync_time(timestamp), protocol.CMD_SYNC_TIME)

    async def set_unit(self, unit: int) -> None:
        await self._send_expect_ack(protocol.build_set_unit(unit), protocol.CMD_SET_UNIT)

    async def set_hello(self, enabled: bool) -> None:
        await self._send_expect_ack(protocol.build_set_hello(enabled), protocol.CMD_SET_HELLO)

    async def get_users(self) -> list[UserRecord]:
        """Request the stored user list, collecting multi-message replies."""
        while not self._user_list_queue.empty():
            self._user_list_queue.get_nowait()
        async with self._cmd_lock:
            await self._send(protocol.build_user_list())
            users: list[UserRecord] = []
            while True:
                try:
                    msg = await asyncio.wait_for(
                        self._user_list_queue.get(), STREAM_GAP_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    break
                users.extend(protocol.parse_user_list(msg))
            if not users:
                _LOGGER.debug("User list empty or no reply")
            return users

    async def select_user(self, record: UserRecord) -> None:
        await self._send_expect_ack(
            protocol.build_current_user(record), protocol.CMD_CURRENT_USER_NEW
        )

    async def update_user(self, record: UserRecord) -> None:
        await self._send_expect_ack(
            protocol.build_update_user(record), protocol.CMD_UPDATE_USER
        )

    async def delete_user(self, user_id: bytes) -> None:
        await self._send_expect_ack(
            protocol.build_delete_user(user_id), protocol.CMD_DEL_USER
        )

    async def drain_history(
        self,
        record: UserRecord,
        on_record: Callable[[Measurement], None] | None = None,
    ) -> list[Measurement]:
        """Select a user and read+acknowledge all pending history records.

        Acknowledging a record DELETES it from the scale. Each record is
        handed to ``on_record`` before its ack is sent, so even if the
        connection drops mid-drain no acknowledged record is lost - callers
        must persist records from the callback, not just the return value.
        """
        while not self._history_queue.empty():
            self._history_queue.get_nowait()
        await self.select_user(record)
        measurements: list[Measurement] = []
        while True:
            try:
                measurement = await asyncio.wait_for(
                    self._history_queue.get(), STREAM_GAP_TIMEOUT
                )
            except asyncio.TimeoutError:
                break
            if measurement is None:
                # Status != valid: treat as end of stream, do not ack.
                break
            measurements.append(measurement)
            if on_record is not None:
                on_record(measurement)
            await self._send(protocol.build_history_ack())
        return measurements
