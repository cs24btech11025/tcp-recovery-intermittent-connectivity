#!/usr/bin/env python3
"""Range-capable HTTP sender (the server is the TCP sender, so CUBIC/BBR is set here).

Run inside the srv namespace:
  sudo ip netns exec srv python3 http_range_server.py --cc cubic --size-mb 15
Serves one synthetic file (zeros) of the given size at any path. Supports
"Range: bytes=START-" so a client can resume after reconnecting.
Standard library only.
"""
import argparse
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = b"\0" * 65536
SIZE = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        start, end = 0, SIZE - 1
        m = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range") or "")
        if m:
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), SIZE - 1)
        if start >= SIZE:
            self.send_error(416)
            return
        n = end - start + 1
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
        self.send_header("Content-Length", str(n))
        self.end_headers()
        try:
            left = n
            while left > 0:
                k = min(left, len(CHUNK))
                self.wfile.write(CHUNK if k == len(CHUNK) else CHUNK[:k])
                left -= k
        except OSError:  # client reconnected / reset: just stop
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    cc = b"cubic"

    def get_request(self):
        sock, addr = super().get_request()
        sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_CONGESTION", 13), self.cc)
        return sock, addr


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--cc", default="cubic", choices=["cubic", "bbr", "reno"])
    ap.add_argument("--size-mb", type=int, default=15)
    a = ap.parse_args()
    SIZE = a.size_mb * 1_000_000
    Server.cc = a.cc.encode()
    Server((a.host, a.port), Handler).serve_forever()
