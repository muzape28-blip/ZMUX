package com.zmux.terminal

import android.os.Bundle
import android.view.ViewGroup
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
import java.util.concurrent.atomic.AtomicBoolean

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
    private var ctrlKeyCap: KeyCapView? = null

    /** One rootfs operation at a time, even if terminal title sequences repeat. */
    private val installInProgress = AtomicBoolean(false)

    /**
     * Chaquopy runs the installer on a worker thread. TerminalEmulator and
     * TerminalView are UI objects, so every byte returned by Python must cross
     * back to the main thread before it is appended or painted.
     */
    private fun appendToTerminal(session: ZmuxTerminalSession, bytes: ByteArray) {
        runOnUiThread {
            if (!isFinishing && !isDestroyed && sessions.contains(session)) {
                session.feed(bytes)
            }
        }
    }

    private fun appendTerminalLine(session: ZmuxTerminalSession, text: String) {
        appendToTerminal(session, ("\r\n$text\r\n").toByteArray(Charsets.UTF_8))
    }

    /** Render the real Java/Python exception, not traceback.format_exc() outside its context. */
    private fun formatInstallFailure(error: Throwable): String {
        val details = mutableListOf<String>()
        var current: Throwable? = error
        while (current != null && details.size < 4) {
            // Take an immutable reference before the loop advances. Kotlin
            // cannot smart-cast a mutable variable inside ifBlank's lambda.
            val item = current ?: break
            val type = item.javaClass.simpleName.ifBlank { item.javaClass.name }
            val message = item.message?.trim()?.takeIf { it.isNotEmpty() }
            details += if (message == null) type else "$type: $message"
            val next = item.cause
            if (next === item) break
            current = next
        }
        return details.distinct().joinToString("\nCaused by: ")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        if (!Python.isStarted()) {
            // Chaquopy does not define the ANDROID_* vars zmux.paths keys on,
            // and its AssetFinder extraction dir is NOT the app filesDir —
            // resolving APP_DIR from __file__ there is what stranded installed
            // rootfses. Hand the embedded interpreter the authoritative
            // app-private root before it starts (os.environ is snapshot at
                // interpreter init). zmux.paths also detects the Chaquopy HOME pin
            // as a fallback if this setenv is unavailable.
            try {
                android.system.Os.setenv("ZMUX_APP_DIR", filesDir.absolutePath, true)
            } catch (_: Exception) {
                // Hardening only; the HOME-pin branch in zmux.paths covers this.
            }
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

    /** Replace the bootstrap host shell with PRoot -> guest /bin/sh in the same tab. */
    private fun launchLinuxSession(
        bootstrapSession: ZmuxTerminalSession,
        prootPath: String,
        osName: String,
        rootfsPath: String,
        homePath: String,
        cachePath: String,
    ) {
        runOnUiThread {
            if (isFinishing || isDestroyed) return@runOnUiThread
            val index = sessions.indexOf(bootstrapSession)
            if (index < 0) return@runOnUiThread

            // The path comes from the Python installer itself (single source of
            // truth). This is ONLY a final race guard: something deleted the
            // rootfs between install() returning and this UI-thread switch.
            val rootfs = java.io.File(rootfsPath)
            if (!rootfs.isDirectory) {
                appendTerminalLine(
                    bootstrapSession,
                    "\u001b[31m[Chaquopy Error]\u001b[0m Rootfs path disappeared before PRoot launch: " +
                        "${rootfs.absolutePath} (run linux-setup again or report this)"
                )
                return@runOnUiThread
            }

            // Stop the Android mksh process which waited for .setup_done, then
            // attach a fresh kernel PTY whose child is PRoot and guest /bin/sh.
            bootstrapSession.finishIfRunning()
            val linuxSession = ZmuxTerminalSession(
                this,
                prootPath,
                applicationInfo.nativeLibraryDir,
                rootfs.absolutePath,
                homePath,
                cachePath,
                osName,
            )
            sessions[index] = linuxSession
            activeSessionIndex = index
            terminalView.attachSession(linuxSession.session)
            statusPill.setState(StatusPillView.State.CONNECTED, "$osName via PRoot")
            renderLocalTabs()
            terminalView.requestFocus()
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

    override fun onTitleChanged(changedSession: TerminalSession) {
        val title = changedSession.title ?: return
        val osName = when (title) {
            "INSTALL_ALPINE" -> "alpine"
            "INSTALL_DEBIAN" -> "debian"
            else -> return
        }
        val zmuxSession = sessions.find { it.session == changedSession } ?: return

        if (!installInProgress.compareAndSet(false, true)) {
            appendTerminalLine(zmuxSession, "\u001b[33m[Chaquopy]\u001b[0m Linux setup is already running. Please wait.")
            return
        }

        val esc = 27.toChar()
        appendTerminalLine(zmuxSession, "${esc}[34m[Chaquopy]${esc}[0m Starting verified $osName rootfs setup…")

        Thread {
            try {
                val py = Python.getInstance()
                val version = py.getModule("sys").get("version")
                    ?.toString()?.substringBefore(" ") ?: "unknown"
                appendTerminalLine(zmuxSession, "${esc}[32m[Chaquopy]${esc}[0m Python $version engine activated!")
                appendTerminalLine(zmuxSession, "${esc}[32m[Chaquopy]${esc}[0m Downloading and verifying rootfs…")

                val progressCallback = object : TerminalSessionHelper.ProgressCallback {
                    override fun invoke(message: String) {
                        // Chaquopy 15 exposes this as a Java object. linuxenv._emit_progress
                        // calls its explicit invoke method rather than treating it as a
                        // Python callable, and this helper moves the UI work to main.
                        appendToTerminal(zmuxSession, message.toByteArray(Charsets.UTF_8))
                    }
                }

                val linuxenv = py.getModule("zmux.linuxenv")
                // This is the authoritative location for libproot.so. The
                // Python engine is Chaquopy (not Kivy/python-for-android), so
                // its legacy activity discovery cannot infer this directory.
                val nativeLibDir = applicationInfo.nativeLibraryDir
                linuxenv.callAttr("set_native_library_dir", nativeLibDir)
                linuxenv.callAttr("install", progressCallback, osName)
                linuxenv.callAttr("install_guest_wrappers")

                // PyObject.get("key") is Python attribute access, not dict
                // item access. Ask the module directly so the success banner
                // reports the real installed guest/version on every Chaquopy release.
                val installedOs = linuxenv.callAttr("installed_os").toString().ifBlank { osName }
                val versionText = linuxenv.callAttr("installed_version").toString()
                    .takeIf { it.isNotBlank() }
                val suffix = if (versionText == null) "" else " ($versionText)"
                appendTerminalLine(
                    zmuxSession,
                    "${esc}[32m[Chaquopy]${esc}[0m $installedOs$suffix rootfs installed and verified successfully!"
                )

                val proot = linuxenv.callAttr("proot_binary")?.toString()
                if (proot.isNullOrBlank() || proot == "None") {
                    val expected = java.io.File(nativeLibDir, "libproot.so")
                    appendTerminalLine(
                        zmuxSession,
                        "${esc}[31m[Chaquopy Error]${esc}[0m PRoot is unavailable at $expected " +
                            "(exists=${expected.isFile}, executable=${expected.canExecute()}). " +
                            "This APK is incomplete; the build now rejects APKs missing PRoot."
                    )
                } else {
                    // Single source of truth: ask the Python installer where the
                    // rootfs/home/cache actually are. Re-deriving them here as
                    // filesDir/<name> previously disagreed with zmux.paths and
                    // stranded a fully installed rootfs.
                    val pathsModule = py.getModule("zmux.paths")
                    val rootfsPath = linuxenv.callAttr("rootfs_dir").toString()
                    val homePath = pathsModule.get("HOME_DIR")?.toString()
                        ?.takeIf { it.isNotBlank() }
                        ?: java.io.File(filesDir, "home").absolutePath
                    val cachePath = pathsModule.get("CACHE_DIR")?.toString()
                        ?.takeIf { it.isNotBlank() }
                        ?: java.io.File(filesDir, "cache").absolutePath
                    appendTerminalLine(zmuxSession, "${esc}[32m[Chaquopy]${esc}[0m PRoot verified at $proot — launching $installedOs shell…")
                    launchLinuxSession(zmuxSession, proot, installedOs, rootfsPath, homePath, cachePath)
                }
            } catch (error: Throwable) {
                // Do not call Python traceback.format_exc() here: this is a Kotlin catch
                // block, so Python has no active exception and returns "NoneType: None".
                appendTerminalLine(
                    zmuxSession,
                    "${esc}[31m[Chaquopy Error]${esc}[0m\r\n${formatInstallFailure(error)}"
                )
            } finally {
                try {
                    // The host shell waits for this marker. Always release it after a
                    // success or a genuine diagnostic so a corrected retry is possible.
                    java.io.File(this@ZmuxTerminalActivity.filesDir, ".setup_done").createNewFile()
                } catch (_: Exception) {
                    // A closing activity may have removed the app directory.
                }
                installInProgress.set(false)
            }
        }.start()
    }
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