package com.zmux.terminal

import android.graphics.Color
import com.termux.terminal.TerminalEmulator

/**
 * **ZMUX Ember** — the native UI's own visual identity.
 *
 * This is deliberately *not* a port of the WebView's GitHub-dark palette
 * (`#0d1117` / `#58a6ff` / `#3fb950`). The native client is a different product surface and
 * gets its own look, built around three ideas:
 *
 * 1. **True black (`#000000`) background.** Not a style tic — on the OLED/AMOLED panels common
 *    even in the budget tier, black pixels are switched off entirely. For a terminal that sits
 *    open for long sessions on an Android Go phone, that is real battery. The WebView theme
 *    could not do this credibly because a browser compositor always paints a surface.
 *
 * 2. **Duotone: ember + teal.** Warm `#FF8A3D` marks *you* — the cursor, the active session,
 *    the latched modifier. Cool `#5EE9D5` marks *the machine* — connected state, success.
 *    One glance tells you whether the terminal is waiting on you or on the backend.
 *
 * 3. **Warm off-white text (`#E8E3DD`) instead of cold grey.** Paired with a true-black field
 *    this reads softer during long sessions than `#c9d1d9` on `#0d1117`.
 */
object ZmuxTheme {

    // ---- core surfaces ----------------------------------------------------
    /** AMOLED true black. The terminal field. */
    const val BG = 0xFF000000.toInt()

    /** Raised chrome (bars, key rows). Slight violet lift so it separates from pure black. */
    const val SURFACE = 0xFF12101A.toInt()

    /** Pressed / inset state. */
    const val SURFACE_SUNK = 0xFF0A0910.toInt()

    /** Hairline dividers. */
    const val OUTLINE = 0xFF241F2E.toInt()

    // ---- duotone accents --------------------------------------------------
    /** Ember: the user. Cursor, active tab, latched CTRL. */
    const val EMBER = 0xFFFF8A3D.toInt()
    const val EMBER_DIM = 0xFF7A4220.toInt()

    /** Teal: the machine. Connected, success. */
    const val TEAL = 0xFF5EE9D5.toInt()
    const val TEAL_DIM = 0xFF2A6B62.toInt()

    /** Rose: destructive (^C, ^D, hold-to-close). */
    const val ROSE = 0xFFFF5D73.toInt()

    /** Amber: transitional states (connecting, busy). */
    const val AMBER = 0xFFFFC857.toInt()

    /** Violet: structural accents. */
    const val VIOLET = 0xFFC792EA.toInt()

    // ---- type -------------------------------------------------------------
    const val TEXT = 0xFFE8E3DD.toInt()
    const val TEXT_DIM = 0xFF6B6478.toInt()

    /**
     * The 16 ANSI colours, tuned to sit on true black without vibrating.
     * Index order is the standard 0-7 normal, 8-15 bright.
     */
    val ANSI_16 = intArrayOf(
        0xFF1A1720.toInt(), // 0  black   — lifted so `ls` dirs are not invisible
        0xFFFF5D73.toInt(), // 1  red
        0xFF4FE3C1.toInt(), // 2  green
        0xFFFFC857.toInt(), // 3  yellow
        0xFF7AA2F7.toInt(), // 4  blue
        0xFFC792EA.toInt(), // 5  magenta
        0xFF5EE9D5.toInt(), // 6  cyan
        0xFFE8E3DD.toInt(), // 7  white
        0xFF6B6478.toInt(), // 8  bright black
        0xFFFF8095.toInt(), // 9  bright red
        0xFF7BF5DC.toInt(), // 10 bright green
        0xFFFFD98A.toInt(), // 11 bright yellow
        0xFF9EC0FF.toInt(), // 12 bright blue
        0xFFDDB4FF.toInt(), // 13 bright magenta
        0xFF8FF5E8.toInt(), // 14 bright cyan
        0xFFFFFFFF.toInt(), // 15 bright white
    )

    // Termux TextStyle indices past the 256-colour cube.
    private const val IDX_FOREGROUND = 256
    private const val IDX_BACKGROUND = 257
    private const val IDX_CURSOR = 258

    /**
     * Paint the palette onto a live emulator.
     *
     * Termux exposes `TerminalEmulator.mColors.mCurrentColors` as a plain `int[]` of ARGB
     * values (indices 0-255 = the colour cube, then fg/bg/cursor). Writing into it is how
     * Termux itself applies `colors.properties`, so this uses the library the intended way
     * rather than reaching around it.
     *
     * Guarded because the field has moved between Termux releases and this PoC has not been
     * compiled against a resolved artifact yet — a theme failure must never take the terminal
     * down with it.
     */
    fun applyTo(emulator: TerminalEmulator?): Boolean {
        if (emulator == null) return false
        return runCatching {
            val mColorsField = emulator.javaClass.getDeclaredField("mColors").apply { isAccessible = true }
            val colorsObj = mColorsField.get(emulator) ?: return false
            val mCurrentColorsField = colorsObj.javaClass.getDeclaredField("mCurrentColors").apply { isAccessible = true }
            val slots = mCurrentColorsField.get(colorsObj) as IntArray
            for (i in ANSI_16.indices) {
                if (i < slots.size) slots[i] = ANSI_16[i]
            }
            if (slots.size > IDX_CURSOR) {
                slots[IDX_FOREGROUND] = TEXT
                slots[IDX_BACKGROUND] = BG
                slots[IDX_CURSOR] = EMBER
            }
            true
        }.getOrDefault(false)
    }

    /** Blend [color] toward transparent — used for pressed/disabled chrome. */
    fun alpha(color: Int, fraction: Float): Int =
        Color.argb((255 * fraction).toInt().coerceIn(0, 255), Color.red(color), Color.green(color), Color.blue(color))
}
