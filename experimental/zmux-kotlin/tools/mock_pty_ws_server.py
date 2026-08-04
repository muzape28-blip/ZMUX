#!/usr/bin/env python3
"""Mock ZMUX WebSocket PTY server (stdlib only) for testing the Kotlin UI PoC.

This is a *test harness only*. The real engine lives in `app/zmux/realpty.py` +
`app/zmux/ws_server.py` and must not be reimplemented in Kotlin.

Protocol implemented here (same shape the Kotlin bridge speaks):
  client -> server : binary frames = raw stdin bytes
                     {"type":"resize","rows":R,"cols":C}
                     {"type":"auth","token":"..."}
  server -> client : binary frames = raw PTY output

Run:  python3 tools/mock_pty_ws_server.py --port 8001
Then on an emulator point the app at ws://10.0.2.2:8001/ws
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pty
import select
import socket
import struct
import sys
import termios
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def handshake(conn: socket.socket) -> bool:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk
    key = None
    for line in data.decode("latin-1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if not key:
        return False
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return True


def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def read_frame(conn: socket.socket):
    b1, b2 = recv_exact(conn, 2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if masked else b"\x00\x00\x00\x00"
    payload = bytearray(recv_exact(conn, length))
    for i in range(length):
        payload[i] ^= mask[i % 4]
    return opcode, bytes(payload)


def send_frame(conn: socket.socket, payload: bytes, opcode: int = 0x2) -> None:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 1 << 16:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    conn.sendall(bytes(header) + payload)


def set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def handle(conn: socket.socket, shell: str) -> None:
    try:
        if not handshake(conn):
            conn.close()
            return
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execvp(shell, [shell])
            os._exit(1)

        def pump() -> None:
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 0.2)
                    if fd in r:
                        data = os.read(fd, 4096)
                        if not data:
                            break
                        send_frame(conn, data, 0x2)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        threading.Thread(target=pump, daemon=True).start()

        while True:
            opcode, payload = read_frame(conn)
            if opcode == 0x8:
                break
            if opcode == 0x9:
                send_frame(conn, payload, 0xA)
            elif opcode == 0x2:
                os.write(fd, payload)
            elif opcode == 0x1:
                text = payload.decode("utf-8", "replace")
                try:
                    msg = json.loads(text)
                except ValueError:
                    os.write(fd, payload)
                    continue
                kind = msg.get("type")
                if kind == "resize":
                    set_winsize(fd, int(msg.get("rows", 24)), int(msg.get("cols", 80)))
                elif kind == "input":
                    os.write(fd, msg.get("data", "").encode())
                elif kind == "auth":
                    send_frame(conn, json.dumps({"type": "ok"}).encode(), 0x1)
    except Exception as exc:  # noqa: BLE001
        print(f"[mock] client error: {exc}", file=sys.stderr)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--shell", default=os.environ.get("SHELL", "/bin/sh"))
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(4)
    print(f"[mock] ZMUX mock PTY WS server on ws://{args.host}:{args.port}/ws")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, args.shell), daemon=True).start()


if __name__ == "__main__":
    main()
