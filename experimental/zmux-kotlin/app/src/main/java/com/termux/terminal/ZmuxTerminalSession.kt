package com.termux.terminal

import android.content.Context

/**
 * One visible terminal tab.
 *
 * A tab starts in the Android host shell so it can run the tiny `linux-setup`
 * bootstrap. Once a verified rootfs exists, [linux] creates a real process
 * chain: Termux TerminalSession PTY -> PRoot from nativeLibraryDir -> guest
 * /bin/sh. There is no pipe-based fake shell in either mode.
 */
class ZmuxTerminalSession(
    private val client: TerminalSessionClient,
    prootPath: String? = null,
    nativeLibraryDir: String? = null,
    rootfsDir: String? = null,
    homeDir: String? = null,
) {
    val session: TerminalSession

    init {
        val filesDir = (client as Context).filesDir.absolutePath
        session = if (
            !prootPath.isNullOrBlank() &&
            !nativeLibraryDir.isNullOrBlank() &&
            !rootfsDir.isNullOrBlank() &&
            !homeDir.isNullOrBlank()
        ) {
            TerminalSessionHelper.createLinuxSession(
                client,
                filesDir,
                prootPath,
                nativeLibraryDir,
                rootfsDir,
                homeDir,
            )
        } else {
            TerminalSessionHelper.createLocalSession(client, filesDir)
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

    fun finishIfRunning() {
        session.finishIfRunning()
    }
}
