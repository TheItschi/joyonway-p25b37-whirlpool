"""Standalone asyncio TCP coordinator for a Joyonway spa controller.

Adapted from the DataUpdateCoordinator in https://github.com/alexbde/ha-joyonway
(MIT License), with all Home Assistant framework dependencies (HA's
DataUpdateCoordinator, config entries, dt_util, etc.) removed. The core
logic — persistent TCP connection, RS485 frame parsing, sync-frame-aligned
command pacing, and intent coalescing/verification — is preserved because
it is what makes writes reliable on this half-duplex bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .adapters import ModelAdapter, get_adapter
from .adapters.base import (
    IDX_BOARD_VERSION,
    IDX_MODEL_FAMILY,
    KNOWN_BOARD_VERSIONS,
    MIN_SIGNATURE_LENGTH,
    format_board_version,
)
from .protocol import (
    SYNC_FRAME,
    find_frames_with_indices,
    is_broadcast,
    unescape_frame,
    validate_frame,
)

_LOGGER = logging.getLogger("hottub.coordinator")

TCP_TIMEOUT = 5.0
RX_STALE_SECONDS = 15.0
INTENT_COALESCE_SECONDS = 0.3
UNRECOGNIZED_FRAME_LOG_INTERVAL = 60.0


class IntentBuildError(Exception):
    """Raised when an intent cannot be built due to invalid/missing prerequisites."""


def _default_verify(overrides: dict[str, Any], data: dict[str, Any] | None) -> bool:
    if data is None:
        return False
    return all(data.get(k) == v for k, v in overrides.items())


@dataclass
class _PendingGroup:
    overrides: dict[str, Any]
    build_fn: Callable[[dict[str, Any], dict[str, Any] | None], bytes | None]
    on_failure_callbacks: list[Callable[[], None]] = field(default_factory=list)
    verify_fn: Callable[[dict[str, Any], dict[str, Any] | None], bool] | None = None


# Delay after the sync frame before we transmit. The first command often gets
# lost on the real bus (timing vs. panel traffic / WiFi jitter), so each retry
# uses a different delay; the one that finally works is logged.
SEND_DELAYS = (0.15, 0.22, 0.30, 0.11, 0.05, 0.40)
SETTLE_SECONDS = 2.5
MAX_ATTEMPTS = 6


class IntentQueue:
    """Coalesces rapid user intents (e.g. slider drags) and drains them
    sequentially so only one command is ever in flight on the bus."""

    def __init__(
        self,
        coordinator: "JoyonwayCoordinator",
        coalesce_seconds: float = INTENT_COALESCE_SECONDS,
    ) -> None:
        self._coordinator = coordinator
        self._coalesce_seconds = coalesce_seconds
        self._pending: dict[str, _PendingGroup] = {}
        self._flush_task: asyncio.Task | None = None
        self._drain_lock = asyncio.Lock()

    def submit(
        self,
        group: str,
        overrides: dict[str, Any],
        build_fn: Callable[[dict[str, Any], dict[str, Any] | None], bytes | None],
        on_failure: Callable[[], None] | None = None,
        verify_fn: Callable[[dict[str, Any], dict[str, Any] | None], bool] | None = None,
    ) -> None:
        if group in self._pending:
            pg = self._pending[group]
            pg.overrides.update(overrides)
            pg.build_fn = build_fn
            pg.verify_fn = verify_fn
            if on_failure:
                pg.on_failure_callbacks.append(on_failure)
        else:
            self._pending[group] = _PendingGroup(
                overrides=dict(overrides),
                build_fn=build_fn,
                on_failure_callbacks=[on_failure] if on_failure else [],
                verify_fn=verify_fn,
            )

        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self) -> None:
        await asyncio.sleep(self._coalesce_seconds)
        await self._drain_all()

    async def _drain_all(self) -> None:
        async with self._drain_lock:
            groups = self._pending
            self._pending = {}
            self._flush_task = None
            for group_key, group_data in groups.items():
                await self._process_group(group_key, group_data)

        if self._pending and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = asyncio.create_task(self._flush_after_window())

    async def _process_group(self, group_key: str, group: _PendingGroup) -> None:
        data = self._coordinator.data
        try:
            frame = group.build_fn(group.overrides, data)
        except IntentBuildError as err:
            _LOGGER.error("Intent queue [%s]: %s", group_key, err)
            self._run_failure_callbacks(group)
            return
        except Exception:
            _LOGGER.exception("Intent queue [%s]: unexpected build error", group_key)
            self._run_failure_callbacks(group)
            return

        if frame is None:
            _LOGGER.debug("Intent queue [%s]: no-op detected, skipping", group_key)
            return

        verify_fn = group.verify_fn or _default_verify

        update_event = asyncio.Event()
        broadcast_count = 0

        def on_update() -> None:
            nonlocal broadcast_count
            broadcast_count += 1
            update_event.set()

        self._coordinator.register_data_callback(on_update)

        converged = False
        max_attempts = MAX_ATTEMPTS

        try:
            for attempt in range(max_attempts):
                broadcast_count = 0
                delay = SEND_DELAYS[attempt % len(SEND_DELAYS)]
                success = await self._coordinator.async_send_command(frame, delay)
                if not success:
                    _LOGGER.warning(
                        "Intent queue [%s]: send failed on attempt %d",
                        group_key,
                        attempt + 1,
                    )
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(1.0)
                    continue

                start_time = time.monotonic()
                while time.monotonic() - start_time < SETTLE_SECONDS:
                    if verify_fn(group.overrides, self._coordinator.data):
                        converged = True
                        break
                    remaining = SETTLE_SECONDS - (time.monotonic() - start_time)
                    if remaining <= 0:
                        break
                    update_event.clear()
                    try:
                        await asyncio.wait_for(update_event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break

                if converged:
                    _LOGGER.info("Intent queue [%s]: confirmed on attempt %d (delay %d ms)", group_key, attempt + 1, delay * 1000)
                    break
                _d = self._coordinator.data or {}
                _LOGGER.warning("Intent queue [%s]: attempt %d not confirmed (frame %s, status=%s heater_byte=%s heater_mode=%s ozone_mode=%s ozone_mode_byte=%s ozone_active=%s)", group_key, attempt + 1, frame.hex(), _d.get("status"), _d.get("heater_byte_raw"), _d.get("heater_mode"), _d.get("ozone_mode"), _d.get("ozone_mode_byte_raw"), _d.get("ozone_active"))
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.5)
        finally:
            self._coordinator.unregister_data_callback(on_update)

        if not converged:
            _LOGGER.error(
                "Intent queue [%s]: failed to converge after %d attempts",
                group_key,
                max_attempts,
            )
            self._run_failure_callbacks(group)

    @staticmethod
    def _run_failure_callbacks(group: _PendingGroup) -> None:
        for cb in group.on_failure_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Intent queue: on_failure callback error")

    async def shutdown(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None


class JoyonwayCoordinator:
    """Owns the persistent TCP connection to the RS485 bridge and the
    latest parsed spa state. Not tied to any web framework."""

    def __init__(self, host: str, port: int, model: str) -> None:
        self.host = host
        self.port = port
        self.model = model
        self.adapter: ModelAdapter = get_adapter(model)

        self.data: dict | None = None
        self._available = False

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_delay = 1.0
        self._stopped = False
        self._last_rx_ts = 0.0

        self._rx_frame_stats = {
            "sync": 0,
            "unicast": 0,
            "broadcast": 0,
            "crc_error": 0,
            "unrecognized": 0,
            "parsed": 0,
        }
        self._last_unrecognized_frame: str | None = None
        self._last_unrecognized_log_ts: float | None = None
        self._unsupported_board_version: int | None = None

        self._sync_frame_event = asyncio.Event()
        self._sync_timeout = 2.0

        self.intent_queue = IntentQueue(self)
        self._on_data_callbacks: list[Callable[[], None]] = []
        self._health_task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def rx_frame_stats(self) -> dict[str, int]:
        return dict(self._rx_frame_stats)

    @property
    def last_unrecognized_frame(self) -> str | None:
        return self._last_unrecognized_frame

    @property
    def unsupported_board_version(self) -> str | None:
        if self._unsupported_board_version is None:
            return None
        return format_board_version(self._unsupported_board_version)

    def set_updated_data(self, data: dict) -> None:
        self.data = data
        for cb in list(self._on_data_callbacks):
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in coordinator data update callback")

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._connect()
        self._health_task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        self._stopped = True
        await self.intent_queue.shutdown()
        if self._health_task is not None:
            self._health_task.cancel()
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
        await self._close_connection()

    async def _health_loop(self) -> None:
        """Periodically check for a stale connection (bus alive but no RX)."""
        try:
            while not self._stopped:
                await asyncio.sleep(5.0)
                if self._available and self._last_rx_ts:
                    if time.monotonic() - self._last_rx_ts > RX_STALE_SECONDS:
                        _LOGGER.warning("No RX data for %.0fs, reconnecting", RX_STALE_SECONDS)
                        await self._close_connection()
                        self._available = False
                        self._schedule_reconnect()
        except asyncio.CancelledError:
            pass

    # ── connection management ────────────────────────────────────────

    async def _connect(self) -> None:
        if self._stopped:
            return
        async with self._connect_lock:
            if self._stopped or self._writer is not None:
                return
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=TCP_TIMEOUT,
                )
                sock = self._writer.transport.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                self._available = True
                self._reconnect_delay = 1.0
                self._reader_task = asyncio.create_task(self._reader_loop())
                _LOGGER.info("RS485 bridge connected: %s:%s", self.host, self.port)
            except (OSError, asyncio.TimeoutError) as err:
                _LOGGER.warning("RS485 bridge connection failed: %s", err)
                self._available = False
                self._schedule_reconnect()

    async def _close_connection(self) -> None:
        if self._writer is not None:
            writer = self._writer
            writer.close()
            self._writer = None
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._reader = None
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            self._reader_task = None

    def _schedule_reconnect(self) -> None:
        if self._stopped:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        delay = self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)
        _LOGGER.info("Reconnecting in %.0fs", delay)
        self._reconnect_task = asyncio.create_task(self._reconnect_after(delay))

    async def _reconnect_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self._stopped:
            await self._connect()

    # ── reader loop ──────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    _LOGGER.warning("RS485 bridge disconnected (EOF)")
                    break
                buf.extend(chunk)
                self._last_rx_ts = time.monotonic()

                result, consumed = self._try_parse_buffer(buf)
                if result is not None:
                    self.set_updated_data(result)
                if consumed:
                    del buf[:consumed]
                if len(buf) > 8192:
                    buf = buf[-2048:]
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            self._available = False
            await self._close_connection()
            if not self._stopped:
                self._schedule_reconnect()

    def _handle_sync_frame(self) -> None:
        self._sync_frame_event.set()

    def _log_unrecognized_broadcast(self, logical: bytes) -> None:
        self._last_unrecognized_frame = logical.hex()
        self._unsupported_board_version = self.adapter.unsupported_board_version(logical)

        now = time.monotonic()
        if (
            self._last_unrecognized_log_ts is not None
            and now - self._last_unrecognized_log_ts < UNRECOGNIZED_FRAME_LOG_INTERVAL
        ):
            return
        self._last_unrecognized_log_ts = now

        if self._unsupported_board_version is not None:
            _LOGGER.error(
                "Unsupported controller board version %s (byte 0x%02x). Your '%s' "
                "controller is recognized, but this adapter has only been "
                "verified against board versions %s.",
                format_board_version(self._unsupported_board_version),
                self._unsupported_board_version,
                self.model,
                ", ".join(format_board_version(b) for b in KNOWN_BOARD_VERSIONS),
            )
            return

        expected = getattr(self.adapter, "broadcast_signature", b"")
        _LOGGER.warning(
            "Received a valid RS485 broadcast frame the '%s' adapter cannot parse. "
            "Expected header %s, got %s (frame length %d). Frame stats: %s.",
            self.model,
            expected.hex(" ") if expected else "<none>",
            logical[:MIN_SIGNATURE_LENGTH].hex(" "),
            len(logical),
            self._rx_frame_stats,
        )

    def _try_parse_buffer(self, buf: bytes | bytearray) -> tuple[dict | None, int]:
        frames = find_frames_with_indices(bytes(buf))
        if not frames:
            return None, 0

        _, last_end = frames[-1]
        latest_data: dict | None = None

        for raw_frame, _ in frames:
            if raw_frame == SYNC_FRAME:
                self._rx_frame_stats["sync"] += 1
                self._handle_sync_frame()
                continue

            if not is_broadcast(raw_frame):
                self._rx_frame_stats["unicast"] += 1
                continue

            self._rx_frame_stats["broadcast"] += 1
            if not validate_frame(raw_frame):
                self._rx_frame_stats["crc_error"] += 1
                continue

            logical = unescape_frame(raw_frame)
            try:
                data = self.adapter.parse_status(logical)
            except (IndexError, ValueError, KeyError):
                _LOGGER.exception("Adapter parse failed for frame: %s", logical.hex())
                continue
            if data is None:
                self._rx_frame_stats["unrecognized"] += 1
                self._log_unrecognized_broadcast(logical)
                continue

            self._rx_frame_stats["parsed"] += 1
            latest_data = data

        return latest_data, last_end

    # ── command sending ──────────────────────────────────────────────

    async def async_send_command(self, frame: bytes, delay: float = 0.03) -> bool:
        if self._writer is None:
            _LOGGER.error("Cannot send command: not connected")
            self._schedule_reconnect()
            return False

        async with self._write_lock:
            if self._sync_timeout > 0.0:
                self._sync_frame_event.clear()
                try:
                    await asyncio.wait_for(self._sync_frame_event.wait(), timeout=self._sync_timeout)
                    await asyncio.sleep(delay)
                except asyncio.TimeoutError:
                    _LOGGER.error("Timeout waiting for sync frame, aborting command send")
                    return False

            try:
                self._writer.write(frame)
                await self._writer.drain()
                return True
            except (OSError, asyncio.TimeoutError) as err:
                _LOGGER.error("Command send failed: %s", err)
                await self._close_connection()
                self._schedule_reconnect()
                return False

    async def send_and_verify(
        self,
        frame: bytes,
        verify: Callable[[dict[str, Any] | None], bool],
        *,
        label: str,
        attempts: int = MAX_ATTEMPTS,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> bool:
        """Send `frame` and wait until `verify(data)` holds; re-send if it does not.

        Only use for frames that are idempotent in the target state (re-sending
        the frame once it has taken effect must not change anything).
        """
        event = asyncio.Event()

        def on_update() -> None:
            event.set()

        self.register_data_callback(on_update)
        try:
            for attempt in range(1, attempts + 1):
                delay = SEND_DELAYS[(attempt - 1) % len(SEND_DELAYS)]
                if not await self.async_send_command(frame, delay):
                    _LOGGER.warning("[%s] attempt %d: send failed", label, attempt)
                    await asyncio.sleep(1.0)
                    continue
                deadline = time.monotonic() + settle_seconds
                while True:
                    if verify(self.data):
                        _LOGGER.info("[%s] confirmed on attempt %d (delay %d ms)", label, attempt, delay * 1000)
                        return True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    event.clear()
                    try:
                        await asyncio.wait_for(event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                _LOGGER.warning("[%s] attempt %d: no state change after %.1fs", label, attempt, settle_seconds)
            _LOGGER.error("[%s] not confirmed after %d attempts", label, attempts)
            return False
        finally:
            self.unregister_data_callback(on_update)

    # ── callbacks ────────────────────────────────────────────────────

    def register_data_callback(self, callback_fn: Callable[[], None]) -> None:
        self._on_data_callbacks.append(callback_fn)

    def unregister_data_callback(self, callback_fn: Callable[[], None]) -> None:
        if callback_fn in self._on_data_callbacks:
            self._on_data_callbacks.remove(callback_fn)
