package com.zmux.terminal

import android.content.Context
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient

/**
 * Native Kotlin replacement for the WebView + xterm.js terminal.
 *
 * Feature parity targets taken from `app/templates/terminal.html`:
 *  - real PTY output via binary WebSocket frames
 *  - `action`-based resize / session control
 *  - session tab strip driven by `{"type":"sessions"}`
 *  - data-driven virtual key row with sticky CTRL and hold-to-repeat
 */
class ZmuxTerminalActivity : AppCompatActivity(), TerminalSessionClient, WebSocketPtyBridge.Listener {

    private lateinit var terminalView: ZmuxTerminalView
    private lateinit var statusView: TextView
    private lateinit var hostInput: EditText
    private lateinit var portInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var connectButton: Button
    private lateinit var tabStrip: LinearLayout
    private lateinit var session: ZmuxTerminalSession
    private lateinit var viewClient: ZmuxViewClient

    private var bridge: WebSocketPtyBridge? = null
    private var ctrlButton: Button? = null
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_terminal)

        terminalView = findViewById(R.id.terminal_view)
        statusView = findViewById(R.id.status_view)
        hostInput = findViewById(R.id.host_input)
        portInput = findViewById(R.id.port_input)
        tokenInput = findViewById(R.id.token_input)
        connectButton = findViewById(R.id.connect_button)
        tabStrip = findViewById(R.id.tab_strip)

        restoreConnectionFields()

        viewClient = ZmuxViewClient(terminalView)
        terminalView.setTerminalViewClient(viewClient)
        terminalView.applyZmuxDefaults()

        session = ZmuxTerminalSession(this)
        terminalView.attach(session)

        session.feedLine("ZMUX Kotlin UI (PoC) — native TerminalView over the Python PTY engine.")
        session.feedLine("Start the backend, then press Connect.")

        connectButton.setOnClickListener { toggleConnection() }
        findViewById<Button>(R.id.new_session_button).setOnClickListener { bridge?.newSession() }
        buildVirtualKeys()
    }

    // ------------------------------------------------------------------ connect
    private fun restoreConnectionFields() {
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        hostInput.setText(prefs.getString(KEY_HOST, "127.0.0.1"))
        portInput.setText(prefs.getInt(KEY_PORT, ZmuxProtocol.DEFAULT_WS_PORT).toString())

        // Token: intent extra > same-APK token file > saved value.
        val token = intent.getStringExtra(EXTRA_TOKEN)
            ?: ZmuxBackendLocator.findToken(this, intent.getStringExtra(EXTRA_ZMUX_PACKAGE))
            ?: prefs.getString(KEY_TOKEN, "")
        tokenInput.setText(token)
    }

    private fun toggleConnection() {
        bridge?.let {
            it.disconnect()
            bridge = null
            connectButton.text = getString(R.string.connect)
            return
        }

        val host = hostInput.text.toString().trim().ifEmpty { "127.0.0.1" }
        val port = portInput.text.toString().trim().toIntOrNull() ?: ZmuxProtocol.DEFAULT_WS_PORT
        val token = tokenInput.text.toString().trim()

        if (token.isEmpty()) {
            session.feedLine("[zmux] No auth token. ws_server rejects the handshake with 401.")
            session.feedLine("[zmux] Get it via: adb shell run-as <pkg> cat files/.zmux_auth_token")
            Toast.makeText(this, "Auth token required", Toast.LENGTH_LONG).show()
            return
        }

        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_HOST, host).putInt(KEY_PORT, port).putString(KEY_TOKEN, token).apply()

        bridge = WebSocketPtyBridge(
            session,
            WebSocketPtyBridge.Config(host = host, port = port, token = token),
            this,
        ).also { it.connect() }

        connectButton.text = getString(R.string.disconnect)
        showKeyboard()
    }

    // ------------------------------------------------------- virtual keys (T3)
    private fun buildVirtualKeys() {
        val container = findViewById<LinearLayout>(R.id.virtual_keys)
        container.removeAllViews()

        for (row in ZmuxKeys.ROWS) {
            val rowView = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            for (key in row) {
                rowView.addView(makeKeyButton(key))
            }
            val scroll = HorizontalScrollView(this).apply {
                isHorizontalScrollBarEnabled = false
                addView(rowView)
            }
            container.addView(
                scroll,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ),
            )
        }
    }

    private fun makeKeyButton(key: ZmuxKeys.Key): Button {
        val button = Button(this, null, android.R.attr.buttonStyleSmall).apply {
            text = key.label
            isAllCaps = false
            minWidth = 0
            minimumWidth = 0
            setPadding(20, 6, 20, 6)
            if (key.danger) setTextColor(Color.parseColor("#FF8A80"))
        }

        when {
            key.modifier == "ctrl" -> {
                ctrlButton = button
                button.setOnClickListener {
                    viewClient.ctrlDown = !viewClient.ctrlDown
                    updateCtrlButton()
                    terminalView.requestFocus()
                }
            }

            key.action != null -> button.setOnClickListener {
                if (key.action == "pty.toggle") bridge?.togglePty()
                terminalView.requestFocus()
            }

            key.repeat -> attachRepeat(button, key.send.orEmpty())

            else -> button.setOnClickListener { sendKeyInput(key.send.orEmpty()) }
        }
        return button
    }

    /** Hold-to-repeat, matching terminal.html's 400 ms / 55 ms timings. */
    private fun attachRepeat(button: Button, text: String) {
        var repeater: Runnable? = null
        button.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    sendKeyInput(text)
                    val tick = object : Runnable {
                        override fun run() {
                            sendKeyInput(text)
                            handler.postDelayed(this, ZmuxKeys.REPEAT_INTERVAL_MS)
                        }
                    }
                    repeater = tick
                    handler.postDelayed(tick, ZmuxKeys.REPEAT_INITIAL_DELAY_MS)
                    view.isPressed = true
                    true
                }

                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    repeater?.let { handler.removeCallbacks(it) }
                    repeater = null
                    view.isPressed = false
                    view.performClick()
                    true
                }

                else -> false
            }
        }
    }

    /** Route input through the CTRL latch, exactly like `sendKeyInput()` in terminal.html. */
    private fun sendKeyInput(text: String) {
        if (text.isEmpty()) return
        var payload = text
        if (viewClient.ctrlDown) {
            ZmuxKeys.toControlCode(text)?.let { payload = it }
            viewClient.ctrlDown = false
            updateCtrlButton()
        }
        val bytes = payload.toByteArray(Charsets.UTF_8)
        session.write(bytes, 0, bytes.size)
    }

    private fun updateCtrlButton() {
        ctrlButton?.setTextColor(
            if (viewClient.ctrlDown) Color.parseColor("#7FDBFF") else Color.WHITE
        )
    }

    // ------------------------------------------------------------- tab strip (T2)
    private fun renderTabs(state: ZmuxProtocol.SessionsState) {
        tabStrip.removeAllViews()
        for (info in state.sessions) {
            val active = info.id == state.active
            val label = buildString {
                append(if (info.busy) "● " else "")
                append(info.id)
            }
            val tab = Button(this, null, android.R.attr.buttonStyleSmall).apply {
                text = label
                isAllCaps = false
                minWidth = 0
                minimumWidth = 0
                setPadding(24, 4, 24, 4)
                setTextColor(if (active) Color.parseColor("#7FDBFF") else Color.parseColor("#9E9E9E"))
                setOnClickListener { bridge?.switchSession(info.id) }
                setOnLongClickListener {
                    bridge?.closeSession(info.id)
                    true
                }
            }
            tabStrip.addView(tab)
        }
        findViewById<Button>(R.id.new_session_button).isEnabled =
            state.sessions.size < state.max
    }

    private fun showKeyboard() {
        terminalView.requestFocus()
        (getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager)
            .showSoftInput(terminalView, InputMethodManager.SHOW_IMPLICIT)
    }

    override fun onDestroy() {
        bridge?.disconnect()
        super.onDestroy()
    }

    // -------------------------------------------------- WebSocketPtyBridge.Listener
    override fun onState(state: WebSocketPtyBridge.State, detail: String?) {
        statusView.visibility = View.VISIBLE
        statusView.text = buildString {
            append(state.name.lowercase())
            detail?.let { append(" · ").append(it) }
        }
        statusView.setTextColor(
            when (state) {
                WebSocketPtyBridge.State.CONNECTED -> Color.parseColor("#69F0AE")
                WebSocketPtyBridge.State.UNAUTHORIZED,
                WebSocketPtyBridge.State.FAILED -> Color.parseColor("#FF8A80")
                else -> Color.parseColor("#FFD54F")
            }
        )
        if (state == WebSocketPtyBridge.State.UNAUTHORIZED) {
            session.feedLine("[zmux] 401 Unauthorized — the token did not match .zmux_auth_token.")
            bridge = null
            connectButton.text = getString(R.string.connect)
        }
    }

    override fun onSessions(state: ZmuxProtocol.SessionsState) = renderTabs(state)

    // ------------------------------------------------------- TerminalSessionClient
    override fun onTextChanged(changedSession: TerminalSession) {
        if (::terminalView.isInitialized) terminalView.onScreenUpdated()
    }

    override fun onTitleChanged(changedSession: TerminalSession) = Unit
    override fun onSessionFinished(finishedSession: TerminalSession) = Unit
    override fun onCopyTextToClipboard(session: TerminalSession, text: String?) = Unit
    override fun onPasteTextFromClipboard(session: TerminalSession?) = Unit
    override fun onBell(session: TerminalSession) = Unit
    override fun onColorsChanged(session: TerminalSession) = Unit
    override fun onTerminalCursorStateChange(state: Boolean) = Unit
    override fun getTerminalCursorStyle(): Int = 0

    override fun logError(tag: String?, message: String?) = Unit
    override fun logWarn(tag: String?, message: String?) = Unit
    override fun logInfo(tag: String?, message: String?) = Unit
    override fun logDebug(tag: String?, message: String?) = Unit
    override fun logVerbose(tag: String?, message: String?) = Unit
    override fun logStackTraceWithMessage(tag: String?, message: String?, e: Exception?) = Unit
    override fun logStackTrace(tag: String?, e: Exception?) = Unit

    companion object {
        private const val PREFS = "zmux_kotlin"
        private const val KEY_HOST = "ws_host"
        private const val KEY_PORT = "ws_port"
        private const val KEY_TOKEN = "ws_token"

        const val EXTRA_TOKEN = "com.zmux.terminal.TOKEN"
        const val EXTRA_ZMUX_PACKAGE = "com.zmux.terminal.ZMUX_PACKAGE"
    }
}
