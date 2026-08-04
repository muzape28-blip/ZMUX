package com.zmux.terminal

import android.content.Context
import android.os.Bundle
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient

/**
 * PoC host activity: native Termux TerminalView + WebSocket link to the Python ZMUX backend.
 * Replaces the WebView + xterm.js UI without touching the PTY engine.
 */
class ZmuxTerminalActivity : AppCompatActivity(), TerminalSessionClient,
    WebSocketPtyBridge.StateListener {

    private lateinit var terminalView: ZmuxTerminalView
    private lateinit var statusView: TextView
    private lateinit var urlInput: EditText
    private lateinit var connectButton: Button
    private lateinit var session: ZmuxTerminalSession
    private lateinit var viewClient: ZmuxViewClient
    private var bridge: WebSocketPtyBridge? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_terminal)

        terminalView = findViewById(R.id.terminal_view)
        statusView = findViewById(R.id.status_view)
        urlInput = findViewById(R.id.url_input)
        connectButton = findViewById(R.id.connect_button)

        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        urlInput.setText(prefs.getString(KEY_URL, WebSocketPtyBridge.DEFAULT_URL))

        viewClient = ZmuxViewClient(terminalView)
        terminalView.setTerminalViewClient(viewClient)
        terminalView.applyZmuxDefaults()

        session = ZmuxTerminalSession(this)
        terminalView.attach(session)
        session.feedLine("ZMUX Kotlin PoC — press Connect to attach to the Python backend.")

        connectButton.setOnClickListener { toggleConnection() }
        setupVirtualKeys()
    }

    private fun toggleConnection() {
        val active = bridge
        if (active != null) {
            active.disconnect()
            bridge = null
            connectButton.text = getString(R.string.connect)
            return
        }
        val url = urlInput.text.toString().trim().ifEmpty { WebSocketPtyBridge.DEFAULT_URL }
        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_URL, url).apply()

        val token = intent.getStringExtra(EXTRA_TOKEN)
        bridge = WebSocketPtyBridge(session, WebSocketPtyBridge.Config(url = url, token = token), this)
            .also { it.connect() }
        connectButton.text = getString(R.string.disconnect)
        showKeyboard()
    }

    // --- Virtual key bar (minimal: ESC, CTRL, ALT, TAB, arrows, Ctrl+C) ---
    private fun setupVirtualKeys() {
        fun send(bytes: ByteArray) = session.write(bytes, 0, bytes.size)
        fun sendStr(s: String) = send(s.toByteArray(Charsets.UTF_8))

        findViewById<Button>(R.id.key_esc).setOnClickListener { sendStr("\u001b") }
        findViewById<Button>(R.id.key_tab).setOnClickListener { sendStr("\t") }
        findViewById<Button>(R.id.key_ctrl_c).setOnClickListener { send(byteArrayOf(0x03)) }
        findViewById<Button>(R.id.key_up).setOnClickListener { sendStr("\u001b[A") }
        findViewById<Button>(R.id.key_down).setOnClickListener { sendStr("\u001b[B") }
        findViewById<Button>(R.id.key_left).setOnClickListener { sendStr("\u001b[D") }
        findViewById<Button>(R.id.key_right).setOnClickListener { sendStr("\u001b[C") }

        val ctrl = findViewById<Button>(R.id.key_ctrl)
        ctrl.setOnClickListener {
            viewClient.ctrlDown = !viewClient.ctrlDown
            ctrl.isSelected = viewClient.ctrlDown
        }
        val alt = findViewById<Button>(R.id.key_alt)
        alt.setOnClickListener {
            viewClient.altDown = !viewClient.altDown
            alt.isSelected = viewClient.altDown
        }
        findViewById<Button>(R.id.key_kb).setOnClickListener { showKeyboard() }
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

    // --- WebSocketPtyBridge.StateListener ---
    override fun onState(state: WebSocketPtyBridge.State, detail: String?) {
        statusView.text = buildString {
            append(state.name.lowercase())
            detail?.let { append(" · ").append(it) }
        }
        statusView.visibility = View.VISIBLE
        if (state == WebSocketPtyBridge.State.FAILED) {
            Toast.makeText(this, detail ?: "connection failed", Toast.LENGTH_SHORT).show()
        }
    }

    // --- TerminalSessionClient ---
    override fun onTextChanged(changedSession: TerminalSession) {
        if (::terminalView.isInitialized) terminalView.onScreenUpdated()
    }

    override fun onTitleChanged(changedSession: TerminalSession) = Unit
    override fun onSessionFinished(finishedSession: TerminalSession) {
        onState(WebSocketPtyBridge.State.DISCONNECTED, "session finished")
    }

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
        private const val KEY_URL = "ws_url"
        const val EXTRA_TOKEN = "com.zmux.terminal.TOKEN"
    }
}
