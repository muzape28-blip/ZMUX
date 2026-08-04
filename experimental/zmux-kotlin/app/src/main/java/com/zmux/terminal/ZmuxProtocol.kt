package com.zmux.terminal

import org.json.JSONArray
import org.json.JSONObject

/**
 * The **real** ZMUX WebSocket contract, transcribed from the Python source so the Kotlin UI
 * is a drop-in replacement for `app/templates/terminal.html`.
 *
 * Sources of truth:
 *  - `app/zmux/ws_server.py`      — framing, auth, `action` routing, `sessions` push
 *  - `app/zmux/sessions.py`       — `RESET_TERMINAL_SCREEN`, `MAX_SESSIONS = 8`, snapshot shape
 *  - `app/zmux/security.py`       — token file + constant-time compare
 *  - `app/zmux/server.py`         — port selection (`WS_PORT = HTTP_PORT + 1`, dynamic)
 *  - `app/templates/terminal.html`— the xterm.js client this UI replaces
 *
 * ## Auth
 * The token is **a query parameter**, not a header and not a JSON message:
 *
 *     ws://127.0.0.1:<WS_PORT>/?token=<AUTH_TOKEN>
 *
 * `ws_server._verify_token()` parses `request_line`, reads `params["token"]`, and compares with
 * `hmac.compare_digest`. A wrong/missing token gets `HTTP/1.1 401 Unauthorized` **before** the
 * WebSocket upgrade — so it surfaces as a handshake failure, not a close frame.
 *
 * ## Client -> server
 * `_handle_client_message()` accepts opcode 1 (text) and 2 (binary) identically. It tries JSON
 * first and falls back to raw PTY input. Note the discriminator is **`action`**, not `type`.
 *
 * | Message | Effect |
 * |---|---|
 * | raw bytes (any opcode) | written straight to the PTY master |
 * | `{"action":"resize","cols":C,"rows":R}` | `TIOCSWINSZ` on the master |
 * | `{"action":"session.new"}` | create + switch |
 * | `{"action":"session.switch","id":"..."}` | switch active session |
 * | `{"action":"session.close","id":"..."}` | close session |
 * | `{"action":"session.list"}` | re-push the `sessions` snapshot |
 * | `{"action":"pty.toggle"}` | toggle Alpine PTY <-> ZMUX host console |
 *
 * > A JSON payload is only treated as a control message when the *trimmed* text both starts
 * > with `{` and ends with `}`. Anything else is PTY input verbatim — which is why typing a
 * > literal `{...}` line in the shell still works.
 *
 * ## Server -> client
 * | Frame | Meaning |
 * |---|---|
 * | binary (opcode 2) | raw PTY output — append to the emulator |
 * | text (opcode 1) `{"type":"sessions",...}` | tab-strip state |
 * | text (opcode 1), anything else | plain output (e.g. `[zmux: session cap reached]`) |
 *
 * On connect the server replays state in this exact order:
 *   1. [RESET_TERMINAL_SCREEN] — clears xterm's alternate buffer so a reconnect during
 *      Vim/less does not inherit a stale TUI screen,
 *   2. the active session's scrollback (binary),
 *   3. a `sessions` snapshot (text).
 */
object ZmuxProtocol {

    /** `sessions.py:35` — exit alt-buffer (1049/47/1047), clear screen, home cursor. */
    val RESET_TERMINAL_SCREEN: ByteArray = byteArrayOf(
        0x1b, '['.code.toByte(), '?'.code.toByte(), '1'.code.toByte(), '0'.code.toByte(),
        '4'.code.toByte(), '9'.code.toByte(), 'l'.code.toByte(),
        0x1b, '['.code.toByte(), '?'.code.toByte(), '4'.code.toByte(), '7'.code.toByte(),
        'l'.code.toByte(),
        0x1b, '['.code.toByte(), '?'.code.toByte(), '1'.code.toByte(), '0'.code.toByte(),
        '4'.code.toByte(), '7'.code.toByte(), 'l'.code.toByte(),
        0x1b, '['.code.toByte(), '2'.code.toByte(), 'J'.code.toByte(),
        0x1b, '['.code.toByte(), 'H'.code.toByte(),
    )

    /** `sessions.py:43` — `SessionManager.MAX_SESSIONS`. */
    const val MAX_SESSIONS = 8

    /** `server.py:63,133` — WS_PORT defaults to P4A_HTTP_PORT + 1, but is chosen dynamically. */
    const val DEFAULT_HTTP_PORT = 8000
    const val DEFAULT_WS_PORT = 8001

    fun wsUrl(host: String, port: Int, token: String): String =
        "ws://$host:$port/?token=${java.net.URLEncoder.encode(token, "UTF-8")}"

    // ------------------------------------------------------------------ outbound
    fun resize(cols: Int, rows: Int): String =
        JSONObject().put("action", "resize").put("cols", cols).put("rows", rows).toString()

    fun sessionNew(): String = JSONObject().put("action", "session.new").toString()

    fun sessionSwitch(id: String): String =
        JSONObject().put("action", "session.switch").put("id", id).toString()

    fun sessionClose(id: String): String =
        JSONObject().put("action", "session.close").put("id", id).toString()

    fun sessionList(): String = JSONObject().put("action", "session.list").toString()

    fun ptyToggle(): String = JSONObject().put("action", "pty.toggle").toString()

    // ------------------------------------------------------------------ inbound
    data class SessionInfo(val id: String, val busy: Boolean)

    data class SessionsState(
        val sessions: List<SessionInfo>,
        val active: String?,
        val max: Int,
    )

    /**
     * Parse a server text frame. Returns a [SessionsState] for `{"type":"sessions"}`, or null
     * when the frame is ordinary terminal output that should be fed to the emulator.
     *
     * Mirrors `handleControlMessage()` in terminal.html: only `type == "sessions"` is control.
     */
    fun parseServerText(text: String): SessionsState? {
        val trimmed = text.trim()
        if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return null
        val json = runCatching { JSONObject(trimmed) }.getOrNull() ?: return null
        if (json.optString("type") != "sessions") return null

        val array: JSONArray = json.optJSONArray("sessions") ?: JSONArray()
        val list = ArrayList<SessionInfo>(array.length())
        for (i in 0 until array.length()) {
            val item = array.optJSONObject(i) ?: continue
            list += SessionInfo(item.optString("id"), item.optBoolean("busy", false))
        }
        return SessionsState(
            sessions = list,
            active = json.optString("active").ifEmpty { null },
            max = json.optInt("max", MAX_SESSIONS),
        )
    }
}
