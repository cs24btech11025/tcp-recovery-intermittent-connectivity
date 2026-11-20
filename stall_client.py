#!/usr/bin/env python3
"""Download client for the mitigation comparison. Standard library only.

Run inside the cli namespace:
  sudo ip netns exec cli python3 stall_client.py --strategy watchdog --N 1.0 --out run.json

Strategies (same code path for all, so tool overhead cancels in the comparison):
  plain     one connection, never reconnects on its own (reopens only if the socket errors/EOFs)
  watchdog  if no byte arrives for N seconds: reset the connection and resume with a Range request
  oracle    when the marker file appears (the injector writes it the moment the link returns):
            reconnect immediately IF the connection is stalled (zero detection delay); lower
            bound for what reconnecting can achieve

Reconnects use a short connect timeout and a fresh socket per attempt, so a SYN lost during an
outage is retried every --connect-timeout seconds, not at the kernel's 1 s, 2 s, 4 s backoff.
Output JSON: timeline [[t, bytes_received], ...] (~20 ms resolution), events, total_time, complete.
"""
import argparse
import json
import os
import socket
import struct
import time


def hard_close(s):
    """Close with RST (like an app that gives up on a connection)."""
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
    except OSError:
        pass


def parse_headers(buf):
    i = buf.find(b"\r\n\r\n")
    if i < 0:
        return None
    lines = buf[:i].decode("latin1").split("\r\n")
    status = int(lines[0].split()[1])
    h = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            h[k.strip().lower()] = v.strip()
    return status, h, buf[i + 4:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--strategy", choices=["plain", "watchdog", "oracle"], default="plain")
    ap.add_argument("--N", type=float, default=1.0)
    ap.add_argument("--marker", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-time", type=float, default=150.0)
    ap.add_argument("--connect-timeout", type=float, default=0.25)
    a = ap.parse_args()

    t0 = time.monotonic()  # monotonic: immune to wall-clock jumps (host sleep, NTP/WSL resync)
    st = {"got": 0, "total": None}
    timeline, events = [], []
    last_log = [-1.0]

    def now():
        return time.monotonic() - t0

    def log(force=False):
        t = now()
        if force or t - last_log[0] >= 0.02:
            timeline.append((round(t, 3), st["got"]))
            last_log[0] = t

    def open_conn(offset):
        while now() < a.max_time:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                s.settimeout(a.connect_timeout)
                s.connect((a.host, a.port))
                req = (f"GET /f HTTP/1.1\r\nHost: x\r\nRange: bytes={offset}-\r\n"
                       f"Connection: close\r\n\r\n")
                s.sendall(req.encode())
                s.settimeout(1.0)
                buf = b""
                while True:
                    d = s.recv(65536)
                    if not d:
                        raise OSError("closed before headers")
                    buf += d
                    ph = parse_headers(buf)
                    if ph:
                        break
                status, h, body = ph
                if status not in (200, 206):
                    raise OSError(f"status {status}")
                total = int(h["content-range"].split("/")[1]) if "content-range" in h \
                    else int(h["content-length"])
                events.append(("connected", round(now(), 3)))
                return s, body, total
            except OSError:
                events.append(("connect_retry", round(now(), 3)))
                hard_close(s)
                time.sleep(0.02)
        return None

    c = open_conn(0)
    result = dict(strategy=a.strategy, N=a.N)
    if c is None:
        result.update(complete=False, total_time=None, timeline=timeline, events=events)
        json.dump(result, open(a.out, "w"))
        return
    sock, body, total = c
    st["total"], st["got"] = total, len(body)
    last_progress = now()
    last_mt = None  # oracle: mtime of the marker we last acted on
    sock.settimeout(0.01)
    log(True)

    def reopen(reason):
        nonlocal sock, last_progress
        events.append((reason, round(now(), 3)))
        hard_close(sock)
        c2 = open_conn(st["got"])
        if c2 is None:
            return False
        sock, body2, _ = c2
        sock.settimeout(0.01)
        st["got"] += len(body2)
        last_progress = now()
        log(True)
        return True

    while st["got"] < total and now() < a.max_time:
        stalled = now() - last_progress
        trigger = False
        if a.strategy == "watchdog" and stalled >= a.N:
            trigger = True
        if a.strategy == "oracle" and a.marker and os.path.exists(a.marker):
            mt = os.stat(a.marker).st_mtime_ns
            if mt != last_mt:  # the injector rewrote the marker: link is back
                last_mt = mt
                trigger = stalled > 0.1
        if trigger:
            if not reopen("reconnect"):
                break
            continue
        try:
            d = sock.recv(262144)
            if not d:
                if not reopen("eof"):
                    break
                continue
            st["got"] += len(d)
            last_progress = now()
            log()
        except socket.timeout:
            pass
        except OSError:
            if not reopen("error"):
                break

    complete = st["got"] >= total
    t_done = now()
    log(True)
    hard_close(sock)
    result.update(complete=complete, total_time=round(t_done, 3) if complete else None,
                  total_bytes=total, got=st["got"], timeline=timeline, events=events)
    json.dump(result, open(a.out, "w"))


if __name__ == "__main__":
    main()
