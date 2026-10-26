import asyncio
import ipaddress
import socket
from typing import List, Optional, Set, Tuple
from urllib.parse import urlsplit


class PublicProxyError(ValueError):
    pass


def public_https_authority(authority: str) -> str:
    try:
        parsed = urlsplit(f"https://{authority}")
        port = parsed.port
    except ValueError as exc:
        raise PublicProxyError("Invalid HTTPS destination.") from exc
    if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or port != 443:
        raise PublicProxyError("Only public HTTPS destinations on port 443 are permitted.")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".internal", ".lan", ".test")):
        raise PublicProxyError("Local network destinations are blocked.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    if not address.is_global:
        raise PublicProxyError("Local network destinations are blocked.")
    return hostname


class PublicHttpsProxy:
    def __init__(self) -> None:
        self._server: Optional[asyncio.AbstractServer] = None
        self._writers: Set[asyncio.StreamWriter] = set()
        self._slots = asyncio.Semaphore(64)

    async def start(self) -> str:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        address = self._server.sockets[0].getsockname()
        return f"http://127.0.0.1:{address[1]}"

    async def _resolve(self, hostname: str) -> Tuple[int, str]:
        loop = asyncio.get_running_loop()
        addresses = await asyncio.wait_for(
            loop.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM), timeout=5
        )
        if not addresses:
            raise PublicProxyError("The website did not resolve.")
        candidates: List[Tuple[int, str]] = []
        for family, _, _, _, address in addresses:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global:
                raise PublicProxyError("The website resolved to a private network address.")
            candidates.append((family, str(ip)))
        return candidates[0]

    async def _respond(self, writer: asyncio.StreamWriter, status: int) -> None:
        reason = "Connection Established" if status == 200 else "Forbidden" if status == 403 else "Busy" if status == 503 else "Bad Gateway"
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n\r\n".encode())
        await writer.drain()

    async def _pump(self, source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
        transferred = 0
        while True:
            chunk = await asyncio.wait_for(source.read(65536), timeout=60)
            if not chunk:
                return
            transferred += len(chunk)
            if transferred > 50 * 1024 * 1024:
                return
            target.write(chunk)
            await target.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        remote_writer: Optional[asyncio.StreamWriter] = None
        self._writers.add(writer)
        acquired = False
        try:
            if self._slots.locked():
                await self._respond(writer, 503)
                return
            await self._slots.acquire()
            acquired = True
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(request_line) > 4096:
                await self._respond(writer, 403)
                return
            parts = request_line.decode("ascii", errors="strict").strip().split(" ")
            if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
                await self._respond(writer, 403)
                return
            hostname = public_https_authority(parts[1])
            total_headers = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                total_headers += len(line)
                if total_headers > 16384 or not line:
                    await self._respond(writer, 403)
                    return
                if line == b"\r\n":
                    break
            family, address = await self._resolve(hostname)
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(address, 443, family=family), timeout=10
            )
            self._writers.add(remote_writer)
            await self._respond(writer, 200)
            tasks = {
                asyncio.create_task(self._pump(reader, remote_writer)),
                asyncio.create_task(self._pump(remote_reader, writer)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except (PublicProxyError, UnicodeError):
            if not writer.is_closing():
                await self._respond(writer, 403)
        except (OSError, asyncio.TimeoutError):
            if not writer.is_closing():
                await self._respond(writer, 502)
        finally:
            if acquired:
                self._slots.release()
            self._writers.discard(writer)
            writer.close()
            if remote_writer is not None:
                self._writers.discard(remote_writer)
                remote_writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
