"""Lutron Protocol."""

import asyncio
from collections.abc import Callable
from enum import Enum
import logging
import re
import socket
import time

from .data import LIPMessage, LIPMode, LIPOperation
from .exceptions import LIPConnectionStateError, LIPProtocolError

LIP_PROTOCOL_LOGIN = "login: "
LIP_PROTOCOL_PASSWORD = "password: "
LIP_PROTOCOL_GNET = "GNET> "
LIP_PROTOCOL_QNET = "QNET> "
LIP_PROTOCOL_GENERIC_NET = "NET> "

LIP_USERNAME = "lutron"
LIP_PASSWORD = "integration"
LIP_PORT = 23

LIP_KEEP_ALIVE = (
    "?SYSTEM,10"  # cannot find documentation on this, return the date time of the db?
)

CONNECT_TIMEOUT = 10
SOCKET_TIMEOUT = 45  # The bridge can try to do ident which can take up to 30 seconds

LIP_READ_TIMEOUT = 60
LIP_KEEP_ALIVE_INTERVAL = 60

# Time (in seconds) to wait before attempting to reconnect again after a failed
# connection attempt. This prevents a tight loop that can overwhelm the event
# loop and the bridge when it is unavailable.
RECONNECT_DELAY = 5

_LOGGER = logging.getLogger(__name__)


class LIPControllerType(Enum):
    """Lutron controller type."""

    UNKNOWN = 0
    RADIORA2 = 1
    HOMEWORKS = 2


class LIPConnectionState(Enum):
    """Connection state."""

    NOT_CONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2


class LIPSocket:
    """A socket that reads and writes lip protocol."""

    def __init__(self, reader, writer):
        """Initialize the socket."""
        self._writer = writer
        self._reader = reader

    async def async_readline(self, timeout=SOCKET_TIMEOUT):
        """Read one line from the socket."""
        buffer = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        if buffer == b"":
            return None

        return buffer.decode("UTF-8")

    async def async_readuntil(self, separator, timeout=SOCKET_TIMEOUT):
        """Read until separator is ended."""
        buffer = await asyncio.wait_for(
            self._reader.readuntil(separator.encode("UTF-8")), timeout=timeout
        )
        if buffer == b"":
            return None

        return buffer.decode("UTF-8")

    async def async_write_command(self, text):
        """Write command to lip protocol."""
        self._writer.write(text.encode("UTF-8") + b"\r\n")
        await self._writer.drain()

    def close(self):
        """Cleanup when disconnected."""
        self._writer.close()

    def __del__(self):
        """Cleanup when the object is deleted."""
        self._writer.close()


class LIPParser:
    """Parse a message from the Lutron controller."""

    def __init__(self):
        """Initialize the parser."""
        # Single regex pattern for all modes with optional parameters
        self._pattern = re.compile(r"~(\w+),(\d+),(\d+)(?:,([0-9.]+))?(?:,([0-9.]+))?")

        # Special message types
        self._keepalive_re = re.compile(r"~SYSTEM")
        self._error_re = re.compile(r"~ERROR,(\d+)")
        self._empty_re = re.compile("^[\r\n]+$")
        self._clean_prompt_re = re.compile("^(\x00|\\s*[A-Z]NET>\\s*)+")

        # Internal state
        self._last_keep_alive_response: float = 0.0

    @property
    def last_keepalive(self) -> float:
        """Return last keepalive response."""
        return self._last_keep_alive_response

    def parse(self, response: str) -> LIPMessage | None:
        """Parse a LIP response and update the internal state. Returns a LIPMessage or None."""
        if not response or self._empty_re.match(response):
            return None

        response = self._clean_prompt_re.sub("", response)

        if self._keepalive_re.match(response):
            self._last_keep_alive_response = time.time()
            return LIPMessage(mode=LIPMode.KEEPALIVE, raw=response)

        if self._error_re.match(response):
            return LIPMessage(mode=LIPMode.ERROR, raw=response)

        if not response.startswith(LIPOperation.RESPONSE):
            return None

        try:
            msg = self._parse_message(response)
            msg.raw = response
        except Exception as ex:
            raise ValueError(f"Failed to parse LIP {response} message") from ex
        return msg

    def _parse_message(self, response: str) -> LIPMessage:
        """Unified parser function for all message types."""
        match = self._pattern.match(response)

        if not match:
            raise ValueError(f"Malformed response: {response}")

        mode = LIPMode.from_string(match.group(1))

        if mode == LIPMode.UNKNOWN:
            return LIPMessage(mode=LIPMode.UNKNOWN, raw=response)

        # Extract common fields
        kwargs = {
            "mode": mode,
            "integration_id": int(match.group(2)),  # Always group 2
        }

        # Extract mode-specific fields based on the mode's parser configuration
        for field_name, (group_index, converter) in mode.parser_config.items():
            if group_index is None:
                # Field is always None for this mode
                kwargs[field_name] = None  # type: ignore[assignment]
            else:
                # Extract and convert the value
                raw_value = match.group(group_index)
                kwargs[field_name] = converter(raw_value)

        return LIPMessage(**kwargs)  # type: ignore[arg-type]


