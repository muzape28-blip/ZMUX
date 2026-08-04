package com.zmux.terminal

import android.os.Handler
import android.os.Looper
import com.termux.terminal.ZmuxTerminalSession
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import java.util.concurrent.TimeUnit

/**
 * Bridges a [ZmuxTerminalSession] to the real ZMUX WebSocket server (`app/zmux/ws_server.py`).
 *
 * See [ZmuxProtocol] for the transcribed contract. The important corrections versus a naive
 * guess at the protocol:
 *
 *  1. **Auth is `?token=` in the URL**, not an `Authorization` header and not a JSON handshake.
 *     A bad token yields HTTP 401 at upgrade time, so it must be reported as a *handshake*
 *     failure and must NOT be retried forever.
 *  2. **The control key is `action`**, not `type`.
 *  3. **Resize uses `cols`/`rows`** under `action: "resize"`.
 *  4. **Server text frames are output**, except `{"type":"sessions"}` which drives the tab strip.
 */
class WebSocketPtyBridge(
    private val session: ZmuxTerminalSession,
    private val config: Config,
    private val listener: Listener,
) {

    data class Config(
        val host: String = "127.0.0.1",
        val port: Int = ZmuxProtocol.DEFAULT_WS_PORT,
        val token: String,
        val autoReconnect: Boolean = true,
    ) {
        val url: String get() = ZmuxProtocol.wsUrl(host, port, token)
    }

    enum class State { IDLE, CONNECTING, CONNECTED, RECONNECTING, DISCONNECTED, UNAUTHORIZED, FAILED }

    interface Listener {
        fun onState(state: State, detail: String?)
        fun onSessions(state: ZmuxProtocol.SessionsState)
    }

    private val main = Handler(Looper.getMainLooper())
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // long-lived stream
        .pingInterval(20, TimeUnit.SECONDS)    // ws_server replies pong (opcode 9 -> 10)
        .retryOnConnectionFailure(false)       // we own the backoff
        .build()

    private var socket: WebSocket? = null
    private var closedByUser = false
    private var retryDelayMs = 500L

    /** Last size we told the backend about, so we do not spam TIOCSWINSZ. */
    private var lastCols = -1
    private var lastRows = -1

    init {
        session.onInput = ::sendInput
        session.onResize = ::sendResize
    }

    val isConnected: Boolean get() = socket != null

    fun connect() {
        closedByUser = false
        emit(State.CONNECTING, "${config.host}:${config.port}")
        socket = http.newWebSocket(Request.Builder().url(config.url).build(), SocketListener())
    }

    fun disconnect() {
        closedByUser = true
        socket?.close(1000, "client closed")
        socket = null
        lastCols = -1
        lastRows = -1
        emit(State.DISCONNECTED, null)
    }

    /**
     * Raw PTY input. Binary frames (opcode 2) are what terminal.html effectively sends and what
     * `_handle_client_message` treats as terminal input after the JSON sniff fails.
     */
    fun sendInput(bytes: ByteArray) {
        socket?.send(ByteString.of(*bytes))
    }

    fun sendResize(cols: Int, rows: Int) {
        if (cols <= 0 || rows <= 0) return
        if (cols == lastCols && rows == lastRows) return
        lastCols = cols
        lastRows = rows
        socket?.send(ZmuxProtocol.resize(cols, rows))
    }

    fun newSession() = socket?.send(ZmuxProtocol.sessionNew())
    fun switchSession(id: String) = socket?.send(ZmuxProtocol.sessionSwitch(id))
    fun closeSession(id: String) = socket?.send(ZmuxProtocol.sessionClose(id))
    fun listSessions() = socket?.send(ZmuxProtocol.sessionList())
    fun togglePty() = socket?.send(ZmuxProtocol.ptyToggle())

    private fun emit(state: State, detail: String?) = main.post { listener.onState(state, detail) }

    private fun scheduleReconnect(reason: String) {
        socket = null
        if (closedByUser || !config.autoReconnect) {
            emit(State.DISCONNECTED, reason)
            return
        }
        emit(State.RECONNECTING, "$reason · retry in ${retryDelayMs}ms")
        main.postDelayed({ if (!closedByUser) connect() }, retryDelayMs)
        retryDelayMs = (retryDelayMs * 2).coerceAtMost(10_000L)
    }

    private inner class SocketListener : WebSocketListener() {

        override fun onOpen(webSocket: WebSocket, response: Response) {
            retryDelayMs = 500L
            // The server replays RESET + scrollback + sessions on its own; we only need to
            // publish our real geometry so the shell reflows to this device's screen.
            lastCols = -1
            lastRows = -1
            main.post { sendResize(session.columns, session.rows) }
            emit(State.CONNECTED, config.url.substringBefore("?token="))
        }

        /** Binary = raw PTY output. */
        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            val data = bytes.toByteArray()
            main.post { session.feed(data) }
        }

        /** Text = `sessions` control message, or plain output. */
        override fun onMessage(webSocket: WebSocket, text: String) {
            val sessions = ZmuxProtocol.parseServerText(text)
            if (sessions != null) {
                main.post { listener.onSessions(sessions) }
                return
            }
            val data = text.toByteArray(Charsets.UTF_8)
            main.post { session.feed(data) }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            // 401 == bad/stale token. Retrying cannot fix it, so stop and tell the user.
            if (response?.code == 401) {
                socket = null
                closedByUser = true
                emit(State.UNAUTHORIZED, "401 — token rejected by ws_server._verify_token()")
                return
            }
            scheduleReconnect(t.message ?: t.javaClass.simpleName)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            scheduleReconnect("closed($code) $reason")
        }
    }
}
