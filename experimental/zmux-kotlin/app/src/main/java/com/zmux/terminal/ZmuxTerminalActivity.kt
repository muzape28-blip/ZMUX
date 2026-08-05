package com.zmux.terminal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Bundle
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient
import com.termux.terminal.ZmuxTerminalSession
import com.termux.terminal.TerminalSessionHelper
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.termux.view.TerminalView
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
class ZmuxTerminalActivity : AppCompatActivity(), TerminalSessionClient {

    private lateinit var terminalView: TerminalView
    private lateinit var statusPill: StatusPillView
    private lateinit var tabStrip: LinearLayout
    private lateinit var newSessionButton: TextView
    private lateinit var viewClient: ZmuxViewClient

    private val sessions = mutableListOf<ZmuxTerminalSession>()
    private var activeSessionIndex = 0
    private var sessionCounter = 1

    private var ctrlKeyCap: KeyCapView? = null

    private val installReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val osName = intent?.getStringExtra("os") ?: return
            val currentSession = activeSession() ?: return
            
            val esc = 27.toChar()
            currentSession.feedLine("${esc}[34m[Chaquopy]${esc}[0m Bootstrapping Python Engine for $osName...")
            
            Thread {
                try {
                    val py = Python.getInstance()
                    val sys = py.getModule("sys")
                    val version = sys.get("version")?.toString()?.split(" ")?.get(0)
                    
                    currentSession.feedLine("${esc}[32m[Chaquopy]${esc}[0m Python $version engine activated!")
                    currentSession.feedLine("${esc}[32m[Chaquopy]${esc}[0m Downloading rootfs (this may take a moment)...")
                    
                    val linuxenv = py.getModule("zmux.linuxenv")
                    
                    // Call install using Chaquopy's direct attribute invocation
                    linuxenv.callAttr("install")
                    linuxenv.callAttr("install_guest_wrappers")
                    
                    currentSession.feedLine("${esc}[32m[Chaquopy]${esc}[0m Install completed successfully!")
                    
                    val proot = linuxenv.callAttr("proot_binary")
                    if (proot == null) {
                        currentSession.feedLine("${esc}[31m[Chaquopy Error]${esc}[0m libproot.so not found. Alpine is extracted, but PRoot C++ build is missing.")
                        currentSession.feedLine("${esc}[33m[Chaquopy]${esc}[0m Press ENTER to continue in local shell.")
                    } else {
                        currentSession.feedLine("${esc}[32m[Chaquopy]${esc}[0m PRoot binary found at $proot")
                        currentSession.feedLine("${esc}[33m[Chaquopy]${esc}[0m Ready for PRoot execution in Phase 3.")
                        currentSession.feedLine("${esc}[33m[Chaquopy]${esc}[0m Press ENTER to continue in local shell.")
                    }
                } catch (e: Exception) {
                    val traceback = Python.getInstance().getModule("traceback")
                    val trace = traceback.callAttr("format_exc").toString()
                    currentSession.feedLine("${esc}[31m[Chaquopy Error]${esc}[0m \r\n$trace")
                    currentSession.feedLine("${esc}[33m[Chaquopy]${esc}[0m Press ENTER to continue in local shell.")
                }
            }.start()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        setContentView(R.layout.activity_terminal)

        terminalView = findViewById(R.id.terminal_view)
        statusPill = findViewById(R.id.status_pill)
        tabStrip = findViewById(R.id.tab_strip)
        newSessionButton = findViewById(R.id.new_session_button)

        viewClient = ZmuxViewClient(terminalView)
        terminalView.setTerminalViewClient(viewClient)
        // Set font size to 10sp as requested
        terminalView.setTextSize((10 * resources.displayMetrics.density).toInt())
        terminalView.keepScreenOn = true
        terminalView.isFocusable = true
        terminalView.isFocusableInTouchMode = true

        terminalView.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ ->
            activeSession()?.let { it.onResize?.invoke(it.columns, it.rows) }
        }

        statusPill.setState(StatusPillView.State.CONNECTED, "local shell")

        newSessionButton.setOnClickListener { createNewSession() }

        buildVirtualKeys()
        
        val filter = IntentFilter("com.zmux.terminal.INSTALL_OS")
        @Suppress("UnspecifiedRegisterReceiverFlag")
        try {
            androidx.core.content.ContextCompat.registerReceiver(
                this, installReceiver, filter, androidx.core.content.ContextCompat.RECEIVER_NOT_EXPORTED
            )
        } catch (e: Exception) {
            registerReceiver(installReceiver, filter)
        }
        
        // Auto-start in local shell mode by default so we bypass the login screen
        createNewSession()
    }

    private fun activeSession(): ZmuxTerminalSession? {
        if (activeSessionIndex in sessions.indices) return sessions[activeSessionIndex]
        return null
    }

    private fun createNewSession() {
        val newSession = ZmuxTerminalSession(this)
        sessions.add(newSession)
        switchToSession(sessions.size - 1)
    }

    private fun switchToSession(index: Int) {
        if (index !in sessions.indices) return
        activeSessionIndex = index
        val s = sessions[index]
        terminalView.attachSession(s.session)
        renderLocalTabs()
    }

    private fun renderLocalTabs() {
        tabStrip.removeAllViews()
        for (i in sessions.indices) {
            tabStrip.addView(
                SessionTabView(this).apply {
                    sessionId = "tab${i + 1}"
                    isActiveTab = (i == activeSessionIndex)
                    isBusy = false
                    onTap = { switchToSession(i) }
                    onHoldComplete = { closeSession(i) }
                }
            )
        }
        val room = sessions.size < 8
        newSessionButton.isEnabled = room
        newSessionButton.alpha = if (room) 1f else 0.35f
    }

    private fun closeSession(index: Int) {
        if (index !in sessions.indices) return
        sessions[index].finishIfRunning()
        sessions.removeAt(index)
        
        if (sessions.isEmpty()) {
            createNewSession()
        } else {
            val newIndex = if (activeSessionIndex >= sessions.size) sessions.size - 1 else activeSessionIndex
            switchToSession(newIndex)
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
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                    )
                }
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
        activeSession()?.session?.write(bytes, 0, bytes.size)
    }

    override fun onDestroy() {
        unregisterReceiver(installReceiver)
        for (s in sessions) {
            s.finishIfRunning()
        }
        sessions.clear()
        super.onDestroy()
    }

    // ------------------------------------------------------- TerminalSessionClient
    override fun onTextChanged(changedSession: TerminalSession) {
        if (::terminalView.isInitialized) {
            terminalView.onScreenUpdated()
            ZmuxTheme.applyTo(TerminalSessionHelper.getEmulator(changedSession))
        }
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
    }
}