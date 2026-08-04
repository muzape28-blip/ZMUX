package com.zmux.terminal

/**
 * Data-driven virtual keys, ported from the `KEY_ROWS` table in `app/templates/terminal.html`
 * so the native UI has key-for-key parity with the WebView it replaces.
 *
 * Behaviours kept from the reference implementations (see `docs/REFERENCE_MINING.md` T3):
 *  - **sticky CTRL** — tap CTRL then a letter (`ctrlLatched` in terminal.html)
 *  - **hold-to-repeat** for arrows/backspace, 400 ms initial delay then 55 ms interval
 */
object ZmuxKeys {

    /** terminal.html: REPEAT_INITIAL_DELAY_MS */
    const val REPEAT_INITIAL_DELAY_MS = 400L

    /** terminal.html: REPEAT_INTERVAL_MS */
    const val REPEAT_INTERVAL_MS = 55L

    data class Key(
        val label: String,
        val send: String? = null,
        val modifier: String? = null,
        val action: String? = null,
        val repeat: Boolean = false,
        val danger: Boolean = false,
    )

    /** Row layout copied 1:1 from terminal.html's KEY_ROWS. */
    val ROWS: List<List<Key>> = listOf(
        listOf(
            Key("ESC", send = "\u001b"),
            Key("CTRL", modifier = "ctrl"),
            Key("Tab", send = "\t"),
            Key("↑", send = "\u001b[A", repeat = true),
            Key("↓", send = "\u001b[B", repeat = true),
            Key("←", send = "\u001b[D", repeat = true),
            Key("→", send = "\u001b[C", repeat = true),
            Key("Home", send = "\u001b[H"),
            Key("End", send = "\u001b[F"),
            Key("^C", send = "\u0003", danger = true),
            Key("^D", send = "\u0004", danger = true),
            Key("⌫", send = "\u007f", repeat = true),
            Key("/", send = "/"),
            Key("-", send = "-"),
            Key("~", send = "~"),
            Key("|", send = "|"),
            Key(">", send = ">"),
            Key("&", send = "&"),
            Key("*", send = "*"),
            Key("\"", send = "\""),
            Key("'", send = "'"),
        )
    )

    /**
     * Translate a printable character to its control code: `c` -> 0x03.
     * Mirrors `toControlCode()` in terminal.html.
     */
    fun toControlCode(text: String): String? {
        if (text.length != 1) return null
        val code = text.uppercase()[0].code
        if (code in 64..127) return (code and 0x1f).toChar().toString()
        return null
    }
}
