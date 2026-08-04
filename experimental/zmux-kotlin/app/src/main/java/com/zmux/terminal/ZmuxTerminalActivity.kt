package com.zmux.terminal

import android.content.Context
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient
import com.termux.terminal.ZmuxTerminalSession
import com.zmux.terminal.widget.BootBrandView
import com.zmux.terminal.widget.KeyCapView
import com.zmux.terminal.widget.SessionTabView
import com.zmux.terminal.widget.StatusPillView

/**
 * Native Kotlin terminal for ZMUX, styled as **ZMUX Ember** (see [ZmuxTheme]).
 *
 * Functionally a peer of the WebView UI — same PTY, same protocol, same key table — but the
 * presentation is its own: true-black field, ember/teal duotone, hand-drawn tabs and keycaps,
 * and a boot overlay that doubles as the connect form instead of a separate top bar.
 */
class ZmuxTerminalActivity : AppCompatActivity(), TerminalSessionClient, WebSocketPtyBridge.Listener {

    private lateinit var terminalView: ZmuxTerminalView
    private lateinit var statusPill: StatusPillView
    private lateinit var hostInput: EditText
    private lateinit var portInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var linkButton: TextView
    private lateinit var bootConnect: TextView
    private lateinit var bootOverlay: View
    private lateinit var bootBrand: BootBrandView
    private lateinit var tabStrip: LinearLayout
    private lateinit var newSessionButton: TextView

    private lateinit var session: ZmuxTerminalSession
    private lateinit var viewClient: ZmuxViewClient

