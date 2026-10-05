"""The poller: one inverter's server mode, client mode, or both, over one inverter (D36). The one loop over the core's
blocks; nothing runs in parallel on the module.

Server mode: the listener hands the poller each module session's connection; the poller makes the session's cloud
session, waits the settle time, reads the identity (`10000 x 32`), and then polls on the schedule. Client mode: the
poller dials per update, reads the identity, reads the due groups and closes, or holds the connection in live mode.
Server first: while a module session is checked the client does not poll; while no session is open, or the open one is
refused, the client does. A module that connects during a client update waits for it to end.

The identity read decides a connection: ours, or none learned yet, and its block is stored in the inverter like any
other, the serial learned (`on_serial_learned`); another inverter's, and the connection is refused, nothing stored, the
reason kept (`Session.refused`, `client_refused`); no reply, and the server session is checked again later, the client
at its next update. A reading keeps its value until a read of its group fails: with no way in, once one was tried, the
due groups fail, "no connection"; before the first update nothing fails. With `clear_on_session_end` the inverter is
cleared when a module session ends, as at the start, and a serial learned in this run forgotten: the tool's way. The
poller never writes to the inverter on its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from .channel import ChannelError
from .cloud_session import CloudSession, CloudSettings, CloudWrite
from .connection import Connection, ReplyTimeout, dial
from .const import (
    CHECK_RETRY_S,
    CONNECT_TIMEOUT_S,
    DEFAULT_BIND,
    DEFAULT_CLIENT_PORT,
    DEFAULT_SERVER_PORT,
    FIRST_REPLY_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    QUIET_TIME_S,
    REPLY_TIMEOUT_S,
    REQUEST_GAP_S,
    SETTLE_S,
)
from .frames import Frame
from .group_reader import GroupResult, read_group
from .groups import IDENTITY_GROUP, IDENTITY_RANGE, PollGroup
from .handshake import ModuleInfo, send_handshake
from .inverter import Inverter
from .listener import Listener
from .registers import Block, decode_block

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerSettings:
    """Server mode: the port the module dials and the connection's timing."""

    bind: str = DEFAULT_BIND
    port: int = DEFAULT_SERVER_PORT
    reply_timeout_s: float = REPLY_TIMEOUT_S
    request_gap_s: float = REQUEST_GAP_S
    idle_timeout_s: float = IDLE_TIMEOUT_S


@dataclass(frozen=True)
class ClientSettings:
    """Client mode: the endpoint the poller dials, the BLE relay in practice, and the connection's timing. The quiet
    time holds the next request back after a frame nobody asked for, the cloud polling over the shared UART."""

    host: str
    port: int = DEFAULT_CLIENT_PORT
    reply_timeout_s: float = REPLY_TIMEOUT_S
    first_reply_timeout_s: float = FIRST_REPLY_TIMEOUT_S
    request_gap_s: float = REQUEST_GAP_S
    quiet_time_s: float = QUIET_TIME_S
    connect_timeout_s: float = CONNECT_TIMEOUT_S
    idle_timeout_s: float = IDLE_TIMEOUT_S

    @property
    def name(self) -> str:
        return f"{self.host}:{self.port}"


class Source(Enum):
    """The way an update went."""

    SERVER = "server"
    CLIENT = "client"


@dataclass
class Session:
    """One module session: its connection, its cloud session, what the module said about itself, and its check."""

    connection: Connection
    module: ModuleInfo = field(default_factory=ModuleInfo)
    cloud: CloudSession | None = None
    checked: bool = False
    refused: str | None = None
    # When the identity is checked next (monotonic seconds); 0 once checked or refused.
    check_at: float = 0.0

    @property
    def state(self) -> str:
        """For logs and diagnostics: pending until the identity is checked, connected once it is, refused when
        another inverter answers."""
        if self.refused is not None:
            return "refused"
        return "connected" if self.checked else "pending"


@dataclass(frozen=True)
class Identity:
    """What the identity read gave: the block and the serial, or why the connection is not accepted. `refused` is True
    when another inverter answers, False when the read failed."""

    block: Block | None = None
    serial: str | None = None
    model: str | None = None
    error: str | None = None
    refused: bool = False

    @property
    def accepted(self) -> bool:
        return self.error is None


