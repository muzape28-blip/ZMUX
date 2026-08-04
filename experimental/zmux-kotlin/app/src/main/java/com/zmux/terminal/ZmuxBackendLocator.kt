package com.zmux.terminal

import android.content.Context
import java.io.File
import java.net.Socket

/**
 * Finds the running ZMUX backend: its auth token and its WebSocket port.
 *
 * Two facts from the Python side make this necessary:
 *
 *  - **The token is generated at runtime.** `security.py` writes a 128-bit hex token to
 *    `APP_DIR/.zmux_auth_token` with mode 0600. It is not a build constant.
 *  - **The port is dynamic.** `server.py` prefers `P4A_HTTP_PORT + 1 = 8001`, but falls back
 *    through a range when occupied (`_bind_ws_socket`), so hardcoding 8001 is a guess.
 *
 * ### Reality check for this PoC
 * Android app-private storage is per-UID. A *separate* Kotlin APK cannot read the Python app's
 * `.zmux_auth_token` — that is exactly the sandbox working as intended. So:
 *
 *  - **Same-APK path (the real target, and what Chaquopy gives us):** the Kotlin UI ships inside
 *    the ZMUX app, `appDataDir` resolves to the same `/data/data/<pkg>/files`, and
 *    [findTokenFile] just works.
 *  - **Two-APK dev path (this PoC):** pass the token manually — via the `com.zmux.terminal.TOKEN`
 *    intent extra, or by typing it in the connect bar. Get it with `adb shell run-as <pkg> cat
 *    files/.zmux_auth_token` on a debuggable build.
 *
 * This is the single strongest argument for the Chaquopy step: it deletes this whole class.
 */
object ZmuxBackendLocator {

    const val TOKEN_FILENAME = ".zmux_auth_token"

    /** Candidate locations for the token, same-APK case first. */
    fun candidateTokenPaths(context: Context, zmuxPackage: String?): List<File> {
        val files = ArrayList<File>()
        files += File(context.filesDir, TOKEN_FILENAME)
        context.getExternalFilesDir(null)?.let { files += File(it, TOKEN_FILENAME) }
        // p4a's app dir layout, when the Kotlin UI is embedded in the Python APK.
        files += File(context.filesDir, "app/$TOKEN_FILENAME")
        zmuxPackage?.let { files += File("/data/data/$it/files/$TOKEN_FILENAME") }
        return files
    }

    /** Read the token if any candidate is readable. Returns null in the two-APK case. */
    fun findToken(context: Context, zmuxPackage: String? = null): String? {
        for (file in candidateTokenPaths(context, zmuxPackage)) {
            val token = runCatching {
                if (file.canRead()) file.readText().trim() else null
            }.getOrNull()
            if (!token.isNullOrEmpty() && token.length >= 16) return token
        }
        return null
    }

    /**
     * Probe the WS port range the way `server.py` allocates it: preferred port first, then the
     * fallback window. Returns the first port accepting TCP on [host].
     */
    fun probeWsPort(
        host: String = "127.0.0.1",
        preferred: Int = ZmuxProtocol.DEFAULT_WS_PORT,
        span: Int = 11,
        timeoutMs: Int = 120,
    ): Int? {
        for (port in preferred until preferred + span) {
            val open = runCatching {
                Socket().use { it.connect(java.net.InetSocketAddress(host, port), timeoutMs); true }
            }.getOrDefault(false)
            if (open) return port
        }
        return null
    }
}
