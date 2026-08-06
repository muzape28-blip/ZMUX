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
import com.chaquo.python.PyObject
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

    /** Background thread for non-blocking Linux detection on startup. */
    private var detectThread: Thread? = null

    /** Chaquopy initialization state. */
    private var chaquopyReady = false

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

        // ---- APP_DIR alignment contract: this line must stay first ----------
        // Chaquopy is not python-for-android: it exports none of the ANDROID_*
        // variables and extracts the app's Python sources into
        // <filesDir>/chaquopy/AssetFinder/app. zmux.paths.resolve_app_dir()
        // then had no host signal at all and fell back to __file__, adopting
        // that extraction directory as APP_DIR — so `linux-setup` installed a
        // fully verified rootfs into <AssetFinder>/linux/rootfs while this
        // activity looked under filesDir ("Rootfs path disappeared before PRoot
        // launch"). The same mismatch hit every other APP_DIR child: bin
        // wrappers, .zmux_auth_token (read by ZmuxBackendLocator), cache, logs.
        //
        // Exporting ANDROID_PRIVATE makes filesDir the one source of truth for
        // both sides. APP_DIR is resolved at *import* time, so this must happen
        // before Python.start() and before the first `zmux` module is imported.
        runCatching { android.system.Os.setenv("ANDROID_PRIVATE", filesDir.absolutePath, true) }

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

        // FIX ANR: Start with bootstrap shell immediately (non-blocking).
        // Chaquopy initialization and Linux detection run in background.
        // This prevents "zmux tidak ada tanggapan" popup.
        createNewSession()

        // Background initialization: Chaquopy + Linux detection
        // Runs AFTER UI is ready to prevent ANR during startup/input
        detectThread = Thread {
            try {
                // Start Chaquopy (can take 1-3 seconds on low-end devices)
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this@ZmuxTerminalActivity))
                }
                chaquopyReady = true

                // Small delay to let the UI settle first
                Thread.sleep(500)

                val installed = detectInstalledLinux()
                if (installed != null && !isFinishing && !isDestroyed) {
                    runOnUiThread {
                        // Replace bootstrap session with real Linux session
                        if (sessions.isNotEmpty()) {
                            sessions[0].finishIfRunning()
                            val linuxSession = ZmuxTerminalSession(
                                this@ZmuxTerminalActivity,
                                installed.prootPath,
                                applicationInfo.nativeLibraryDir,
                                installed.rootfsDir,
                                installed.homeDir,
                                installed.osName,
                            )
                            sessions[0] = linuxSession
                            activeSessionIndex = 0
                            terminalView.attachSession(linuxSession.session)
                            statusPill.setState(StatusPillView.State.CONNECTED, "${installed.osName} via PRoot")
                        }
                    }
                }
            } catch (e: Exception) {
                // Chaquopy/Detection failed silently - user stays in bootstrap shell
            }
        }.apply { start() }
    }

    /** What [detectInstalledLinux] found: everything needed to relaunch PRoot. */
    private data class InstalledLinux(
        val osName: String,
        val prootPath: String,
        val rootfsDir: String,
        val homeDir: String,
    )

    /** Imported once by [linuxenv]; Chaquopy caches the module itself too. */
    private var linuxenvModule: PyObject? = null

    /** One-shot note from [linuxenv]'s legacy-install migration, or null. */
    private var legacyInstallNote: String? = null

    /**
     * The `zmux.linuxenv` module, ready to answer path questions.
     *
     * Python (`zmux.paths.APP_DIR`) owns the runtime layout, so this activity
     * *asks* for every directory instead of rebuilding it from [filesDir]. A
     * second, hand-written copy of that layout is exactly what produced the
     * "Rootfs path disappeared" mismatch, and a hard-coded fallback here would
     * reintroduce it — so there is none: when Python is unavailable the caller
     * degrades to the bootstrap shell and says why.
     *
     * Synchronised because the installer thread and the UI thread both use it;
     * the migration inside must run exactly once.
     */
    @Synchronized
    private fun linuxenv(): PyObject? {
        linuxenvModule?.let { return it }
        return runCatching {
            val module = Python.getInstance().getModule("zmux.linuxenv")
            // nativeLibraryDir is the only directory Android allows execve()
            // from, and Chaquopy cannot discover it: hand it over before any
            // path/proot query.
            module.callAttr("set_native_library_dir", applicationInfo.nativeLibraryDir)
            // Adopt a rootfs left behind by a pre-alignment build (it lives in
            // Chaquopy's asset tree) instead of making the user download the
            // guest a second time. Renames only — never a blocking copy.
            legacyInstallNote = module.callAttr("migrate_legacy_install")
                ?.toString()?.trim()?.takeIf { it.isNotEmpty() && it != "None" }
            linuxenvModule = module
            module
        }.getOrNull()
    }

    /** Read a `zmux.linuxenv` path accessor (`rootfs_dir`, `home_dir`, …). */
    private fun pythonPath(accessor: String): java.io.File? {
        val module = linuxenv() ?: return null
        val value = runCatching { module.callAttr(accessor)?.toString() }.getOrNull()
        if (value.isNullOrBlank() || value == "None") return null
        return java.io.File(value)
    }

    /** Guest rootfs location, exactly as `zmux.linuxenv.rootfs_dir()` reports it. */
    private fun installedRootfsDir(): java.io.File? = pythonPath("rootfs_dir")

    /** Host directory bound to the guest `/root`, per `zmux.linuxenv.home_dir()`. */
    private fun guestHomeDir(): java.io.File? = pythonPath("home_dir")

    /**
     * Detect an already-installed, already-verified guest rootfs.
     *
     * The location comes from [installedRootfsDir]/[guestHomeDir] (i.e. from
     * `zmux.linuxenv`), never from a path built here — that is the whole point
     * of the alignment contract. The checks themselves mirror
     * linuxenv.installed_os(): guest /bin/sh must resolve (busybox absolute
     * links included) and the OS marker decides the flavour. PRoot must come
     * from nativeLibraryDir — the only directory Android allows execve() from
     * — so its own executable check also gates the launch.
     */
    private fun detectInstalledLinux(): InstalledLinux? {
        val rootfs = installedRootfsDir() ?: return null
        val home = guestHomeDir() ?: return null
        if (!rootfs.isDirectory) return null
        if (!TerminalSessionHelper.guestRegularFile(rootfs, "bin/sh")) return null

        val osName = run {
            val marker = java.io.File(rootfs, "etc/.zmux-rootfs")
            val fromMarker = runCatching { marker.takeIf { it.isFile }?.readText()?.trim()?.lowercase() }
                .getOrNull()
            when {
                fromMarker in setOf("alpine", "debian") -> fromMarker!!
                java.io.File(rootfs, "etc/alpine-release").isFile -> "alpine"
                java.io.File(rootfs, "etc/debian_version").isFile -> "debian"
                else -> return null
            }
        }

        val proot = java.io.File(applicationInfo.nativeLibraryDir, "libproot.so")
        if (!proot.isFile || !proot.canExecute()) return null

        return InstalledLinux(
            osName = osName,
            prootPath = proot.absolutePath,
            rootfsDir = rootfs.absolutePath,
            homeDir = home.absolutePath,
        )
    }

    private fun activeSession(): ZmuxTerminalSession? {
        if (activeSessionIndex in sessions.indices) return sessions[activeSessionIndex]
        return null
    }

    private fun createNewSession() {
        val installed = detectInstalledLinux()
        val newSession = if (installed != null) {
            // A verified rootfs + executable PRoot: reopen the real guest
            // shell directly. Guest binaries run through PRoot's ptrace mmap,
            // so the host W^X execve ban never applies inside the guest.
            ZmuxTerminalSession(
                this,
                installed.prootPath,
                applicationInfo.nativeLibraryDir,
                installed.rootfsDir,
                installed.homeDir,
                installed.osName,
            )
        } else {
            ZmuxTerminalSession(this)
        }
        sessions.add(newSession)
        if (installed != null) {
            statusPill.setState(StatusPillView.State.CONNECTED, "${installed.osName} via PRoot")
        }
        switchToSession(sessions.size - 1)
        if (installed == null) showLegacyInstallNote(newSession)
    }

    /**
     * Tell the user, once, what happened to an install made by an older build.
     *
     * Only reached when no guest could be opened — either the stranded rootfs
     * could not be renamed into place, or it was already superseded. The
     * bootstrap rc clears the screen as its first command, so the note is
     * posted after the banner has been drawn; printing it immediately would
     * wipe it before it could be read.
     */
    private fun showLegacyInstallNote(session: ZmuxTerminalSession) {
        val note = legacyInstallNote ?: return
        legacyInstallNote = null
        terminalView.postDelayed({
            appendTerminalLine(session, "\u001b[33m[ZMUX]\u001b[0m ${note.replace("\n", "\r\n")}")
        }, 900L)
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
    ) {
        runOnUiThread {
            if (isFinishing || isDestroyed) return@runOnUiThread
            val index = sessions.indexOf(bootstrapSession)
            if (index < 0) return@runOnUiThread

            // Never guess the guest location: ask the module that installed it.
            // Reporting the path Python actually returned is what makes a
            // layout regression diagnosable instead of mysterious.
            val rootfs = installedRootfsDir()
            val home = guestHomeDir()
            if (rootfs == null || home == null || !rootfs.isDirectory) {
                appendTerminalLine(
                    bootstrapSession,
                    "\u001b[31m[ZMUX Error]\u001b[0m Guest rootfs is not a directory at the path " +
                        "zmux.linuxenv reports: ${rootfs?.absolutePath ?: "<zmux.linuxenv unavailable>"}. " +
                        "Run linux-setup again — a reinstall is safe and keeps your home directory."
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
                home.absolutePath,
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
        // Cancel background detection thread
        detectThread?.interrupt()
        detectThread = null
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

                // Shared accessor: imports the module once, hands it the
                // authoritative libproot.so location (Chaquopy is not
                // Kivy/python-for-android, so its legacy activity discovery
                // cannot infer nativeLibraryDir) and adopts any pre-alignment
                // install before the installer decides what to download.
                val nativeLibDir = applicationInfo.nativeLibraryDir
                val pyLinuxenv = linuxenv()
                    ?: throw IllegalStateException(
                        "zmux.linuxenv could not be imported; the Python runtime is unavailable."
                    )
                pyLinuxenv.callAttr("install", progressCallback, osName)
                pyLinuxenv.callAttr("install_guest_wrappers")

                // PyObject.get("key") is Python attribute access, not dict
                // item access. Ask the module directly so the success banner
                // reports the real installed guest/version on every Chaquopy release.
                val installedOs = pyLinuxenv.callAttr("installed_os").toString().ifBlank { osName }
                val versionText = pyLinuxenv.callAttr("installed_version").toString()
                    .takeIf { it.isNotBlank() }
                val suffix = if (versionText == null) "" else " ($versionText)"
                appendTerminalLine(
                    zmuxSession,
                    "${esc}[32m[Chaquopy]${esc}[0m $installedOs$suffix rootfs installed and verified successfully!"
                )

                val proot = pyLinuxenv.callAttr("proot_binary")?.toString()
                if (proot.isNullOrBlank() || proot == "None") {
                    val expected = java.io.File(nativeLibDir, "libproot.so")
                    appendTerminalLine(
                        zmuxSession,
                        "${esc}[31m[Chaquopy Error]${esc}[0m PRoot is unavailable at $expected " +
                            "(exists=${expected.isFile}, executable=${expected.canExecute()}). " +
                            "This APK is incomplete; the build now rejects APKs missing PRoot."
                    )
                } else {
                    appendTerminalLine(zmuxSession, "${esc}[32m[Chaquopy]${esc}[0m PRoot verified at $proot — launching $installedOs shell…")
                    launchLinuxSession(zmuxSession, proot, installedOs)
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