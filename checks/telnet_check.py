import asyncio
import unittest
from typing import cast
from unittest.mock import AsyncMock, patch

from apps._telnet import TelnetClient


class _Reader:
    def __init__(self, *, at_eof: bool = False) -> None:
        self._at_eof = at_eof

    def at_eof(self) -> bool:
        return self._at_eof


class _Writer:
    def __init__(self, *, write_error: Exception | None = None, close_error: Exception | None = None) -> None:
        self.write_error = write_error
        self.close_error = close_error
        self.closing = False
        self.writes: list[bytes] = []

    def is_closing(self) -> bool:
        return self.closing

    def write(self, data: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closing = True

    async def wait_closed(self) -> None:
        if self.close_error is not None:
            raise self.close_error


class TelnetClientTests(unittest.IsolatedAsyncioTestCase):
    def _client(self) -> TelnetClient:
        client = object.__new__(TelnetClient)
        client.app_alive = lambda: True
        client._host = "localhost"
        client._port = 18081
        client._max_attempts = 1
        client._reader = None
        client._writer = None
        client._setup_lock = asyncio.Lock()
        client._send_lock = asyncio.Lock()
        client.connected_event = asyncio.Event()
        client._running = False
        client._prefix = ""
        client._suffix = "\n"
        return client

    async def test_teardown_clears_state_when_wait_closed_raises(self) -> None:
        client = self._client()
        client._reader = cast(asyncio.StreamReader, cast(object, _Reader()))
        client._writer = cast(
            asyncio.StreamWriter,
            cast(object, _Writer(close_error=BrokenPipeError("closed"))),
        )
        client._running = True
        client.connected_event.set()

        await client.teardown()

        self.assertFalse(client.is_connected)
        self.assertIsNone(client.reader)
        self.assertFalse(client.connected_event.is_set())

    async def test_send_reconnects_once_after_write_failure(self) -> None:
        client = self._client()
        client._reader = cast(asyncio.StreamReader, cast(object, _Reader()))
        client._writer = cast(
            asyncio.StreamWriter,
            cast(object, _Writer(write_error=BrokenPipeError("closed"))),
        )
        client._running = True
        replacement_reader = cast(asyncio.StreamReader, cast(object, _Reader()))
        replacement_writer = _Writer()

        with patch(
            "apps._telnet.asyncio.open_connection",
            new=AsyncMock(
                return_value=(
                    replacement_reader,
                    cast(asyncio.StreamWriter, cast(object, replacement_writer)),
                )
            ),
        ) as open_connection:
            result = await client.send("shutdown")

        self.assertTrue(result)
        open_connection.assert_awaited_once_with("localhost", 18081)
        self.assertEqual(replacement_writer.writes, [b"shutdown\n"])

    async def test_closed_writer_is_not_reported_as_connected(self) -> None:
        client = self._client()
        writer = _Writer()
        writer.closing = True
        client._reader = cast(asyncio.StreamReader, cast(object, _Reader()))
        client._writer = cast(asyncio.StreamWriter, cast(object, writer))
        client._running = True

        self.assertFalse(client.is_connected)


if __name__ == "__main__":
    unittest.main()
