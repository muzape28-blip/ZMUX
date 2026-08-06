package com.zmux.terminal

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.WindowManager
import androidx.appcompat.app.AppCompatActivity

/**
 * Cold-start intro: a full-screen black canvas with a "cmatrix" rain, then it
 * hands off to the real terminal.
 *
 * Purely cosmetic — it never touches the Python / PRoot / Chaquopy stack, so it
 * cannot affect the W^X ("Permission denied") or APP_DIR-alignment gates, nor
 * the terminal's auto-reopen behaviour on activity recreation.
 */
class SplashActivity : AppCompatActivity() {
    private lateinit var rain: MatrixRainView
    private var launched = false

    // How long the rain shows before the terminal opens (ms). Tap to skip.
    private val splashMs = 2400L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(
            WindowManager.LayoutParams.FLAG_FULLSCREEN,
            WindowManager.LayoutParams.FLAG_FULLSCREEN
        )
        rain = MatrixRainView(this)
        rain.setBackgroundColor(Color.BLACK)
        setContentView(rain)
        rain.start()
        rain.postDelayed({ launchTerminal() }, splashMs)
        rain.setOnClickListener { launchTerminal() }
    }

    private fun launchTerminal() {
        if (launched) return
        launched = true
        rain.stop()
        startActivity(Intent(this, ZmuxTerminalActivity::class.java))
        finish()
    }

    override fun onDestroy() {
        rain.stop()
        super.onDestroy()
    }
}
