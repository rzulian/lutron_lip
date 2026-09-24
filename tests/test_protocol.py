"""Reconnect tests for the LIP protocol against a fake bridge on localhost."""

import asyncio
import gc
import logging
import socket
import struct
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from aiolip import protocol
from aiolip.data import LIPMode
from aiolip.exceptions import LIPConnectionStateError

# The last subscription _async_setup_monitoring() sends.
LAST_MONITORING_COMMAND = "#MONITORING,10,1"
ZONE_UPDATE = "~OUTPUT,5,1,75.00"


class FakeBridge:
    """A minimal LIP server: log in, then watch the commands the client sends."""

    def __init__(self) -> None:
        """Initialize the fake bridge."""
        self.server: asyncio.Server | None = None
        self.port = 0
        self.sessions: list[asyncio.StreamWriter] = []
        self.monitoring_done: defaultdict[int, asyncio.Event] = defaultdict(
            asyncio.Event
        )
        # While True, the bridge is "down": it drops each new session at once.
        self.refuse = False
        self.refused: asyncio.Queue[int] = asyncio.Queue()
        # Sessions that get a garbled login banner instead of "login: ".
        self.garble: set[int] = set()
        self.keepalives: defaultdict[int, int] = defaultdict(int)

    async def start(self) -> None:
        """Listen on an ephemeral localhost port."""
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Close every session and stop listening."""
        for writer in self.sessions:
            writer.transport.abort()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.sessions.append(writer)
        index = len(self.sessions) - 1
        monitoring_done = self.monitoring_done[index]
        if self.refuse:
            writer.close()
            self.refused.put_nowait(index)
            return
        try:
            writer.write(b"l\x00gin: " if index in self.garble else b"login: ")
            await reader.readline()
            writer.write(b"password: ")
            await reader.readline()
            writer.write(b"GNET> ")
            while line := await reader.readline():
                command = line.decode().strip()
                if command == LAST_MONITORING_COMMAND:
                    monitoring_done.set()
                elif command == protocol.LIP_KEEP_ALIVE:
                    self.keepalives[index] += 1
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def wait_for_session(self, index: int) -> None:
        """Wait until session `index` has finished subscribing to monitoring."""
        async with asyncio.timeout(5):
            await self.monitoring_done[index].wait()

    async def push(self, index: int, line: str) -> None:
        """Send an unsolicited update on session `index`."""
        self.sessions[index].write(line.encode() + b"\r\n")
        await self.sessions[index].drain()

    def reset(self, index: int) -> None:
        """Drop session `index` with a TCP RST (SO_LINGER 0)."""
        writer = self.sessions[index]
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.transport.abort()

    def close(self, index: int) -> None:
        """Close session `index` cleanly with a FIN."""
        self.sessions[index].close()

    def open_sessions(self) -> int:
        """Count the sessions the client still holds open."""
        return sum(not writer.is_closing() for writer in self.sessions)

    async def wait_for_refused(self, count: int) -> None:
        """Wait until the bridge has turned away `count` more sessions."""
        async with asyncio.timeout(5):
            for _ in range(count):
                await self.refused.get()


@pytest.fixture
def fast_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make failed reconnect attempts retry immediately."""
    monkeypatch.setattr(protocol, "RECONNECT_DELAY", 0.01)


async def _connect(
    bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> tuple[protocol.LIP, asyncio.Task, asyncio.Queue]:
    """Connect a LIP client to the bridge and start its reader loop."""
    monkeypatch.setattr(protocol, "LIP_PORT", bridge.port)
    lip = protocol.LIP()
    messages: asyncio.Queue = asyncio.Queue()
    lip.set_callback(messages.put_nowait)
    await lip.async_connect("127.0.0.1", "user", "pass")
    run_task = asyncio.create_task(lip.async_run())
    await bridge.wait_for_session(0)
    return lip, run_task, messages


async def _assert_update_received(
    bridge: FakeBridge, session: int, messages: asyncio.Queue
) -> None:
    await bridge.push(session, ZONE_UPDATE)
    async with asyncio.timeout(5):
        message = await messages.get()
    assert message.mode == LIPMode.OUTPUT
    assert message.integration_id == 5
    assert message.value == 75.0


async def _shutdown(
    lip: protocol.LIP,
    run_task: asyncio.Task,
    bridge: FakeBridge,
    *,
    stop_bridge: bool = True,
) -> None:
    await lip.async_stop()
    # The reader loop must notice the stop and return on its own.
    async with asyncio.timeout(5):
        await run_task
    if stop_bridge:
        await bridge.stop()


def _run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """
    Run a scenario, failing on any error asyncio reports but cannot raise.

    That covers "Task exception was never retrieved" and exceptions in
    callbacks, which would otherwise only be logged.
    """
    errors: list[dict[str, Any]] = []
    with asyncio.Runner(debug=True) as runner:
        runner.get_loop().set_exception_handler(
            lambda _loop, context: errors.append(context)
        )
        runner.run(scenario())
        # Finalize dropped tasks and sockets while the loop is still open, so
        # their errors reach the handler above.
        gc.collect()
    assert not errors, [context["message"] for context in errors]


@pytest.mark.usefixtures("fast_reconnect")
@pytest.mark.parametrize(
    ("drop", "logged"),
    [
        ("reset", "connection lost while reading"),
        ("close", "connection closed while reading"),
    ],
)
def test_reconnects_and_resubscribes_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    drop: str,
    logged: str,
) -> None:
    """A TCP reset or a clean EOF reconnects, re-subscribes, and updates resume."""
    caplog.set_level(logging.INFO, logger=protocol.__name__)

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)
        await _assert_update_received(bridge, 0, messages)

        getattr(bridge, drop)(0)

        await bridge.wait_for_session(1)
        await _assert_update_received(bridge, 1, messages)
        assert not run_task.done()
        assert lip.connection_state == protocol.LIPConnectionState.CONNECTED
        assert not lip._reconnecting_event.is_set()
        assert len(bridge.sessions) == 2
        # The two cases really took different paths.
        assert logged in caplog.text

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


