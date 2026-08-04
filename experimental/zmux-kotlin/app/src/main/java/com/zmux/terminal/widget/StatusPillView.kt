package com.zmux.terminal.widget

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.os.SystemClock
import android.util.AttributeSet
import android.view.View
import com.zmux.terminal.WebSocketPtyBridge
import com.zmux.terminal.ZmuxTheme
import kotlin.math.sin

/**
 * Connection state as a single glanceable pill: a status lamp plus a short label.
 *
 * The lamp is not a static dot. Transitional states (connecting / reconnecting) **breathe** —
 * a slow sine pulse — while settled states (connected / disconnected) hold steady. Motion means
 * "waiting", stillness means "resolved", so the state reads from peripheral vision without
 * anyone parsing the word.
 *
 * Colour follows the ZMUX Ember duotone: teal = the machine is ready, amber = in flight,
 * rose = broken.
 */
class StatusPillView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private val density = resources.displayMetrics.density

    private val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val lampPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val haloPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 11f * density
        typeface = android.graphics.Typeface.MONOSPACE
    }

    private val bounds = RectF()

    private var state: WebSocketPtyBridge.State = WebSocketPtyBridge.State.IDLE
    private var label: String = "idle"
    private var animating = false

    private val ticker = object : Runnable {
        override fun run() {
            if (!animating) return
            invalidate()
            postOnAnimation(this)
        }
    }

    fun setState(state: WebSocketPtyBridge.State, detail: String?) {
        this.state = state
        this.label = buildString {
            append(state.name.lowercase())
            if (!detail.isNullOrBlank()) append("  ").append(detail)
        }

        val shouldAnimate = state == WebSocketPtyBridge.State.CONNECTING ||
            state == WebSocketPtyBridge.State.RECONNECTING
        if (shouldAnimate && !animating) {
            animating = true
            postOnAnimation(ticker)
        } else if (!shouldAnimate) {
            animating = false
            removeCallbacks(ticker)
        }
        requestLayout()
        invalidate()
    }

    private fun accent(): Int = when (state) {
        WebSocketPtyBridge.State.CONNECTED -> ZmuxTheme.TEAL
        WebSocketPtyBridge.State.CONNECTING,
        WebSocketPtyBridge.State.RECONNECTING -> ZmuxTheme.AMBER
        WebSocketPtyBridge.State.UNAUTHORIZED,
        WebSocketPtyBridge.State.FAILED -> ZmuxTheme.ROSE
        else -> ZmuxTheme.TEXT_DIM
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = (textPaint.measureText(label) + 34f * density).toInt()
        val height = (22f * density).toInt()
        setMeasuredDimension(
            resolveSize(width, widthMeasureSpec),
            resolveSize(height, heightMeasureSpec),
        )
    }

    override fun onDetachedFromWindow() {
        animating = false
        removeCallbacks(ticker)
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        val accent = accent()
        bounds.set(0f, 0f, width.toFloat(), height.toFloat())
        val r = height / 2f

        bgPaint.color = ZmuxTheme.alpha(accent, 0.10f)
        canvas.drawRoundRect(bounds, r, r, bgPaint)

        // Breathing lamp: sine in [0,1], ~1.4 s period.
        val pulse = if (animating) {
            (sin(SystemClock.uptimeMillis() / 220.0) * 0.5 + 0.5).toFloat()
        } else 1f

        val cx = 12f * density
        val cy = height / 2f

        haloPaint.color = ZmuxTheme.alpha(accent, 0.18f + 0.22f * pulse)
        canvas.drawCircle(cx, cy, 7f * density, haloPaint)

        lampPaint.color = if (animating) ZmuxTheme.alpha(accent, 0.55f + 0.45f * pulse) else accent
        canvas.drawCircle(cx, cy, 3.4f * density, lampPaint)

        textPaint.color = accent
        val baseline = cy - (textPaint.descent() + textPaint.ascent()) / 2f
        canvas.drawText(label, 22f * density, baseline, textPaint)
    }
}