class LIP:
    """Async class to speak LIP."""

    def __init__(self):
        """Create the LIP class."""
        self.connection_state = LIPConnectionState.NOT_CONNECTED
        self.controller_type = None
        self._socket = None
        self._host = None
        self._username = LIP_USERNAME  # Default fallback
        self._password = LIP_PASSWORD  # Default fallback
        self._parser = LIPParser()
        self._read_connect_lock = asyncio.Lock()
        self._disconnect_event = asyncio.Event()
        self._reconnecting_event = asyncio.Event()
        self._callback = None  # Only one callback, the coordinator
        self._keep_alive_reconnect_task = None
        self._last_keep_alive_response = None
        self._keep_alive_task = None
        self.loop = None

    async def async_connect(self, server_addr, username=None, password=None):
        """Connect to the bridge via LIP."""
        self.loop = asyncio.get_event_loop()

        # A reconnect in progress owns the connection, even while it waits
        # between attempts with the state at NOT_CONNECTED. Connecting here too
        # would open a second session and start a second reader loop.
        if (
            self.connection_state != LIPConnectionState.NOT_CONNECTED
            or self._reconnecting_event.is_set()
        ):
            raise LIPConnectionStateError

        self._disconnect_event.clear()

        # Store credentials for reconnection
        if username is not None:
            self._username = username
        if password is not None:
            self._password = password

        try:
            await self._async_connect(server_addr)
        except TimeoutError:
            _LOGGER.debug("Timed out while trying to connect to %s", server_addr)
            self.connection_state = LIPConnectionState.NOT_CONNECTED
            raise

        # set the correct monitoring
        await self._async_setup_monitoring()

    async def _async_setup_monitoring(self) -> None:
        """Subscribe to the unsolicited updates we care about."""
        # Monitoring is session scoped, so this has to be re-issued after every
        # reconnect -- not just on the initial connect. Without it the socket is
        # healthy and outgoing commands still work, but the bridge never pushes
        # state changes back and every entity goes stale.
        await self.action(LIPMode.MONITORING, 12, 2)  # disable prompt state
        await self.action(
            LIPMode.MONITORING, 255, 2
        )  # disable everything (not reply state 11, not prompt 12)
        await self.action(LIPMode.MONITORING, 3, 1)  # Button
        await self.action(LIPMode.MONITORING, 4, 1)  # Led
        await self.action(LIPMode.MONITORING, 5, 1)  # Zone
        await self.action(LIPMode.MONITORING, 6, 1)  # Occupancy
        await self.action(LIPMode.MONITORING, 8, 1)  # Scene
        await self.action(LIPMode.MONITORING, 10, 1)  # SysVar

    async def _async_connect(self, server_addr):
        """Make the connection.
        It uses _username and _password stored in the class because this method is called
        also from the reconnect method."""
        _LOGGER.debug("Connecting to %s", server_addr)
        self.connection_state = LIPConnectionState.CONNECTING
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                server_addr,
                LIP_PORT,
                family=socket.AF_INET,
            ),
            timeout=CONNECT_TIMEOUT,
        )
        self._socket = LIPSocket(reader, writer)
        _verify_expected_response(
            await self._socket.async_readuntil(" "), LIP_PROTOCOL_LOGIN
        )
        await self._socket.async_write_command(self._username)
        _verify_expected_response(
            await self._socket.async_readuntil(" "), LIP_PROTOCOL_PASSWORD
        )
        await self._socket.async_write_command(self._password)

        controller_type = await self._socket.async_readuntil(" ")

        _verify_expected_response(controller_type, LIP_PROTOCOL_GENERIC_NET)

        if LIP_PROTOCOL_QNET in controller_type:
            self.controller_type = LIPControllerType.HOMEWORKS
        elif LIP_PROTOCOL_GNET in controller_type:
            self.controller_type = LIPControllerType.RADIORA2
        else:
            self.controller_type = LIPControllerType.UNKNOWN
            _LOGGER.warning("Unknown controller type: %s", controller_type)

        self.connection_state = LIPConnectionState.CONNECTED
        self._host = server_addr
        _LOGGER.debug("Connected to %s", server_addr)

    async def _async_disconnected(self):
        """Reconnect after disconnected."""
        if self._reconnecting_event.is_set():
            return

        if self._socket:
            self._socket.close()
            self._socket = None
        # Set together with dropping the socket, so a command can never pass
        # the CONNECTED check in _async_send_command() with no socket.
        self.connection_state = LIPConnectionState.NOT_CONNECTED

        self._reconnecting_event.set()
        try:
            async with self._read_connect_lock:
                while not self._disconnect_event.is_set():
                    try:
                        await self._async_connect(self._host)
                        await self._async_setup_monitoring()
                    except Exception as ex:  # noqa: BLE001
                        # Deliberately broad. This retry loop is the only thing
                        # that can bring the link back, so nothing may escape
                        # it: an escaping exception leaves _reconnecting_event
                        # set forever, and the guard at the top of this method
                        # then makes every future reconnect a no-op while
                        # connection_state stays CONNECTING -- the integration
                        # wedges until it is reloaded by hand. A bridge that
                        # garbles its login banner mid-handshake raises
                        # LIPProtocolError here, which is not an OSError.
                        _LOGGER.debug("Reconnect to %s failed: %s", self._host, ex)
                        # _async_connect leaves the state at CONNECTING and the
                        # half-open socket assigned when it raises part-way
                        # through; clean both up or we leak a session on the
                        # bridge with every attempt.
                        if self._socket:
                            self._socket.close()
                            self._socket = None
                        self.connection_state = LIPConnectionState.NOT_CONNECTED
                        await asyncio.sleep(RECONNECT_DELAY)
                        continue

                    if self._disconnect_event.is_set():
                        # async_stop() ran while this attempt was in flight.
                        self._socket.close()
                        self._socket = None
                        self.connection_state = LIPConnectionState.NOT_CONNECTED
                        return

                    _LOGGER.info(
                        "Restored monitoring after reconnect to %s", self._host
                    )
                    # The last keep-alive response predates the outage; without
                    # this the first keep-alive after a long outage would find
                    # it stale and immediately reconnect again.
                    self._last_keep_alive_response = time.time()
                    self._keepalive_watchdog()
                    return
        finally:
            # This method owns the event for the whole reconnect, including the
            # monitoring subscriptions. Clearing it any earlier would let the
            # keep-alive watchdog start a second reconnect that closes the
            # socket being set up here.
            self._reconnecting_event.clear()

    async def async_stop(self):
        """Disconnect from the bridge."""
        self._disconnect_event.set()
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            self._keep_alive_task = None
        # The socket is None while a reconnect is between attempts.
        if self._socket:
            self._socket.close()
        self.connection_state = LIPConnectionState.NOT_CONNECTED

    async def _async_keep_alive_or_reconnect(self):
        """Keep alive or reconnect."""
        connection_error = False
        try:
            if self._socket is None:
                _LOGGER.error(
                    "Socket is None, cannot send keep-alive. Attempting reconnect"
                )
                await self._async_disconnected()
                return

            await self._socket.async_write_command(LIP_KEEP_ALIVE)
        except (TimeoutError, OSError) as ex:
            _LOGGER.debug("Lutron bridge disconnected: %s", ex)
            connection_error = True

        if connection_error or self._last_keep_alive_response < time.time() - (
            LIP_KEEP_ALIVE_INTERVAL + SOCKET_TIMEOUT
        ):
            _LOGGER.debug("Lutron bridge keep alive timeout, reconnecting")
            await self._async_disconnected()
            self._last_keep_alive_response = time.time()

    def _keepalive_watchdog(self):
        """Send keepalives."""
        if (
            self._disconnect_event.is_set()
            or self.connection_state != LIPConnectionState.CONNECTED
        ):
            return

        # Drop any still-pending timer first. Every reconnect calls back into
        # this method, and without this each one starts an additional
        # self-rescheduling chain -- N reconnects means N keep-alives a minute
        # against a bridge that is already struggling. Cancelling an
        # already-fired handle (the self-rescheduling case) is a no-op.
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            self._keep_alive_task = None

        self._keep_alive_reconnect_task = asyncio.create_task(
            self._async_keep_alive_or_reconnect()
        )

        self._keep_alive_task = self.loop.call_later(
            LIP_KEEP_ALIVE_INTERVAL, self._keepalive_watchdog
        )

    async def async_run(self):
        """Start interacting with the bridge."""
        if self.connection_state != LIPConnectionState.CONNECTED:
            raise LIPConnectionStateError

        self._last_keep_alive_response = time.time()
        self._keepalive_watchdog()

        while not self._disconnect_event.is_set():
            await self._async_run_once()

    async def _async_run_once(self):
        """Process one message or event."""
        async with self._read_connect_lock:
            if self._disconnect_event.is_set():
                # async_stop() ran while we waited for a reconnect to release
                # the lock, and the socket is gone.
                return
            read_task = asyncio.create_task(self._socket.async_readline())
            disconnect_task = asyncio.create_task(self._disconnect_event.wait())
            reconnecting_task = asyncio.create_task(self._reconnecting_event.wait())
            tasks = (read_task, disconnect_task, reconnecting_task)

            try:
                await asyncio.wait(
                    tasks,
                    timeout=LIP_READ_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Also when async_run() itself is cancelled: asyncio.wait()
                # does not cancel the tasks it waits on.
                for task in tasks:
                    task.cancel()
            # Let the cancellations finish, and mark every exception retrieved:
            # a read that fails just as we stop or reconnect is dropped below,
            # and asyncio would log it as never retrieved. result() below still
            # re-raises it when it is used.
            await asyncio.gather(*tasks, return_exceptions=True)

        if (
            self._disconnect_event.is_set()
            or self._reconnecting_event.is_set()
            # Only if nothing finished within LIP_READ_TIMEOUT.
            or read_task.cancelled()
        ):
            _LOGGER.debug("Stopping run because of disconnect or reconnect")
            return

        try:
            response = read_task.result()
        except TimeoutError:
            return
        except BrokenPipeError as ex:
            _LOGGER.debug("Error processing message", exc_info=ex)
            return
        except OSError as ex:
            # The bridge reset the socket mid-read. Without this the exception
            # escapes async_run(), killing the reader task for good: outgoing
            # commands keep working (the keep-alive watchdog reconnects the
            # socket) but no state update is ever read again. Reconnect and
            # let async_run() loop.
            _LOGGER.info("Lutron connection lost while reading (%s), reconnecting", ex)
            await self._async_disconnected()
            return

        if response is None:
            # EOF: the bridge closed the connection cleanly. Every further read
            # returns EOF at once, so without this the loop would spin.
            _LOGGER.info("Lutron connection closed while reading, reconnecting")
            await self._async_disconnected()
            return

        try:
            self._process_message(response)
        except Exception:
            # A malformed line, or a subscriber that raises, must not escape
            # async_run() and stop all further updates.
            _LOGGER.exception("Error processing Lutron message: %s", response)

    def _process_message(self, response):
        """Process a lip message. This is processing only response (i.e. ~") events."""

        message = self._parser.parse(response)
        _LOGGER.debug("Incoming message: %s", message.raw if message else response)
        if message:
            if message.mode == LIPMode.KEEPALIVE:
                self._last_keep_alive_response = self._parser.last_keepalive
            elif message.mode == LIPMode.ERROR:
                _LOGGER.error("Protocol Error: %s", response)
            elif message.mode != LIPMode.UNKNOWN:
                try:
                    if self._callback:
                        self._callback(message)
                except ValueError:
                    _LOGGER.warning("Error dispatching message: %s", response)
            else:
                _LOGGER.debug("Unknown lutron message: %s", response)

    async def query(self, mode, *args):
        """Query the bridge."""
        await self._async_send_command(LIPOperation.QUERY, mode, *args)

    async def action(self, mode, *args):
        """Do an action on the bridge."""
        await self._async_send_command(LIPOperation.EXECUTE, mode, *args)

    def set_callback(self, callback: Callable[[LIPMessage], None]) -> None:
        """Set the callback for LIP messages."""
        self._callback = callback

    async def _async_send_command(self, protocol_header, mode, *cmd):
        """Send a command."""
        if self.connection_state != LIPConnectionState.CONNECTED:
            raise LIPConnectionStateError

        assert isinstance(mode, LIPMode)

        request = ",".join([mode.name, *[str(key) for key in cmd]])
        _LOGGER.debug("Outgoing message:%s-%s", protocol_header, request)
        await self._socket.async_write_command(f"{protocol_header}{request}")


def _verify_expected_response(received, expected):
    if expected not in received:
        raise LIPProtocolError(received, expected)
