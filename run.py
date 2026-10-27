import asyncio
import socket
import sys
from typing import List

from companion import runtime
from companion.native_bridge import register_host
import uvicorn

from companion.app import app


async def main() -> None:
    sockets: List[socket.socket] = []
    try:
        for port in (8000, 41691):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform == "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                listener.close()
                raise RuntimeError(f"Port {port} is already in use. Close the original desktop app or another companion before starting this one.") from None
            listener.listen(128)
            listener.setblocking(False)
            sockets.append(listener)
        register_host()
        print(f"Browser companion: http://127.0.0.1:8000\nExtension bridge: http://127.0.0.1:41691\nLocal data: {runtime.DATA}", flush=True)
        servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)) for port in (8000, 41691)]
        await asyncio.gather(*(server.serve(sockets=[listener]) for server, listener in zip(servers, sockets)))
    finally:
        for listener in sockets:
            listener.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
