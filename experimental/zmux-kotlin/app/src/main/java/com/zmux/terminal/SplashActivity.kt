package com.zmux.terminal

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageView
import androidx.appcompat.app.AppCompatActivity

/**
 * Cold-start intro: a static centered app logo on a black canvas, then it
 * hands off to the real terminal.
 *
 * The previous animated matrix rain was removed to keep startup as light as
 * possible on low-end Android Go devices. This screen never touches the
 * Python / PRoot / Chaquopy stack.
 */
class SplashActivity : AppCompatActivity() {
    private var launched = false

    // How long the logo shows before the terminal opens (ms). Tap to skip.
    private val splashMs = 2400L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(
            WindowManager.LayoutParams.FLAG_FULLSCREEN,
            WindowManager.LayoutParams.FLAG_FULLSCREEN
        )

        val density = resources.displayMetrics.density
        val logoSize = (144 * density).toInt()

        val logo = ImageView(this).apply {
            setImageResource(R.mipmap.ic_launcher)
            scaleType = ImageView.ScaleType.FIT_CENTER
            adjustViewBounds = true
            contentDescription = getString(R.string.app_name)
            layoutParams = FrameLayout.LayoutParams(logoSize, logoSize).apply {
                gravity = Gravity.CENTER
            }
        }

        val root = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            addView(logo)
            setOnClickListener { launchTerminal() }
        }

        setContentView(root)
        root.postDelayed({ launchTerminal() }, splashMs)
    }

    private fun launchTerminal() {
        if (launched) return
        launched = true
        startActivity(Intent(this, ZmuxTerminalActivity::class.java))
        finish()
    }
}
