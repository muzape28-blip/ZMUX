package com.zmux.terminal

import android.content.Context
import android.util.AttributeSet
import com.termux.terminal.TerminalSession
import com.termux.view.TerminalView

/**
 * Thin wrapper around Termux' Apache-2.0 [TerminalView] so ZMUX-specific defaults
 * (font size, Android Go friendly settings) live in one place.
 */
class ZmuxTerminalView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : TerminalView(context, attrs) {

    fun applyZmuxDefaults(textSizeSp: Int = DEFAULT_TEXT_SIZE_SP) {
        setTextSize((textSizeSp * resources.displayMetrics.density).toInt())
        keepScreenOn = true
        isFocusable = true
        isFocusableInTouchMode = true
    }

    fun attach(session: TerminalSession) {
        attachSession(session)
        requestFocus()
    }

    companion object {
        const val DEFAULT_TEXT_SIZE_SP = 12
    }
}
