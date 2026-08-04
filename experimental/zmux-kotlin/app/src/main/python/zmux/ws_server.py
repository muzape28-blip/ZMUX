"""
ZMUX WebSocket Server — Pure Python RFC-6455 compliant WebSocket server.

Handles real-time bi-directional communication between the xterm.js frontend
and the backend PTY session.
Binds strictly to 127.0.0.1 (loopback only) and authenticates connections
by verifying a secure token during handshake.
"""

import base64
import hashlib
import hmac
import json
import socket
import struct
import threading
import urllib.parse
from typing import Callable, Dict, List, Optional, Set

from zmux.security import AUTH_TOKEN


class WebSocketServer:
    """A pure-Python WebSocket server running on its own thread."""

    # Max seconds a single client send may block before the client is dropped.
    SEND_TIMEOUT_SECONDS = 5.0

    def __init__(self, host: str = "127.0.0.1", port: int = 5001):
        self.host = host
        self.port = port
        self.clients: Set[socket.socket] = set()
        self.clients_lock = threading.Lock()
        self.server_sockets: List[socket.socket] = []
        self.server_socket: Optional[socket.socket] = None
        self.is_running = False
        self.thread: Optional[threading.Thread] = None

        # Callbacks for received events
        self.on_data_callback: Optional[Callable[[bytes], None]] = None
        self.on_resize_callback: Optional[Callable[[int, int], None]] = None

    def start(self, listener: Optional[socket.socket] = None, listeners: Optional[List[socket.socket]] = None) -> None:
        """Start the WebSocket server on a background thread.

        Optionally takes already bound+listening socket(s) so callers can
        eliminate the probe-then-bind race at startup and support dual IPv4/IPv6.
        """
        if listener is not None:
            self.server_sockets = [listener]
            self.server_socket = listener
            self.port = listener.getsockname()[1]
        elif listeners:
            self.server_sockets = list(listeners)
            self.server_socket = self.server_sockets[0]
            self.port = self.server_socket.getsockname()[1]
        self.is_running = True
        self.thread = threading.Thread(target=self._run_server, daemon=True, name="ZMUX-WebSocket-Server")
        self.thread.start()

    def stop(self) -> None:
        """Stop the server and close all connections."""
        self.is_running = False
        for sock in getattr(self, "server_sockets", []):
            try:
                sock.close()
            except Exception:
                pass
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        with self.clients_lock:
            for client in list(self.clients):
                try:
                    client.close()
                except Exception:
                    pass
            self.clients.clear()

    def register_callbacks(self, on_data: Callable[[bytes], None], on_resize: Callable[[int, int], None]) -> None:
        """Register callbacks for input data and terminal resize actions."""
        self.on_data_callback = on_data
        self.on_resize_callback = on_resize

    def broadcast(self, data: bytes) -> None:
        """Broadcast binary data to all connected and authenticated clients.

        Sends happen *outside* clients_lock: previously a send failure here
        called _unregister_client() while already holding clients_lock, which
        deadlocked the PTY reader thread permanently and froze the terminal
        (the classic "app stuck" after a WebView reload or screen rotation).
        """
        with self.clients_lock:
            clients = list(self.clients)

        dead: List[socket.socket] = []
        for client in clients:
            try:
                self._send_frame(client, 2, data)  # Binary frame (opcode 2)
            except Exception:
                # Client disconnected, stalled beyond the send timeout, or failed
                dead.append(client)

        for client in dead:
            self._unregister_client(client)

    def _unregister_client(self, client: socket.socket) -> None:
        with self.clients_lock:
            if client in self.clients:
                self.clients.remove(client)
                try:
                    client.close()
                except Exception:
                    pass

    def _run_server(self) -> None:
        if not getattr(self, "server_sockets", []):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, self.port))
                sock.listen(min(socket.SOMAXCONN, 128))
                self.server_sockets = [sock]
                self.server_socket = sock
            except Exception as e:
                print(f"[ERROR] Failed to bind WebSocket server on {self.host}:{self.port}: {e}")
                self.is_running = False
                return
        self.server_socket = self.server_sockets[0]
        self.port = self.server_socket.getsockname()[1]
        print(f"[INFO] Secure WebSocket Server listening on {self.host}:{self.port}")

        for sock in self.server_sockets:
            t = threading.Thread(target=self._accept_loop, args=(sock,), daemon=True)
            t.start()

    def _accept_loop(self, sock: socket.socket) -> None:
        while self.is_running:
            try:
                client_sock, _ = sock.accept()
                if not self.is_running:
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                    break
                # Handle each client in a separate thread
                t = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
                t.start()
            except Exception:
                break

    def _handle_client(self, sock: socket.socket) -> None:
        try:
            # 1. Read HTTP Handshake Headers
            headers, request_line = self._read_http_headers(sock)
            if not headers or not request_line:
                sock.close()
                return

            # 2. Token Security Verification
            if not self._verify_token(request_line):
                print(f"[WARN] WebSocket unauthorized access attempt rejected from {sock.getpeername()}")
                sock.sendall(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
                sock.close()
                return

            # 3. Complete WebSocket Handshake
            sec_key = headers.get("sec-websocket-key")
            if not sec_key:
                sock.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                sock.close()
                return

            accept_key = self._compute_accept_key(sec_key)
            handshake_response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key}\r\n\r\n"
            )
            sock.sendall(handshake_response.encode("utf-8"))

            # Bound the send wait for this client: a stalled (e.g. suspended
            # WebView) peer must surface as an exception instead of blocking
            # sendall() — and with it the whole PTY output pipeline — forever.
            self._set_send_timeout(sock)

            # Register client
            with self.clients_lock:
                self.clients.add(sock)

            # Replay the active session's scrollback so a reload, rotation or
            # reconnect restores the screen instead of showing a blank terminal.
            try:
                from zmux.sessions import RESET_TERMINAL_SCREEN, get_manager
                session = get_manager(self).active
                scrollback = session.get_scrollback() if session else b""
                # A reconnect can happen while Vim/less owns xterm's alternate
                # buffer. Reset it before replaying the selected session so a
                # normal shell tab never inherits a stale TUI screen.
                self._send_frame(sock, 2, RESET_TERMINAL_SCREEN)
                if scrollback:
                    self._send_frame(sock, 2, scrollback)
                self._send_sessions_state()
            except Exception as e:
                print(f"[WARN] Failed to send initial scrollback: {e}")

            # 4. Message Loop
            while self.is_running:
                opcode, payload = self._read_frame(sock)
                if opcode == 8:  # Connection close
                    break
                elif opcode == 9:  # Ping
                    self._send_frame(sock, 10, payload)  # Pong
                elif opcode in (1, 2):  # Text or Binary data
                    self._handle_client_message(payload)

        except (ConnectionError, OSError):
            pass  # Normal disconnect (reload, rotation, app backgrounded)
        except Exception as e:
            print(f"[WARN] WebSocket client handler ended with error: {e}")
        finally:
            self._unregister_client(sock)

    def _handle_client_message(self, payload: bytes) -> None:
        """Parse incoming websocket message and route it appropriately."""
        # Try to parse as JSON first (control actions: resize, session mgmt)
        try:
            msg_str = payload.decode("utf-8")
            if msg_str.strip().startswith("{") and msg_str.strip().endswith("}"):
                data = json.loads(msg_str)
                action = data.get("action")
                if action == "resize":
                    cols = int(data.get("cols", 80))
                    rows = int(data.get("rows", 24))
                    if self.on_resize_callback:
                        self.on_resize_callback(cols, rows)
                    return
                if action in ("session.new", "session.switch", "session.close", "session.list"):
                    self._handle_session_action(action, data)
                    return
                if action == "pty.toggle":
                    # Ctrl+B from the frontend: jump between the real Alpine
                    # PTY shell and the ZMUX host console.
                    from zmux.sessions import get_manager
                    session = get_manager(self).active
                    if session is not None:
                        session.toggle_pty()
                    return
        except Exception:
            pass

        # Otherwise, treat as raw interactive terminal input
        if self.on_data_callback:
            self.on_data_callback(payload)

    def _handle_session_action(self, action: str, data: dict) -> None:
        """Create / switch / close terminal sessions on the client's behalf."""
        from zmux.sessions import get_manager

        manager = get_manager(self)
        try:
            if action == "session.new":
                manager.switch(manager.create())
            elif action == "session.switch":
                manager.switch(str(data.get("id", "")))
            elif action == "session.close":
                manager.close(str(data.get("id", "")))
        except ValueError as error:
            # e.g. session cap reached — tell the user rather than failing mute.
            self.broadcast(f"\r\n[zmux: {error}]\r\n".encode("utf-8"))
        self._send_sessions_state()

    def _send_sessions_state(self) -> None:
        """Push the tab strip state to every client."""
        from zmux.sessions import get_manager

        payload = json.dumps({"type": "sessions", **get_manager(self).snapshot()})
        message = payload.encode("utf-8")
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            try:
                self._send_frame(client, 1, message)  # text frame
            except Exception:
                self._unregister_client(client)

    def _verify_token(self, request_line: str) -> bool:
        """Constant-time token verification from request query parameters."""
        try:
            parts = request_line.split()
            if len(parts) < 2:
                return False
            path = parts[1]
            parsed = urllib.parse.urlparse(path)
            params = urllib.parse.parse_qs(parsed.query)
            token_list = params.get("token", [])
            if not token_list:
                return False
            return hmac.compare_digest(token_list[0], AUTH_TOKEN)
        except Exception:
            return False

    def _compute_accept_key(self, sec_key: str) -> str:
        GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept_val = sec_key + GUID
        sha1 = hashlib.sha1(accept_val.encode("utf-8")).digest()
        return base64.b64encode(sha1).decode("utf-8")

    def _read_http_headers(self, sock: socket.socket) -> tuple[Dict[str, str], str]:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buffer += chunk
        if not buffer:
            return {}, ""

        parts = buffer.split(b"\r\n\r\n", 1)
        header_part = parts[0].decode("utf-8", errors="ignore")
        lines = header_part.split("\r\n")
        request_line = lines[0]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, val = line.split(":", 1)
                headers[key.strip().lower()] = val.strip()
        return headers, request_line

    def _recv_exact(self, sock: socket.socket, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Connection closed while receiving payload")
            data += chunk
        return data

    def _read_frame(self, sock: socket.socket) -> tuple[int, bytes]:
        first_byte = self._recv_exact(sock, 1)[0]
        fin = (first_byte & 0x80) != 0
        opcode = first_byte & 0x0F

        second_byte = self._recv_exact(sock, 1)[0]
        masked = (second_byte & 0x80) != 0
        payload_len = second_byte & 0x7F

        if payload_len == 126:
            len_bytes = self._recv_exact(sock, 2)
            payload_len = int.from_bytes(len_bytes, "big")
        elif payload_len == 127:
            len_bytes = self._recv_exact(sock, 8)
            payload_len = int.from_bytes(len_bytes, "big")

        if masked:
            masking_key = self._recv_exact(sock, 4)
        else:
            masking_key = None

        payload = self._recv_exact(sock, payload_len)

        if masked and masking_key:
            payload = bytes(b ^ masking_key[i % 4] for i, b in enumerate(payload))

        return opcode, payload

    def _set_send_timeout(self, sock: socket.socket) -> None:
        """Bound send() waits via SO_SNDTIMEO (POSIX/Linux/Bionic, best-effort).

        A WebView that is suspended or no longer draining its TCP receive
        buffer would otherwise let sendall() block indefinitely while the PTY
        reader thread is broadcasting, freezing all terminal output.
        """
        try:
            if hasattr(socket, "SO_SNDTIMEO"):
                seconds = int(self.SEND_TIMEOUT_SECONDS)
                micros = int((self.SEND_TIMEOUT_SECONDS - seconds) * 1_000_000)
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_SNDTIMEO,
                    struct.pack("ll", seconds, micros),
                )
        except OSError:
            # Platform without usable SO_SNDTIMEO (e.g. some 32-bit ABIs):
            # sends stay blocking, which is the old behaviour — acceptable.
            pass

    def _send_frame(self, sock: socket.socket, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)

        payload_len = len(payload)
        if payload_len < 126:
            header.append(payload_len)
        elif payload_len <= 65535:
            header.append(126)
            header.extend(payload_len.to_bytes(2, "big"))
        else:
            header.append(127)
            header.extend(payload_len.to_bytes(8, "big"))

        sock.sendall(header + payload)
