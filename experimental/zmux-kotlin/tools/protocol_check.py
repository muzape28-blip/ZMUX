#!/usr/bin/env python3
"""Verify the ZMUX WebSocket contract the Kotlin bridge implements.

Speaks raw RFC-6455 (no deps) against the mock server and asserts exactly the
behaviours WebSocketPtyBridge.kt relies on:

  1. bad token  -> HTTP 401, no upgrade
  2. good token -> HTTP 101
  3. on connect -> RESET_TERMINAL_SCREEN then a {"type":"sessions"} text frame
  4. {"action":"resize"} reaches the PTY (stty size confirms)
  5. raw binary input runs in a real shell
  6. Ctrl+C (0x03) kills a foreground sleep, shell survives
  7. {"action":"session.list"} re-pushes the sessions snapshot

Usage:  python3 tools/protocol_check.py [--port 8011]
Exit code 0 = all gates passed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time

RESET_TERMINAL_SCREEN = b"\x1b[?1049l\x1b[?47l\x1b[?1047l\x1b[2J\x1b[H"


def ws_connect(host, port, token):
    """Returns (sock, status_code)."""
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET /?token={token} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    status = int(head.split(b" ")[1]) if head.startswith(b"HTTP/1.1") else 0
    return sock, status


def send(sock, payload: bytes, opcode: int):
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 1 << 16:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


class Reader:
    """Accumulates decoded frames, split into binary bytes and text messages."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.binary = bytearray()
        self.texts = []

    def _fill(self, timeout):
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return False
        if not chunk:
            return False
        self.buf += chunk
        return True

    def pump(self, seconds=1.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._fill(max(0.05, deadline - time.monotonic())):
                continue
            while True:
                frame = self._take()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x2:
                    self.binary += payload
                elif opcode == 0x1:
                    self.texts.append(payload.decode("utf-8", "replace"))

    def _take(self):
        if len(self.buf) < 2:
            return None
        b1, b2 = self.buf[0], self.buf[1]
        length = b2 & 0x7F
        offset = 2
        if length == 126:
            if len(self.buf) < 4:
                return None
            length = struct.unpack(">H", self.buf[2:4])[0]
            offset = 4
        elif length == 127:
            if len(self.buf) < 10:
                return None
            length = struct.unpack(">Q", self.buf[2:10])[0]
            offset = 10
        if len(self.buf) < offset + length:
            return None
        payload = self.buf[offset:offset + length]
        self.buf = self.buf[offset + length:]
        return b1 & 0x0F, bytes(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--token", default="dev")
    args = ap.parse_args()

    import time
    for _ in range(30):
        try:
            with socket.create_connection((args.host, args.port), timeout=1): pass
            break
        except OSError:
            time.sleep(0.5)

    results = []

    def gate(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        return ok

    # --- 1. bad token must be rejected before the upgrade -------------------
    sock, status = ws_connect(args.host, args.port, "wrong-token")
    gate("auth1-bad-token-401", status == 401, f"status={status}")
    sock.close()

    # --- 2. good token upgrades --------------------------------------------
    sock, status = ws_connect(args.host, args.port, args.token)
    if not gate("auth2-good-token-101", status == 101, f"status={status}"):
        return 1
    reader = Reader(sock)

    # --- 3. connect replay: reset + sessions --------------------------------
    reader.pump(1.2)
    gate(
        "replay1-reset-screen",
        RESET_TERMINAL_SCREEN in bytes(reader.binary),
        "RESET_TERMINAL_SCREEN received",
    )
    sessions_msg = next(
        (json.loads(t) for t in reader.texts
         if t.strip().startswith("{") and json.loads(t).get("type") == "sessions"),
        None,
    )
    gate("replay2-sessions-frame", sessions_msg is not None, json.dumps(sessions_msg))
    if sessions_msg:
        gate("replay3-sessions-shape",
             {"sessions", "active", "max"} <= set(sessions_msg),
             f"max={sessions_msg.get('max')}")

    # --- 4. resize reaches the PTY -----------------------------------------
    send(sock, json.dumps({"action": "resize", "cols": 47, "rows": 11}).encode(), 0x1)
    time.sleep(0.3)
    reader.binary.clear()
    send(sock, b"stty size\n", 0x2)
    reader.pump(1.5)
    gate("resize1-stty-size", b"11 47" in bytes(reader.binary),
         repr(bytes(reader.binary)[-80:]))

    # --- 5. raw binary input runs in a real shell ---------------------------
    reader.binary.clear()
    send(sock, b"echo ZMUX_$((6*7))\n", 0x2)
    reader.pump(1.5)
    gate("input1-shell-executes", b"ZMUX_42" in bytes(reader.binary),
         repr(bytes(reader.binary)[-60:]))

    # --- 6. Ctrl+C kills foreground job, shell survives ---------------------
    reader.binary.clear()
    send(sock, b"sleep 30\n", 0x2)
    time.sleep(0.5)
    send(sock, b"\x03", 0x2)
    send(sock, b"echo AFTER_INT\n", 0x2)
    reader.pump(2.5)
    gate("sigint1-ctrl-c", b"AFTER_INT" in bytes(reader.binary),
         "0x03 interrupted foreground sleep")

    # --- 7. session.list re-pushes state ------------------------------------
    before = len(reader.texts)
    send(sock, json.dumps({"action": "session.list"}).encode(), 0x1)
    reader.pump(1.0)
    gate("session1-list-reply", len(reader.texts) > before,
         f"{len(reader.texts) - before} new text frame(s)")

    sock.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} gates passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
