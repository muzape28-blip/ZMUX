package com.zmux.terminal

import com.termux.terminal.TerminalEmulator
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient

/**
 * A [TerminalSession] that is NOT backed by a locally forked process.
 *
 * The real PTY lives in the existing Python engine (`app/zmux/realpty.py`), reached over a
 * WebSocket. This class therefore:
 *
 *  - overrides [initializeEmulator] so no `fork()` / JNI PTY is created on the Android side,
 *  - forwards every byte the emulator wants to send to the "process" ([write]) to the bridge,
 *  - exposes [feed] so bytes arriving from the backend are appended to the emulator.
 *
 * Nothing about the Python PTY engine is reimplemented here — this is purely a UI-side adapter.
 */
class ZmuxTerminalSession(
    private val client: TerminalSessionClient,
    private val transcriptRows: Int = 2000,
) : TerminalSession(
    /* shellPath = */ "/system/bin/sh",
    /* cwd = */ "/",
    /* args = */ arrayOf(),
    /* env = */ arrayOf(),
    /* transcriptRows = */ transcriptRows,
    /* client = */ client,
) {

    /** Set by [WebSocketPtyBridge]; receives user keystrokes as raw bytes. */
    var onInput: ((ByteArray) -> Unit)? = null

    /** Set by [WebSocketPtyBridge]; receives terminal resize events (cols, rows). */
    var onResize: ((Int, Int) -> Unit)? = null

    private var running = true

    override fun initializeEmulator(columns: Int, rows: Int) {
        // Deliberately does not call TerminalSession#initializeEmulator(), which would fork a
        // local shell through the Termux JNI PTY. We only need the emulator/screen buffer.
        mEmulator = TerminalEmulator(this, columns, rows, transcriptRows)
        onResize?.invoke(columns, rows)
        client.onTextChanged(this)
    }

    override fun updateSize(columns: Int, rows: Int) {
        val emulator = mEmulator
        if (emulator == null) {
            initializeEmulator(columns, rows)
            return
        }
        if (emulator.mColumns == columns && emulator.mRows == rows) return
        emulator.resize(columns, rows)
        onResize?.invoke(columns, rows)
    }

    /** Called by the emulator with user input destined for the remote PTY. */
    override fun write(data: ByteArray?, offset: Int, count: Int) {
        if (data == null || count <= 0) return
        onInput?.invoke(data.copyOfRange(offset, offset + count))
    }

    override fun write(data: String?) {
        if (data.isNullOrEmpty()) return
        val bytes = data.toByteArray(Charsets.UTF_8)
        write(bytes, 0, bytes.size)
    }

    /** Feed output coming from the backend PTY into the emulator. */
    fun feed(bytes: ByteArray) {
        val emulator = mEmulator ?: return
        emulator.append(bytes, bytes.size)
        client.onTextChanged(this)
    }

    /** Convenience for status/banner lines rendered locally (connection notices, errors). */
    fun feedLine(text: String) = feed(("\r\n" + text + "\r\n").toByteArray(Charsets.UTF_8))

    override fun isRunning(): Boolean = running

    override fun finishIfRunning() {
        running = false
    }

    val columns: Int get() = mEmulator?.mColumns ?: 80
    val rows: Int get() = mEmulator?.mRows ?: 24
}
