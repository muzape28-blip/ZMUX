package com.termux.terminal

import com.chaquo.python.Python
import com.chaquo.python.PyObject
import com.zmux.terminal.ZmuxTheme

/**
 * Wraps a [TerminalSession] and adapts it for remote PTY use via Chaquopy.
 *
 * `TerminalSession` in Termux is a `final` class that hardcodes a JNI fork() inside
 * `initializeEmulator()`. To bypass this without forking a local process, we inject a 
 * pre-created [TerminalEmulator] via package-private access.
 * We then spin up a Chaquopy Python interpreter in the background which forks the real
 * Alpine PRoot shell and communicates with us by calling `BridgeCallback`.
 */
class ZmuxTerminalSession(
    private val client: TerminalSessionClient,
    private val transcriptRows: Int = 2000,
) {
    val session = TerminalSession("/system/bin/sh", "/", arrayOf<String>(), arrayOf<String>(), transcriptRows, client)

    var onInput: ((ByteArray) -> Unit)? = null
    var onResize: ((Int, Int) -> Unit)? = null

    private var bridge: PyObject? = null

    init {
        // 1. Bypass the JNI fork by pre-seeding the emulator.
        val emulator = TerminalEmulator(session, 80, 24, transcriptRows, client)
        
        // 2. Apply theme immediately.
        ZmuxTheme.applyTo(emulator)
        
        // 3. Inject emulator
        TerminalSessionHelper.injectEmulator(session, emulator)

        // 4. Start Python Bridge
        val py = Python.getInstance()
        val bridgeModule = py.getModule("bridge")
        bridge = bridgeModule.callAttr("PtyBridge", BridgeCallback())
        bridge?.callAttr("start", 80, 24)

        onInput = { bytes ->
            bridge?.callAttr("write", bytes)
        }
        onResize = { cols, rows ->
            bridge?.callAttr("resize", cols, rows)
        }
    }

    inner class BridgeCallback {
        fun onData(data: ByteArray) {
            val emulator = TerminalSessionHelper.getEmulator(session) ?: return
            emulator.append(data, data.size)
            client.onTextChanged(session)
        }
        fun onClosed() {
            client.onSessionFinished(session)
        }
    }

    fun feed(bytes: ByteArray) {
        val emulator = TerminalSessionHelper.getEmulator(session) ?: return
        emulator.append(bytes, bytes.size)
        client.onTextChanged(session)
    }

    fun feedLine(text: String) {
        val bytes = ("\r\n$text\r\n").toByteArray(Charsets.UTF_8)
        val emulator = TerminalSessionHelper.getEmulator(session) ?: return
        emulator.append(bytes, bytes.size)
        client.onTextChanged(session)
    }

    val columns: Int get() = TerminalSessionHelper.getColumns(TerminalSessionHelper.getEmulator(session))
    val rows: Int get() = TerminalSessionHelper.getRows(TerminalSessionHelper.getEmulator(session))

    fun finishIfRunning() {
        bridge?.callAttr("close")
    }
}
