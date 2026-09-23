"""Reconnect tests for the LIP protocol against a fake bridge on localhost."""

import asyncio
import gc
import socket
import struct
from collections import defaultdict

import pytest
from aiolip import protocol
from aiolip.data import LIPMode

# The last subscription _async_setup_monitoring() sends.
LAST_MONITORING_COMMAND = "#MONITORING,10,1"
ZONE_UPDATE = "~OUTPUT,5,1,75.00"


class FakeBridge:
    """A minimal LIP server: log in, then wait for the monitoring subscriptions."""

    def __init__(self) -> None:
        """Initialize the fake bridge."""
        self.server: asyncio.Server | None = None
        self.port = 0
        self.sessions: list[asyncio.StreamWriter] = []
        self.monitoring_done: defaultdict[int, asyncio.Event] = defaultdict(
            asyncio.Event
        )

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
        monitoring_done = self.monitoring_done[len(self.sessions) - 1]
        try:
            writer.write(b"login: ")
            await reader.readline()
            writer.write(b"password: ")
            await reader.readline()
            writer.write(b"GNET> ")
            while line := await reader.readline():
                if line.decode().strip() == LAST_MONITORING_COMMAND:
                    monitoring_done.set()
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
    lip: protocol.LIP, run_task: asyncio.Task, bridge: FakeBridge
) -> None:
    await lip.async_stop()
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)
    await bridge.stop()
    # Replaced LIPSockets close their writer in __del__, which needs the loop.
    gc.collect()


@pytest.mark.usefixtures("fast_reconnect")
@pytest.mark.parametrize("drop", ["reset", "close"])
def test_reconnects_and_resubscribes_after_disconnect(
    monkeypatch: pytest.MonkeyPatch, drop: str
) -> None:
    """A TCP reset or a clean EOF reconnects, re-subscribes, and updates resume."""

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

        await _shutdown(lip, run_task, bridge)

    asyncio.run(scenario())


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

        async def fail_once() -> None:
            lip._async_setup_monitoring = setup_monitoring
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
        await asyncio.sleep(0.2)

        assert len(bridge.sessions) == 3
        assert bridge.sessions[1].is_closing()
        assert not bridge.sessions[2].is_closing()
        assert lip.connection_state == protocol.LIPConnectionState.CONNECTED
        assert not lip._reconnecting_event.is_set()
        await _assert_update_received(bridge, 2, messages)

        await _shutdown(lip, run_task, bridge)

    asyncio.run(scenario())


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
        await asyncio.sleep(0.2)

        assert len(bridge.sessions) == 2
        assert not bridge.sessions[1].is_closing()
        await _assert_update_received(bridge, 1, messages)

        await _shutdown(lip, run_task, bridge)

    asyncio.run(scenario())