@pytest.mark.usefixtures("fast_reconnect")
def test_monitoring_failure_retries_without_a_second_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed re-subscribe is retried, and a concurrent reconnect is a no-op."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)

        setup_monitoring = lip._async_setup_monitoring
        watchdog_reconnects: list[asyncio.Task] = []
        failed_sockets: list[protocol.LIPSocket] = []

        async def fail_once() -> None:
            lip._async_setup_monitoring = setup_monitoring
            failed_sockets.append(lip._socket)
            # The keep-alive watchdog firing mid-reconnect, as it can in
            # production. It must see the reconnect in progress and back off.
            watchdog_reconnects.append(asyncio.create_task(lip._async_disconnected()))
            await asyncio.sleep(0.05)
            msg = "simulated failure while subscribing"
            raise OSError(msg)

        lip._async_setup_monitoring = fail_once
        bridge.reset(0)

        # Session 1 fails its subscriptions, session 2 is the retry.
        await bridge.wait_for_session(2)
        await asyncio.gather(*watchdog_reconnects)

        assert len(bridge.sessions) == 3
        # The half-open session was closed, not left to the garbage collector.
        assert failed_sockets[0]._writer.is_closing()
        assert not bridge.sessions[2].is_closing()
        assert lip.connection_state == protocol.LIPConnectionState.CONNECTED
        assert not lip._reconnecting_event.is_set()
        await _assert_update_received(bridge, 2, messages)

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


@pytest.mark.usefixtures("fast_reconnect")
def test_no_extra_reconnect_after_a_long_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keep-alive timer restarts from the reconnect, not the old response."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)

        # The last keep-alive response is long past, as after a long outage.
        lip._last_keep_alive_response = 0.0
        bridge.reset(0)

        await bridge.wait_for_session(1)
        # The reconnect starts a keep-alive at once; let it run to completion.
        async with asyncio.timeout(5):
            while lip._reconnecting_event.is_set():  # noqa: ASYNC110
                await asyncio.sleep(0.001)
            await lip._keep_alive_reconnect_task

        assert len(bridge.sessions) == 2
        assert not bridge.sessions[1].is_closing()
        await _assert_update_received(bridge, 1, messages)

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


def test_stop_while_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping mid-read ends the reader loop without leaving errors behind."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, _ = await _connect(bridge, monkeypatch)
        await _shutdown(lip, run_task, bridge)
        assert len(bridge.sessions) == 1

    _run(scenario)


@pytest.mark.usefixtures("fast_reconnect")
def test_reconnect_started_outside_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect started elsewhere, as by the keep-alive, interrupts the read."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)

        reconnect = asyncio.create_task(lip._async_disconnected())
        # Let it drop the socket and queue for the lock the reader holds.
        await asyncio.sleep(0)
        # A command now fails cleanly instead of writing to a missing socket.
        with pytest.raises(LIPConnectionStateError):
            await lip.action(LIPMode.OUTPUT, 5, 1, 0)
        await reconnect

        await bridge.wait_for_session(1)
        await _assert_update_received(bridge, 1, messages)
        assert len(bridge.sessions) == 2

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


async def _wait_for_backoff(lip: protocol.LIP) -> None:
    """Wait until a reconnect is sleeping between two attempts."""
    async with asyncio.timeout(5):
        while not (  # noqa: ASYNC110 - polling LIP's own state
            lip._reconnecting_event.is_set()
            and lip._socket is None
            and lip.connection_state == protocol.LIPConnectionState.NOT_CONNECTED
        ):
            await asyncio.sleep(0.001)


