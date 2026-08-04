package com.zmux.terminal

import android.view.KeyEvent
import android.view.MotionEvent
import com.termux.terminal.TerminalSession
import com.termux.view.TerminalView
import com.termux.view.TerminalViewClient

/**
 * Minimal [TerminalViewClient] for the PoC: no extra keyboard shortcuts, no bell/vibration
 * handling beyond defaults. Ctrl/Alt state is driven by the virtual key bar.
 */
class ZmuxViewClient(
    private val view: TerminalView,
    private val onLog: (String) -> Unit = {},
) : TerminalViewClient {

    var ctrlDown = false
    var altDown = false
    /** One-shot modifiers reset after the next key. */
    var stickyModifiers = true

    override fun onScale(scale: Float): Float = 1.0f

    override fun onSingleTapUp(e: MotionEvent?) {
        view.requestFocus()
        val imm = view.context.getSystemService(Context.INPUT_METHOD_SERVICE) as android.view.inputmethod.InputMethodManager
        imm.showSoftInput(view, android.view.inputmethod.InputMethodManager.SHOW_IMPLICIT)
    }

    override fun shouldBackButtonBeMappedToEscape(): Boolean = false
    override fun shouldEnforceCharBasedInput(): Boolean = true
    override fun shouldUseCtrlSpaceWorkaround(): Boolean = false
    override fun isTerminalViewSelected(): Boolean = true

    override fun copyModeChanged(copyMode: Boolean) = Unit

    override fun onKeyDown(keyCode: Int, e: KeyEvent?, session: TerminalSession?): Boolean = false
    override fun onKeyUp(keyCode: Int, e: KeyEvent?): Boolean = false

    override fun onLongPress(event: MotionEvent?): Boolean = false

    override fun readControlKey(): Boolean = consume(ctrlDown) { ctrlDown = false }
    override fun readAltKey(): Boolean = consume(altDown) { altDown = false }
    override fun readShiftKey(): Boolean = false
    override fun readFnKey(): Boolean = false

    override fun onCodePoint(codePoint: Int, ctrlDown: Boolean, session: TerminalSession?): Boolean = false

    override fun onEmulatorSet() = Unit

    override fun logError(tag: String?, message: String?) = onLog("E/$tag: $message")
    override fun logWarn(tag: String?, message: String?) = onLog("W/$tag: $message")
    override fun logInfo(tag: String?, message: String?) = Unit
    override fun logDebug(tag: String?, message: String?) = Unit
    override fun logVerbose(tag: String?, message: String?) = Unit
    override fun logStackTraceWithMessage(tag: String?, message: String?, e: Exception?) =
        onLog("E/$tag: $message ${e?.message}")

    override fun logStackTrace(tag: String?, e: Exception?) = onLog("E/$tag: ${e?.message}")

    private inline fun consume(value: Boolean, reset: () -> Unit): Boolean {
        if (value && stickyModifiers) reset()
        return value
    }
}
