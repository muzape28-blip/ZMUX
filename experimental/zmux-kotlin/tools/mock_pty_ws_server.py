#!/usr/bin/env python3
"""Mock ZMUX WebSocket PTY server (stdlib only) for testing the Kotlin UI PoC.

This is a *test harness only*. The real engine lives in `app/zmux/realpty.py` +
`app/zmux/ws_server.py` and must not be reimplemented in Kotlin.

Protocol implemented here (same shape the Kotlin bridge speaks):
  client -> server : binary/text frames = raw stdin bytes
                     {"action":"resize","cols":C,"rows":R}
                     {"action":"session.new"|"session.switch"|"session.close"|"session.list"}
                     {"action":"pty.toggle"}
  server -> client : binary frames = raw PTY output
                     text frame {"type":"sessions","sessions":[...],"active":id,"max":8}

  Auth is a QUERY PARAMETER, exactly like ws_server._verify_token():
      ws://127.0.0.1:8001/?token=<AUTH_TOKEN>
  A wrong token gets HTTP 401 before the upgrade.

Run:  python3 tools/mock_pty_ws_server.py --port 8001 --token dev

Prints the token it expects on startup.
Then on an emulator point the app at host 10.0.2.2, port 8001.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import pty
import select
import socket
import struct
import sys
import termios
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


RESET_TERMINAL_SCREEN = b"\x1b[?1049l\x1b[?47l\x1b[?1047l\x1b[2J\x1b[H"
MAX_SESSIONS = 8


def handshake(conn: socket.socket, expected_token: str) -> bool:
    """RFC-6455 upgrade + token check, mirroring ws_server._handle_client."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk

    text = data.decode("latin-1")
    request_line = text.split("\r\n", 1)[0]

    # --- token verification (ws_server._verify_token) -----------------------
    try:
        path = request_line.split()[1]
    except IndexError:
        return False
    params = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    supplied = (params.get("token") or [""])[0]
    if not hmac.compare_digest(supplied, expected_token):
        print(f"[mock] 401 rejected token={supplied!r}", file=sys.stderr)
        conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
        return False

    key = None
    for line in text.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if not key:
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
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


def sessions_frame(active="1"):
    return json.dumps({
        "type": "sessions",
        "sessions": [{"id": active, "busy": False}],
        "active": active,
        "max": MAX_SESSIONS,
    }).encode()


def handle(conn: socket.socket, shell: str, token: str) -> None:
    try:
        if not handshake(conn, token):
            conn.close()
            return
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execvp(shell, [shell])
            os._exit(1)

        # Replay order copied from ws_server: reset, scrollback, sessions state.
        send_frame(conn, RESET_TERMINAL_SCREEN, 0x2)
        send_frame(conn, sessions_frame(), 0x1)

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
                continue
            if opcode not in (0x1, 0x2):
                continue

            # ws_server._handle_client_message: JSON sniff first, else raw PTY input.
            handled = False
            try:
                text = payload.decode("utf-8").strip()
                if text.startswith("{") and text.endswith("}"):
                    msg = json.loads(text)
                    action = msg.get("action")
                    if action == "resize":
                        set_winsize(fd, int(msg.get("rows", 24)), int(msg.get("cols", 80)))
                        handled = True
                    elif action in ("session.new", "session.switch",
                                    "session.close", "session.list"):
                        send_frame(conn, sessions_frame(), 0x1)
                        handled = True
                    elif action == "pty.toggle":
                        send_frame(conn, b"\r\n[mock: pty.toggle]\r\n", 0x2)
                        handled = True
            except Exception:
                pass

            if not handled:
                os.write(fd, payload)
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
    ap.add_argument("--token", default="dev", help="token the client must supply as ?token=")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(4)
    print(f"[mock] ZMUX mock PTY WS on ws://{args.host}:{args.port}/?token={args.token}")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, args.shell, args.token), daemon=True).start()


if __name__ == "__main__":
    main()
