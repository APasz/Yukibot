from __future__ import annotations

import asyncio
import socket
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from aiohttp.abc import ResolveResult
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from archive_safety import ZipArchiveLimits, validated_zip_entries
from node_api_upload import NodeApiRequestBodyLimitMiddleware, persist_upload_to_temp
from safe_http import PublicAddressResolver, UnsafeOutboundUrlError, validate_public_http_url


class ArchiveSafetyTests(unittest.TestCase):
    def test_zip_limits_reject_excessive_file_count(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "save.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("world/one.dat", b"one")
                archive.writestr("world/two.dat", b"two")

            with self.assertRaisesRegex(ValueError, "maximum file count"):
                validated_zip_entries(
                    archive_path,
                    archive_label="Save upload",
                    limits=ZipArchiveLimits(file_count=1),
                )

    def test_zip_limits_reject_high_compression_ratio(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "save.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("world/repeated.dat", b"x" * 4096)

            with self.assertRaisesRegex(ValueError, "compression ratio"):
                validated_zip_entries(
                    archive_path,
                    archive_label="Save upload",
                    limits=ZipArchiveLimits(compression_ratio=2),
                )

    def test_zip_limits_reject_path_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "save.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", b"unsafe")

            with self.assertRaisesRegex(ValueError, "member path is invalid"):
                validated_zip_entries(
                    archive_path,
                    archive_label="Save upload",
                    limits=ZipArchiveLimits(),
                )


class NodeUploadLimitTests(unittest.TestCase):
    def test_streamed_upload_stops_at_configured_limit(self) -> None:
        class _Upload:
            filename: str | None = "save.zip"

            def __init__(self) -> None:
                self._chunks = iter((b"ab", b"cd"))
                self.closed = False

            async def read(self, size: int) -> bytes:
                del size
                return next(self._chunks, b"")

            async def close(self) -> None:
                self.closed = True

        upload = _Upload()
        with self.assertRaisesRegex(ValueError, "3 byte limit"):
            asyncio.run(persist_upload_to_temp(upload, max_bytes=3))
        self.assertTrue(upload.closed)

    def test_request_middleware_rejects_chunked_uploads_that_exceed_limit(self) -> None:
        received_messages: list[Message] = [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
        ]
        sent_messages: list[Message] = []

        async def receive() -> Message:
            return received_messages.pop(0)

        async def send(message: Message) -> None:
            sent_messages.append(message)

        async def app(scope: Scope, receive_message: Receive, send_message: Send) -> None:
            del scope, send_message
            await receive_message()
            await receive_message()

        middleware = NodeApiRequestBodyLimitMiddleware(app, max_bytes=3, api_prefix="/api/node")
        asyncio.run(
            middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/node/apps/example/saves/upload",
                    "headers": [],
                },
                receive,
                send,
            )
        )

        self.assertEqual(sent_messages[0].get("status"), 413)

    def test_request_middleware_returns_413_before_fastapi_body_parsing(self) -> None:
        app = FastAPI()
        app.add_middleware(NodeApiRequestBodyLimitMiddleware, max_bytes=3, api_prefix="/api/node")

        @app.post("/api/node/upload")
        async def _upload(request: Request) -> dict[str, int]:
            return {"bytes": len(await request.body())}

        with TestClient(app) as client:
            response = client.post("/api/node/upload", content=b"1234")

        self.assertEqual(response.status_code, 413)


class SafeHttpTests(unittest.TestCase):
    def test_public_http_url_rejects_private_addresses_and_userinfo(self) -> None:
        for url in (
            "http://127.0.0.1/admin",
            "http://[::1]/admin",
            "http://user:password@example.com/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeOutboundUrlError):
                validate_public_http_url(url)

    def test_public_http_url_accepts_public_https_address(self) -> None:
        parsed = validate_public_http_url("https://1.1.1.1/dns-query")

        self.assertEqual(parsed.hostname, "1.1.1.1")

    def test_public_resolver_rejects_private_dns_answer(self) -> None:
        class _PrivateResolver:
            async def resolve(
                self,
                host: str,
                port: int = 0,
                family: socket.AddressFamily = socket.AddressFamily.AF_INET,
            ) -> list[ResolveResult]:
                del host, port, family
                return [cast(ResolveResult, cast(object, {"host": "169.254.169.254"}))]

            async def close(self) -> None:
                return None

        async def _resolve_private_host() -> None:
            resolver = PublicAddressResolver()
            resolver._resolver = _PrivateResolver()
            with self.assertRaisesRegex(UnsafeOutboundUrlError, "non-public"):
                await resolver.resolve("metadata.example", 80)

        asyncio.run(_resolve_private_host())


if __name__ == "__main__":
    unittest.main()
