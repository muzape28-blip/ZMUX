package com.zmux.terminal

import android.os.Handler
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Bridges a [ZmuxTerminalSession] to the existing Python ZMUX WebSocket server
 * (`app/zmux/ws_server.py`, default ws://127.0.0.1:8001).
 *
 * Wire protocol (transition layer, kept intentionally tolerant):
 *
 *  Client -> server
 *    - binary frame            : raw keystroke bytes for the PTY (fast path)
 *    - {"type":"auth","token":"..."}          (sent first if a token is configured)
 *    - {"type":"input","data":"<base64|utf8>"} (text fallback, see [useBinaryInput])
 *    - {"type":"resize","rows":R,"cols":C}
 *    - {"type":"ping"}
 *
 *  Server -> client
 *    - binary frame            : raw PTY output, appended verbatim to the emulator
 *    - {"type":"output","data":"..."}         : PTY output as text
 *    - {"type":"exit"|"error"|"ok"|"pong", ...}
 *    - any other text frame is appended verbatim (harmless for plain-text servers)
 */
class WebSocketPtyBridge(
    private val session: ZmuxTerminalSession,
    private val config: Config,
    private val listener: StateListener,
) {

    data class Config(
        val url: String = DEFAULT_URL,
        val token: String? = null,
        /** Send keystrokes as binary frames; set false for text-only servers. */
        val useBinaryInput: Boolean = true,
        val autoReconnect: Boolean = true,
    )

    enum class State { IDLE, CONNECTING, CONNECTED, RECONNECTING, DISCONNECTED, FAILED }

    interface StateListener {
        fun onState(state: State, detail: String?)
    }

    private val main = Handler(Looper.getMainLooper())
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // long-lived stream
        .pingInterval(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private var socket: WebSocket? = null
    private var closedByUser = false
    private var retryDelayMs = 1000L

    init {
        session.onInput = { bytes -> sendInput(bytes) }
        session.onResize = { cols, rows -> sendResize(cols, rows) }
    }

    fun connect() {
        closedByUser = false
        emit(State.CONNECTING, config.url)
        val request = Request.Builder()
            .url(config.url)
            .apply { config.token?.let { header("Authorization", "Bearer $it") } }
            .build()
        socket = http.newWebSocket(request, SocketListener())
    }

    fun disconnect() {
        closedByUser = true
        socket?.close(1000, "client closed")
        socket = null
        emit(State.DISCONNECTED, null)
    }

    fun sendInput(bytes: ByteArray) {
        val ws = socket ?: return
        if (config.useBinaryInput) {
            ws.send(ByteString.of(*bytes))
        } else {
            ws.send(
                JSONObject()
                    .put("type", "input")
                    .put("data", String(bytes, Charsets.UTF_8))
                    .toString()
            )
        }
    }

    fun sendResize(cols: Int, rows: Int) {
        socket?.send(
            JSONObject()
                .put("type", "resize")
                .put("rows", rows)
                .put("cols", cols)
                .toString()
        )
    }

    private fun handshake(ws: WebSocket) {
        config.token?.let {
            ws.send(JSONObject().put("type", "auth").put("token", it).toString())
        }
        sendResize(session.columns, session.rows)
    }

    private fun emit(state: State, detail: String?) = main.post { listener.onState(state, detail) }

    private fun feed(bytes: ByteArray) = main.post { session.feed(bytes) }

    private fun scheduleReconnect(reason: String) {
        if (closedByUser || !config.autoReconnect) {
            emit(State.DISCONNECTED, reason)
            return
        }
        emit(State.RECONNECTING, "$reason — retrying in ${retryDelayMs}ms")
        main.postDelayed({ if (!closedByUser) connect() }, retryDelayMs)
        retryDelayMs = (retryDelayMs * 2).coerceAtMost(15_000L)
    }

    private inner class SocketListener : WebSocketListener() {

        override fun onOpen(webSocket: WebSocket, response: Response) {
            retryDelayMs = 1000L
            handshake(webSocket)
            emit(State.CONNECTED, config.url)
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            feed(bytes.toByteArray())
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val trimmed = text.trimStart()
            if (trimmed.startsWith("{")) {
                runCatching { JSONObject(trimmed) }.getOrNull()?.let { json ->
                    when (json.optString("type")) {
                        "output", "data", "stdout" ->
                            feed(json.optString("data").toByteArray(Charsets.UTF_8))
                        "error" -> emit(State.FAILED, json.optString("message", "server error"))
                        "exit" -> emit(State.DISCONNECTED, "session exited")
                        "ok", "pong", "auth" -> Unit
                        else -> feed(text.toByteArray(Charsets.UTF_8))
                    }
                    return
                }
            }
            feed(text.toByteArray(Charsets.UTF_8))
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            scheduleReconnect(t.message ?: t.javaClass.simpleName)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            scheduleReconnect("closed ($code) $reason")
        }
    }

    companion object {
        const val DEFAULT_URL = "ws://127.0.0.1:8001/ws"
    }
}
