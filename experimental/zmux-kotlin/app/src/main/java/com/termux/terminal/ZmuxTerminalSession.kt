package com.termux.terminal

import com.zmux.terminal.ZmuxTheme

/**
 * Wraps a [TerminalSession] and adapts it for remote PTY use over a WebSocket.
 *
 * `TerminalSession` in Termux is a `final` class that hardcodes a JNI fork() inside
 * `initializeEmulator()`. To bypass this without forking a local process, we inject a 
 * pre-created [TerminalEmulator] via package-private access before [updateSize] is called.
 * This skips `initializeEmulator` entirely. We then set `mShellPid = 1` to trick `write()`
 * into accepting user input, which we intercept from `mTerminalToProcessIOQueue`.
 */
class ZmuxTerminalSession(
    private val client: TerminalSessionClient,
    private val transcriptRows: Int = 2000,
) {
    val session = TerminalSession("/system/bin/sh", "/", arrayOf(), arrayOf(), transcriptRows, client)

    var onInput: ((ByteArray) -> Unit)? = null
    var onResize: ((Int, Int) -> Unit)? = null

    private var running = true
    private var readThread: Thread? = null

    init {
        // 1. Bypass the JNI fork by pre-seeding the emulator.
        val emulator = TerminalEmulator(session, 80, 24, transcriptRows, client)
        
        // 2. Apply theme immediately.
        ZmuxTheme.applyTo(emulator)
        
        // 3. Trick TerminalSession.write() into NOT discarding input, and inject emulator
        TerminalSessionHelper.injectEmulator(session, emulator)

        // 4. Intercept the user's keystrokes.
        readThread = Thread {
            val buffer = ByteArray(4096)
            while (running) {
                val bytes = runCatching { TerminalSessionHelper.readQueue(session, buffer, true) }.getOrDefault(-1)
                if (bytes == -1) break
                onInput?.invoke(buffer.copyOfRange(0, bytes))
            }
        }.apply { start() }
    }

    fun feed(bytes: ByteArray) {
        val emulator = session.getEmulator() ?: return
        emulator.append(bytes, bytes.size)
        client.onTextChanged(session)
    }

    fun feedLine(text: String) = feed(("\r\n$text\r\n").toByteArray(Charsets.UTF_8))

    val emulator: TerminalEmulator? get() = session.getEmulator()
    val columns: Int get() = TerminalSessionHelper.getColumns(session.getEmulator())
    val rows: Int get() = TerminalSessionHelper.getRows(session.getEmulator())

    fun finishIfRunning() {
        running = false
        TerminalSessionHelper.closeQueue(session)
    }
}
