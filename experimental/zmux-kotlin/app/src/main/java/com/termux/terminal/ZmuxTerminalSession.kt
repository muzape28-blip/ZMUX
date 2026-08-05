package com.termux.terminal

import android.content.Context

class ZmuxTerminalSession(
    private val client: TerminalSessionClient
) {
    val session: TerminalSession
    
    init {
        val filesDir = (client as Context).filesDir.absolutePath
        session = TerminalSessionHelper.createLocalSession(client, filesDir)
    }

    var onResize: ((Int, Int) -> Unit)? = null

    fun feedLine(text: String) {
        val bytes = ("\r\n$text\r\n").toByteArray(Charsets.UTF_8)
        val emulator = TerminalSessionHelper.getEmulator(session) ?: return
        emulator.append(bytes, bytes.size)
        client.onTextChanged(session)
    }

    val columns: Int get() = TerminalSessionHelper.getColumns(TerminalSessionHelper.getEmulator(session))
    val rows: Int get() = TerminalSessionHelper.getRows(TerminalSessionHelper.getEmulator(session))

    fun finishIfRunning() {
        session.finishIfRunning()
    }
}