def test_connect_during_reconnect_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command between reconnect attempts must not open a second session."""
    monkeypatch.setattr(protocol, "RECONNECT_DELAY", 0.3)

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)

        bridge.refuse = True
        bridge.reset(0)
        await bridge.wait_for_refused(1)
        await _wait_for_backoff(lip)
        bridge.refuse = False

        # What LutronController._ensure_connected() does for a command.
        with pytest.raises(LIPConnectionStateError):
            await lip.async_connect("127.0.0.1", "user", "pass")

        # Session 1 was refused; the retry opens session 2, and only that.
        await bridge.wait_for_session(2)
        await asyncio.sleep(0.2)
        assert len(bridge.sessions) == 3
        assert bridge.open_sessions() == 1
        await _assert_update_received(bridge, 2, messages)

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


@pytest.mark.parametrize("started_by", ["reader", "keepalive"])
def test_stop_during_reconnect(
    monkeypatch: pytest.MonkeyPatch, started_by: str
) -> None:
    """Stopping between reconnect attempts ends the retry loop for good."""
    monkeypatch.setattr(protocol, "RECONNECT_DELAY", 0.3)

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, _ = await _connect(bridge, monkeypatch)

        bridge.refuse = True
        if started_by == "reader":
            # The reader runs the reconnect itself.
            bridge.reset(0)
        else:
            # The reconnect runs in its own task while the reader waits for
            # the lock.
            reconnect = asyncio.create_task(lip._async_disconnected())
        await bridge.wait_for_refused(1)
        await _wait_for_backoff(lip)
        bridge.refuse = False

        await _shutdown(lip, run_task, bridge, stop_bridge=False)
        if started_by == "keepalive":
            await reconnect
        # Longer than RECONNECT_DELAY, so a surviving retry would connect.
        await asyncio.sleep(0.5)

        assert len(bridge.sessions) == 2
        assert bridge.open_sessions() == 0
        assert lip.connection_state == protocol.LIPConnectionState.NOT_CONNECTED
        assert not lip._reconnecting_event.is_set()
        await bridge.stop()

    _run(scenario)


def test_bad_message_does_not_stop_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line the parser rejects is logged and skipped, not fatal."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)

        await bridge.push(0, "~GROUP,3,3")  # no state value: parse() raises
        await _assert_update_received(bridge, 0, messages)
        assert len(bridge.sessions) == 1

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


@pytest.mark.usefixtures("fast_reconnect")
def test_garbled_login_banner_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A protocol error during a reconnect is retried rather than wedging it."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)

        bridge.garble.add(1)
        bridge.reset(0)

        await bridge.wait_for_session(2)
        assert lip.connection_state == protocol.LIPConnectionState.CONNECTED
        assert not lip._reconnecting_event.is_set()
        await _assert_update_received(bridge, 2, messages)

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


@pytest.mark.usefixtures("fast_reconnect")
def test_reconnects_do_not_multiply_keepalives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each reconnect restarts the keep-alive timer instead of adding one."""
    interval = 0.05
    monkeypatch.setattr(protocol, "LIP_KEEP_ALIVE_INTERVAL", interval)

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, _ = await _connect(bridge, monkeypatch)

        for session in range(3):
            bridge.reset(session)
            await bridge.wait_for_session(session + 1)

        await asyncio.sleep(20 * interval)
        # One timer sends about 20 keep-alives here; four would send about 80.
        assert 10 <= bridge.keepalives[3] <= 30

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


@pytest.mark.usefixtures("fast_reconnect")
def test_keepalive_write_failure_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket error on the keep-alive write starts a reconnect."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, messages = await _connect(bridge, monkeypatch)

        async def broken_pipe(_text: str) -> None:
            raise BrokenPipeError

        lip._socket.async_write_command = broken_pipe
        await lip._async_keep_alive_or_reconnect()

        await bridge.wait_for_session(1)
        await _assert_update_received(bridge, 1, messages)

        await _shutdown(lip, run_task, bridge)

    _run(scenario)


def test_stop_while_a_reconnect_is_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect that succeeds after a stop closes its new session."""

    async def scenario() -> None:
        bridge = FakeBridge()
        await bridge.start()
        lip, run_task, _ = await _connect(bridge, monkeypatch)

        open_connection = asyncio.open_connection
        connecting = asyncio.Event()
        release = asyncio.Event()

        async def slow_open_connection(*args: Any, **kwargs: Any) -> Any:
            connecting.set()
            await release.wait()
            return await open_connection(*args, **kwargs)

        monkeypatch.setattr(protocol.asyncio, "open_connection", slow_open_connection)
        bridge.reset(0)
        async with asyncio.timeout(5):
            await connecting.wait()

        await lip.async_stop()
        release.set()
        await bridge.wait_for_session(1)
        async with asyncio.timeout(5):
            await run_task

        assert lip.connection_state == protocol.LIPConnectionState.NOT_CONNECTED
        assert lip._socket is None
        await asyncio.sleep(0.1)
        assert bridge.open_sessions() == 0
        await bridge.stop()

    _run(scenario)