    private var bridge: WebSocketPtyBridge? = null
    private var ctrlKeyCap: KeyCapView? = null
    private var themeApplied = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_terminal)

        terminalView = findViewById(R.id.terminal_view)
        statusPill = findViewById(R.id.status_pill)
        hostInput = findViewById(R.id.host_input)
        portInput = findViewById(R.id.port_input)
        tokenInput = findViewById(R.id.token_input)
        linkButton = findViewById(R.id.link_button)
        bootConnect = findViewById(R.id.boot_connect)
        bootOverlay = findViewById(R.id.boot_overlay)
        bootBrand = findViewById(R.id.boot_brand)
        tabStrip = findViewById(R.id.tab_strip)
        newSessionButton = findViewById(R.id.new_session_button)

        restoreConnectionFields()

        viewClient = ZmuxViewClient(terminalView)
        terminalView.setTerminalViewClient(viewClient)
        terminalView.applyZmuxDefaults()

        session = ZmuxTerminalSession(this)
        terminalView.attach(session)
        
        terminalView.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ ->
            session.onResize?.invoke(session.columns, session.rows)
        }
        
        applyThemeOnce()

        statusPill.setState(WebSocketPtyBridge.State.IDLE, null)

        linkButton.setOnClickListener { toggleConnection() }
        bootConnect.setOnClickListener { toggleConnection() }
        newSessionButton.setOnClickListener { bridge?.newSession() }

        buildVirtualKeys()
    }

    /** Repaint the emulator palette once it exists. */
    private fun applyThemeOnce() {
        if (themeApplied) return
        themeApplied = ZmuxTheme.applyTo(session.emulator)
    }

    // ------------------------------------------------------------------ connect
    private fun restoreConnectionFields() {
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        hostInput.setText(prefs.getString(KEY_HOST, "127.0.0.1"))
        portInput.setText(prefs.getInt(KEY_PORT, ZmuxProtocol.DEFAULT_WS_PORT).toString())
        tokenInput.setText(
            intent.getStringExtra(EXTRA_TOKEN)
                ?: ZmuxBackendLocator.findToken(this, intent.getStringExtra(EXTRA_ZMUX_PACKAGE))
                ?: prefs.getString(KEY_TOKEN, "")
        )
    }

    private fun toggleConnection() {
        bridge?.let {
            it.disconnect()
            bridge = null
            linkButton.text = getString(R.string.connect)
            showBootOverlay(true)
            return
        }

        val host = hostInput.text.toString().trim().ifEmpty { "127.0.0.1" }
        val port = portInput.text.toString().trim().toIntOrNull() ?: ZmuxProtocol.DEFAULT_WS_PORT
        val token = tokenInput.text.toString().trim()

        if (token.isEmpty()) {
            Toast.makeText(this, "Auth token required — ws_server replies 401", Toast.LENGTH_LONG).show()
            return
        }

        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_HOST, host).putInt(KEY_PORT, port).putString(KEY_TOKEN, token).apply()

        bridge = WebSocketPtyBridge(
            session,
            WebSocketPtyBridge.Config(host = host, port = port, token = token),
            this,
        ).also { it.connect() }

        linkButton.text = getString(R.string.disconnect)
    }

    private fun showBootOverlay(visible: Boolean) {
        if (visible) {
            bootOverlay.alpha = 1f
            bootOverlay.visibility = View.VISIBLE
        } else if (bootOverlay.visibility == View.VISIBLE) {
            bootOverlay.animate().alpha(0f).setDuration(220L).withEndAction {
                bootOverlay.visibility = View.GONE
                showKeyboard()
            }.start()
        }
    }

    // ------------------------------------------------------- virtual keys (T3)
    private fun buildVirtualKeys() {
        val container = findViewById<LinearLayout>(R.id.virtual_keys)
        container.removeAllViews()

        for (row in ZmuxKeys.ROWS) {
            val rowView = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            for (key in row) rowView.addView(makeKeyCap(key))
            container.addView(
                HorizontalScrollView(this).apply {
                    isHorizontalScrollBarEnabled = false
                    addView(rowView)
                },
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ),
            )
        }
    }

    private fun makeKeyCap(key: ZmuxKeys.Key): KeyCapView = KeyCapView(this).apply {
        label = key.label
        danger = key.danger
        repeatable = key.repeat

        when {
            key.modifier == "ctrl" -> {
                ctrlKeyCap = this
                onFire = {
                    viewClient.ctrlDown = !viewClient.ctrlDown
                    latched = viewClient.ctrlDown
                    terminalView.requestFocus()
                }
            }

            key.action != null -> onFire = {
                if (key.action == "pty.toggle") bridge?.togglePty()
                terminalView.requestFocus()
            }

            else -> onFire = { sendKeyInput(key.send.orEmpty()) }
        }
    }

    /** Route input through the CTRL latch, mirroring `sendKeyInput()` in terminal.html. */
    private fun sendKeyInput(text: String) {
        if (text.isEmpty()) return
        var payload = text
        if (viewClient.ctrlDown) {
            ZmuxKeys.toControlCode(text)?.let { payload = it }
            viewClient.ctrlDown = false
            ctrlKeyCap?.latched = false
        }
        val bytes = payload.toByteArray(Charsets.UTF_8)
        session.session.write(bytes, 0, bytes.size)
    }

    // ------------------------------------------------------------- tab strip (T2)
    private fun renderTabs(state: ZmuxProtocol.SessionsState) {
        tabStrip.removeAllViews()
        for (info in state.sessions) {
            tabStrip.addView(
                SessionTabView(this).apply {
                    sessionId = info.id
                    isActiveTab = info.id == state.active
                    isBusy = info.busy
                    onTap = { bridge?.switchSession(info.id) }
                    onHoldComplete = { bridge?.closeSession(info.id) }
                }
            )
        }
        val room = state.sessions.size < state.max
        newSessionButton.isEnabled = room
        newSessionButton.alpha = if (room) 1f else 0.35f
    }

    private fun showKeyboard() {
        terminalView.requestFocus()
        (getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager)
            .showSoftInput(terminalView, InputMethodManager.SHOW_IMPLICIT)
    }

    override fun onDestroy() {
        bridge?.disconnect()
        session.finishIfRunning()
        super.onDestroy()
    }

    // -------------------------------------------------- WebSocketPtyBridge.Listener
    override fun onState(state: WebSocketPtyBridge.State, detail: String?) {
        statusPill.setState(state, detail)

        when (state) {
            WebSocketPtyBridge.State.CONNECTED -> {
                applyThemeOnce()
                showBootOverlay(false)
            }

            WebSocketPtyBridge.State.UNAUTHORIZED -> {
                bridge = null
                linkButton.text = getString(R.string.connect)
                showBootOverlay(true)
                bootBrand.tagline = "401 · token rejected"
            }

            else -> Unit
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
    override fun getTerminalCursorStyle(): Int? = 0

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
