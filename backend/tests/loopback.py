"""Real loopback servers for the status client tests (demo #230).

The findings of the security review of PR #42 were about SOCKET behaviour (a
server that dribbles bytes), which a fake opener cannot show: the fake replaced
exactly the code that was wrong. These servers speak real HTTP over 127.0.0.1.
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DripServer:
    """Answers every request, then dribbles one byte per `interval` seconds.

    mode "body":    a valid 200 header block with a huge Content-Length, then the
                    body one space at a time (every recv stays far under the
                    socket timeout, so only a whole-read deadline can stop it).
    mode "headers": the status line and one header that never ends, one byte at a
                    time (the body deadline is never even reached).
    """

    def __init__(self, mode: str, interval: float = 0.05, lifetime: float = 5.0) -> None:
        assert mode in ("body", "headers")
        self.mode = mode
        self.interval = interval
        # A broken client must fail the test, not hang the run: after this long
        # the server hangs up on its own.
        self.lifetime = lifetime
        self.requests = 0
        self.peer_gone = threading.Event()  # set once a client socket was closed on us
        self._stop = threading.Event()
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True, name="drip-accept").start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            self.requests += 1
            if self.mode == "body":
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
                piece = b" "
            else:
                conn.sendall(b"HTTP/1.1 200 OK\r\nX-A: ")
                piece = b"a"
            end = time.monotonic() + self.lifetime
            while not self._stop.is_set() and time.monotonic() < end:
                conn.sendall(piece)
                self._stop.wait(self.interval)
        except OSError:
            self.peer_gone.set()
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        for conn in self._conns:
            conn.close()


class RedirectServer:
    """302 to ANOTHER PATH ON THE SAME SERVER. A client that follows redirects
    requests that path, which the test can see; a redirect to a closed port (the
    old test) fails the same way whether or not it was followed."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.seen.append(self.path)
                if self.path.startswith("/elsewhere"):
                    body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