async def read_identity(connection: Connection) -> Identity:
    """Reads the identity block, `10000 x 32`: the model and the serial, or why it gave none. The poller's first read
    of every connection, and the setup flow's test of one."""
    try:
        reply = await connection.read(IDENTITY_RANGE.address, IDENTITY_RANGE.count)
    except (ReplyTimeout, ChannelError) as error:
        return Identity(error=f"the identity read failed: {error}")
    if reply.is_exception:
        return Identity(error=f"the identity read failed: exception {reply.exception_code or 0:02d}")
    block = Block(IDENTITY_RANGE.address, reply.registers())
    readings = decode_block(block, IDENTITY_GROUP.registers)
    serial = str(readings["serial"].value) if "serial" in readings else ""
    model = str(readings["model"].value) if "model" in readings else None
    if not serial:
        return Identity(block=block, model=model, error="the identity read gave no inverter serial")
    return Identity(block=block, serial=serial, model=model)


def quiet_time(connection: Connection, seconds: float) -> Callable[[Frame], None]:
    """Client mode's `on_unsolicited`: a frame nobody asked for holds the connection's next request back."""

    def on_unsolicited(frame: Frame) -> None:
        connection.hold(seconds)

    return on_unsolicited


class Poller:
    """One inverter's server mode, client mode, or both, over one inverter. `serial` is the serial learned before,
    None when none is yet."""

    def __init__(
        self,
        inverter: Inverter,
        *,
        server: ServerSettings | None = None,
        client: ClientSettings | None = None,
        cloud: CloudSettings | None = None,
        serial: str | None = None,
        send_handshake: bool = True,
        settle_s: float = SETTLE_S,
        clear_on_session_end: bool = False,
    ) -> None:
        if server is None and client is None:
            raise ValueError("neither server mode nor client mode")
        self.inverter = inverter
        self.client_settings = client
        self.cloud_settings = cloud
        self.serial = serial
        self.send_handshake = send_handshake
        self.settle_s = settle_s
        self.clear_on_session_end = clear_on_session_end
        self.listener: Listener | None = None
        if server is not None:
            self.listener = Listener(
                server.bind,
                server.port,
                self._on_session,
                idle_timeout_s=server.idle_timeout_s,
                reply_timeout_s=server.reply_timeout_s,
                request_gap_s=server.request_gap_s,
            )
        # The current module session, None while no module is connected.
        self.session: Session | None = None
        # The client's connection while one is open: during an update, or held in live mode.
        self.client_connection: Connection | None = None
        self.client_refused: str | None = None
        # Why the client's last update failed as a connection: the dial, the identity read, the connection lost. None
        # when it went through, a group's own failure aside.
        self.client_error: str | None = None
        # The connection opened last, either way, open or closed since: its counters for the diagnostics.
        self.last_connection: Connection | None = None
        # The way in use: the last source picked, None before any.
        self.source: Source | None = None
        self.last_session_end: str | None = None
        # Called after every change worth showing: a session, a check, an update, the cloud.
        self.on_update: Callable[[], None] | None = None
        # Called with the first serial ever read; the caller keeps it for the next start.
        self.on_serial_learned: Callable[[str], None] | None = None
        # Called for every cloud write outside the clock, blocked or forwarded.
        self.on_cloud_write: Callable[[CloudWrite], None] | None = None
        self._serial_given = serial is not None
        self._new_connection: Connection | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def live_mode(self) -> bool:
        return self.inverter.live_mode

    @live_mode.setter
    def live_mode(self, on: bool) -> None:
        """Live mode on: the live and energy groups at their live intervals, the client's connection held. Off: a held
        connection is closed at the loop's next step."""
        self.inverter.live_mode = on
        LOGGER.info("live mode %s", "on" if on else "off")
        self._wake.set()

    @property
    def module(self) -> ModuleInfo:
        """What the current module session's handshake replies said; empty without a session."""
        return self.session.module if self.session is not None else ModuleInfo()

    @property
    def cloud(self) -> CloudSession | None:
        """The current module session's cloud session, None without one."""
        return self.session.cloud if self.session is not None else None

    @property
    def server_refused(self) -> str | None:
        return self.session.refused if self.session is not None else None

    async def start(self) -> None:
        """Opens the server's port and starts the loop. Raises ChannelError when the port cannot be opened."""
        parts = []
        if self.listener is not None:
            await self.listener.start()
            parts.append(f"waiting for the module on {self.listener.name}")
            parts.append(self.cloud_settings.describe() if self.cloud_settings is not None else "no cloud")
            parts.append(f"handshake {'on' if self.send_handshake else 'off'}")
        if self.client_settings is not None:
            parts.append(f"client to {self.client_settings.name}")
        parts.append(f"inverter serial {self.serial}" if self.serial is not None else "inverter serial to learn")
        LOGGER.info("%s", "; ".join(parts))
        self._task = asyncio.create_task(self._run(), name="poller")

    async def stop(self) -> None:
        """Stops the loop, closes the port, the module's session with its cloud, and the client's connection."""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.listener is not None:
            await self.listener.stop()
        if self.session is not None:
            await self._end_session(self.session)
        if self._new_connection is not None:
            await self._new_connection.close("poller stopped")
            self._new_connection = None
        if self.client_connection is not None:
            await self.client_connection.close("poller stopped")
            self.client_connection = None

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            connection = self._new_connection
            if connection is not None:
                self._new_connection = None
                await self._start_session(connection)
                continue
            session = self.session
            if session is not None and not session.connection.is_connected:
                await self._end_session(session)
                continue
            if self.client_connection is not None and not self.client_connection.is_connected:
                self.client_connection = None
            if self.client_connection is not None and not self.live_mode:
                # Held in live mode, which is over: back to a connection per update.
                await self.client_connection.close("live mode off")
                self.client_connection = None
            now = time.monotonic()
            if session is not None and session.check_at and now >= session.check_at:
                await self._check_session(session)
                continue
            source = self.current_source
            if source is not None and source is not self.source:
                LOGGER.info("updating through the %s", source.value)
                self.source = source
            due = self.inverter.due(now)
            if due:
                if source is Source.SERVER:
                    assert session is not None
                    results = await self._poll(session.connection, due)
                elif source is Source.CLIENT:
                    results = await self._update_client(due)
                elif self._no_way_in():
                    results = self.inverter.fail(due, "no connection")
                else:
                    # A session waiting for its check, or no way in yet: nothing to read with.
                    results = []
                if results:
                    self._notify()
            await self._sleep(self._wait_s())

    @property
    def current_source(self) -> Source | None:
        """The way to read through now: the server while its session is checked; the client while no session is open,
        or the open one is refused; None otherwise. `source` is the last one picked."""
        session = self.session
        if session is not None and session.checked and session.connection.is_connected:
            return Source.SERVER
        if self.client_settings is not None and (session is None or session.refused is not None):
            return Source.CLIENT
        return None

    def _no_way_in(self) -> bool:
        """True when no source is there to read with, and there was one before: the due groups fail. Before the
        first source nothing fails; the readings are empty anyway."""
        session = self.session
        return (session is None or session.refused is not None) and self.source is not None

    def _wait_s(self) -> float | None:
        """Seconds until the next thing to do, None when nothing waits for a time."""
        now = time.monotonic()
        session = self.session
        waits = []
        if session is not None and session.check_at:
            waits.append(session.check_at - now)
        if self.current_source is not None or self._no_way_in():
            due = self.inverter.seconds_until_due(now)
            if due is not None:
                waits.append(due)
        return min(waits) if waits else None

    async def _sleep(self, seconds: float | None) -> None:
        """Waits for the time, a wake, or the end of a connection in use, whichever comes first."""
        if seconds is not None and seconds <= 0:
            return
        events = [self._wake]
        if self.session is not None:
            events.append(self.session.connection.closed)
        if self.client_connection is not None:
            events.append(self.client_connection.closed)
        waits = [asyncio.ensure_future(event.wait()) for event in events]
        try:
            await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    def _on_session(self, connection: Connection) -> None:
        """From the listener; the loop takes the connection at its next step, after an update under way."""
        self._new_connection = connection
        self._wake.set()

    async def _start_session(self, connection: Connection) -> None:
        """A module session: the session before it ended, the client's connection closed, the cloud session made, the
        identity check due after the settle time."""
        if self.session is not None:
            await self._end_session(self.session)
        if self.client_connection is not None:
            await self.client_connection.close("the module connected")
            self.client_connection = None
        session = Session(connection, check_at=time.monotonic() + self.settle_s)
        self.last_connection = connection
        if self.cloud_settings is not None:
            cloud = CloudSession(connection, self.cloud_settings, session.module)
            cloud.on_update = self._notify
            cloud.on_cloud_write = self._report_cloud_write
            connection.on_unsolicited = cloud.take_unsolicited
            cloud.start()
            session.cloud = cloud
        self.session = session
        self._notify()

    async def _end_session(self, session: Session) -> None:
        if self.session is session:
            self.session = None
        if session.cloud is not None:
            await session.cloud.stop("the module's session ended")
        if session.connection.is_connected:
            await session.connection.close("session ended")
        reason = session.connection.close_reason or "closed"
        self.last_session_end = reason
        LOGGER.info("module session ended: %s", reason)
        if self.clear_on_session_end:
            # As at the start: nothing read, nothing failing, the serial to learn again.
            self.inverter.clear()
            self.source = None
            if not self._serial_given:
                self.serial = None
        self._notify()

    async def _check_session(self, session: Session) -> None:
        """The identity read on the module's session: checked, the handshake while no cloud is connected; refused; or
        checked again later, the due groups failed meanwhile."""
        identity = await self._read_identity(session.connection)
        if session is not self.session or not session.connection.is_connected:
            return
        name = session.connection.name
        if identity.accepted:
            session.checked = True
            session.check_at = 0.0
            self._accept(identity)
            self.inverter.retry_failed()
            LOGGER.info("module session %s: the inverter %s", name, self.serial)
            if session.cloud is not None:
                session.cloud.inverter = self.inverter
            if self.send_handshake and not (session.cloud is not None and session.cloud.connected):
                await send_handshake(session.connection, session.module)
        elif identity.refused:
            session.refused = identity.error
            session.check_at = 0.0
            LOGGER.warning("module session %s refused: %s; not polled", name, identity.error)
        else:
            now = time.monotonic()
            session.check_at = now + CHECK_RETRY_S
            LOGGER.warning("module session %s: %s; checking again in %g s", name, identity.error, CHECK_RETRY_S)
            self.inverter.fail(self.inverter.due(now), identity.error or "the identity read failed")
        self._notify()

    async def _read_identity(self, connection: Connection) -> Identity:
        """Reads the model and the serial: accepted when the serial is the learned one, or none is learned yet."""
        identity = await read_identity(connection)
        if identity.accepted and self.serial is not None and identity.serial != self.serial:
            error = f"the inverter {identity.serial} answers, not {self.serial}"
            return Identity(block=identity.block, serial=identity.serial, model=identity.model, error=error, refused=True)
        return identity

    def _accept(self, identity: Identity) -> None:
        """The connection is the inverter's: its serial learned when none is yet, its identity block stored."""
        if self.serial is None and identity.serial is not None:
            self.serial = identity.serial
            LOGGER.info("inverter serial %s learned", identity.serial)
            if self.on_serial_learned is not None:
                self.on_serial_learned(identity.serial)
        if identity.block is not None:
            self.inverter.store_block(IDENTITY_GROUP, identity.block)

    async def _update_client(self, due: list[PollGroup]) -> list[GroupResult]:
        """The due groups through the client: dialled and checked when no connection is held, closed after unless in
        live mode."""
        settings = self.client_settings
        assert settings is not None
        connection = self.client_connection
        try:
            if connection is None:
                connection = await dial(
                    settings.host,
                    settings.port,
                    connect_timeout_s=settings.connect_timeout_s,
                    first_reply_timeout_s=settings.first_reply_timeout_s,
                    idle_timeout_s=settings.idle_timeout_s,
                    reply_timeout_s=settings.reply_timeout_s,
                    request_gap_s=settings.request_gap_s,
                )
                self.client_connection = connection
                self.last_connection = connection
                if settings.quiet_time_s > 0:
                    connection.on_unsolicited = quiet_time(connection, settings.quiet_time_s)
                identity = await self._read_identity(connection)
                if not identity.accepted:
                    reason = identity.error or "not accepted"
                    await connection.close(reason)
                    self.client_connection = None
                    self.client_refused = reason if identity.refused else None
                    self.client_error = reason
                    return self.inverter.fail(due, reason)
                self.client_refused = None
                self._accept(identity)
            results = await self._poll(connection, due)
            self.client_error = None if connection.is_connected else connection.close_reason or "the connection was lost"
            return results
        except ChannelError as error:
            self.client_error = str(error)
            return self.inverter.fail(due, str(error))
        finally:
            if not self.live_mode and self.client_connection is not None:
                await self.client_connection.close()
                self.client_connection = None

    async def _poll(self, connection: Connection, due: list[PollGroup]) -> list[GroupResult]:
        """Reads the due groups through the connection, in order, then those that became due meanwhile, each group
        once per update: no second dial right after this one. A lost connection ends the update, the rest stays
        due."""
        results: list[GroupResult] = []
        read: set[str] = set()
        while due:
            for group in due:
                result = await read_group(connection, group)
                read.add(group.key)
                self.inverter.store_result(result)
                results.append(result)
                if not connection.is_connected:
                    return results
            due = [group for group in self.inverter.due(time.monotonic()) if group.key not in read]
        return results

    def _report_cloud_write(self, write: CloudWrite) -> None:
        if self.on_cloud_write is not None:
            self.on_cloud_write(write)

    def _notify(self) -> None:
        if self.on_update is not None:
            self.on_update()
