"""Line-streaming stdout/stderr proxies for the interactive terminal.

Rationale
---------
ZMUX used to capture command output into ``io.StringIO`` and emit the whole
buffer only after the command returned. Two consequences, both user-visible:

* a command that prints progressively (a loop, a build, a download) looked
  frozen and then dumped everything at once;
* ``input("Your name? ")`` wrote its prompt into the captured buffer, so the
  user was asked a question **they could not see** and the terminal appeared
  hung. Interactive stdin was effectively unusable.

Every reference terminal solves this the same way — a reader that forwards
bytes to the display as they arrive (Termux's ``TerminalSession`` reader
thread, Rin's PTY read loop, android-shell's ``StreamGobbler``, whose javadoc
notes that failing to drain promptly can deadlock the child).

:class:`StreamingWriter` is the Python-native equivalent: a text sink that
pushes complete lines to the websocket immediately, while *also* teeing into
a buffer so the REST API keeps returning the full text it always returned.
"""
from __future__ import annotations

import io
from typing import Callable

#: Flush a partial (newline-less) line once it exceeds this many characters,
#: so progress bars and `print(..., end="")` output are not held hostage by a
#: newline that may never come.
PARTIAL_FLUSH_THRESHOLD = 256


class StreamingWriter(io.TextIOBase):
    """A ``sys.stdout``/``sys.stderr`` replacement that streams as it writes.

    ``emit`` receives already-encoded bytes with CRLF line endings, ready for
    xterm.js. Everything written is additionally accumulated in
    :attr:`buffer` so callers that need the whole text (the REST endpoints,
    the test-suite) are unaffected.
    """

    def __init__(self, emit: Callable[[bytes], None]) -> None:
        self._emit = emit
        self._pending = ""
        self.buffer_text: list = []

    # ------------------------------------------------------------- TextIOBase
    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        # Programs branch on this to decide whether to colourise / prompt.
        return True

    def write(self, text) -> int:
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        if not text:
            return 0
        self.buffer_text.append(text)
        self._pending += text
        # Emit every complete line right away.
        if "\n" in self._pending:
            head, _, self._pending = self._pending.rpartition("\n")
            payload = (head + "\n").replace("\r\n", "\n").replace("\n", "\r\n")
            self._emit(payload.encode("utf-8", errors="replace"))
        elif len(self._pending) >= PARTIAL_FLUSH_THRESHOLD:
            self.flush()
        return len(text)

    def flush(self) -> None:
        """Push any buffered partial line (prompts, progress bars, spinners)."""
        if self._pending:
            payload = self._pending.replace("\r\n", "\n").replace("\n", "\r\n")
            self._emit(payload.encode("utf-8", errors="replace"))
            self._pending = ""

    # ----------------------------------------------------------------- extras
    def getvalue(self) -> str:
        """Everything written so far, exactly as written (no CRLF rewriting)."""
        return "".join(self.buffer_text)
