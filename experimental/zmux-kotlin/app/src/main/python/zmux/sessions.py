"""Multiple terminal sessions, addressable by id.

ZMUX previously had exactly one session, created lazily in a module global.
Every reference terminal (Termux, ReTerminal, Rin) offers several, because
one is genuinely limiting: you cannot start a long job and keep working.

Design
------
Each session owns a :class:`~zmux.pty_session.PTYTerminalSession`, which in
turn owns its own :class:`~zmux.python_shell.PythonShell` — so ``cwd``,
Python globals, history and the running command are all naturally isolated.
Nothing needed to be made thread-safe that was not already.

Only the **active** session writes to the websocket. Background sessions keep
running and keep appending to their own scrollback; switching replays the
target's scrollback so the client sees a consistent screen. This mirrors how
ReTerminal's ``SessionService`` keeps a map of sessions while the UI shows
one at a time.

Session ids are short strings (``"1"``, ``"2"``…) chosen by the manager, so
the front-end never invents one.
"""
from __future__ import annotations

import threading
from typing import Optional

from zmux import crash

# A tab can be switched while the previous program (Vim, less, top, …) owns
# xterm's alternate screen. A plain ED2 clear only clears that *same* buffer;
# it does not return to the normal terminal screen, so the next tab can look
# blank or inherit a TUI frame. Always leave the known xterm alternate-buffer
# modes before clearing/replaying a session.
RESET_TERMINAL_SCREEN = b"\x1b[?1049l\x1b[?47l\x1b[?1047l\x1b[2J\x1b[H"


class SessionManager:
    """Owns every terminal session and decides which one is on screen."""

    #: Hard cap. Each session holds a worker thread and a scrollback buffer;
    #: on a low-end phone an unbounded tab list is a memory leak with a UI.
    MAX_SESSIONS = 8

    def __init__(self, ws_server):
        self.ws_server = ws_server
        self._sessions: dict = {}
        self._order: list = []          # creation order, drives tab display
        self._active: Optional[str] = None
        self._counter = 0
        # Geometry belongs to the visible terminal, not every background TUI.
        # A Vim tab must not receive a storm of SIGWINCH events merely because
        # another tab's keyboard opened. Apply the latest geometry when that
        # tab becomes visible again.
        self._cols = 80
        self._rows = 24
        self._lock = threading.RLock()

    # ------------------------------------------------------------- internals
    def _new_id(self) -> str:
        self._counter += 1
        return str(self._counter)

    def _emit_for(self, session_id: str):
        """Build the emit callback for a session.

        Output only reaches the websocket while that session is active;
        otherwise it accumulates in the session's own scrollback.
        """
        def emit(data: bytes) -> None:
            if self._active == session_id:
                self.ws_server.broadcast(data)
        return emit

    # ---------------------------------------------------------------- public
    def create(self, activate: bool = True) -> str:
        """Create a session and return its id. Raises when the cap is hit."""
        from zmux.pty_session import PTYTerminalSession

        with self._lock:
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise ValueError(f"session limit reached ({self.MAX_SESSIONS})")
            previous_active = self._active
            session_id = self._new_id()
            session = PTYTerminalSession(self.ws_server, emit=self._emit_for(session_id))
            self._sessions[session_id] = session
            self._order.append(session_id)
            if activate or self._active is None:
                self._active = session_id
            if activate and previous_active is not None and previous_active != session_id:
                # A new tab is a *new page*: wipe the previous session's
                # screen *before* the fresh session starts writing, exactly
                # like switch(). Without this the new banner/prompt were
                # appended over the old screen — "the tab did not change,
                # there are just more prompts now".
                self.ws_server.broadcast(RESET_TERMINAL_SCREEN)
            session.resize(self._cols, self._rows)
            session.start()
            return session_id

    def get(self, session_id: str):
        with self._lock:
            return self._sessions.get(session_id)

    @property
    def active_id(self) -> Optional[str]:
        return self._active

    @property
    def active(self):
        with self._lock:
            return self._sessions.get(self._active) if self._active else None

    def ids(self) -> list:
        with self._lock:
            return list(self._order)

    def switch(self, session_id: str) -> bool:
        """Make ``session_id`` active and replay its screen. False if unknown."""
        with self._lock:
            if session_id not in self._sessions or session_id == self._active:
                return session_id in self._sessions
            self._active = session_id
            session = self._sessions[session_id]
            cols, rows = self._cols, self._rows
        # A background session receives exactly one resize when it becomes
        # visible, rather than being disturbed by every IME animation.
        session.resize(cols, rows)
        # Clear the client's screen, then repaint from this session's history
        # so switching never shows a mix of two sessions.
        self.ws_server.broadcast(RESET_TERMINAL_SCREEN)
        scrollback = session.get_scrollback()
        if scrollback:
            self.ws_server.broadcast(scrollback)
        return True

    def close(self, session_id: str) -> bool:
        """Terminate a session. Activates a neighbour; never leaves zero."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            index = self._order.index(session_id)
            self._order.remove(session_id)
            was_active = self._active == session_id
            # Prefer the session to the right, matching tabbed-UI convention.
            successor = None
            if self._order:
                successor = self._order[min(index, len(self._order) - 1)]
            if was_active:
                self._active = successor
        try:
            session.stop()
        except Exception as error:
            crash.record("session-close", type(error), error, error.__traceback__)
        if self._active is None:
            # Closing the last tab opens a fresh one rather than leaving the
            # user staring at a dead terminal.
            self.create()
        elif was_active:
            active = self._active
            self._active = None      # force switch() to repaint
            self.switch(active)
        return True

    def write_input(self, data: bytes) -> None:
        """Route keystrokes to the active session."""
        session = self.active
        if session is not None:
            session.write_input(data)

    def resize(self, cols: int, rows: int) -> None:
        """Resize only the displayed terminal and remember it for other tabs.

        Mobile IMEs animate through several viewport heights. Resizing every
        background session on every frame was especially disruptive to Vim and
        other full-screen programs; inactive tabs now catch up on switch().
        """
        if cols <= 0 or rows <= 0:
            return
        with self._lock:
            self._cols, self._rows = cols, rows
            session = self._sessions.get(self._active) if self._active else None
        if session is not None:
            session.resize(cols, rows)

    def snapshot(self) -> dict:
        """State for the front-end tab strip."""
        with self._lock:
            return {
                "sessions": [
                    {"id": sid, "busy": self._sessions[sid]._busy.is_set()}
                    for sid in self._order
                ],
                "active": self._active,
                "max": self.MAX_SESSIONS,
            }

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._order.clear()
            self._active = None
        for session in sessions:
            try:
                session.stop()
            except Exception:
                pass


_manager: Optional[SessionManager] = None


def get_manager(ws_server) -> SessionManager:
    """Return the process-wide session manager, creating it on first use."""
    global _manager
    if _manager is None:
        _manager = SessionManager(ws_server)
        _manager.create()
    return _manager


def reset_manager() -> None:
    """Drop the manager (tests only)."""
    global _manager
    if _manager is not None:
        _manager.stop_all()
    _manager = None
