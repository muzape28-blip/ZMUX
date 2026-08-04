package com.zmux.terminal.widget

import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.os.SystemClock
import android.util.AttributeSet
import android.view.View
import com.zmux.terminal.ZmuxTheme
import kotlin.math.sin

/**
 * The boot mark: a monogram **Z** drawn as a terminal prompt, with an ember cursor blinking
 * beside it.
 *
 * Vector, not a PNG — it stays crisp on any density and adds nothing to the APK, which matters
 * for the Android Go target. The Z is stroked with an ember→teal gradient, tying the two halves
 * of the duotone together: the user and the machine meeting at the prompt.
 */
class BootBrandView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private val density = resources.displayMetrics.density

    private val strokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 4f * density
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }
    private val cursorPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val wordPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 15f * density
        typeface = android.graphics.Typeface.create(android.graphics.Typeface.MONOSPACE, android.graphics.Typeface.BOLD)
        textAlign = Paint.Align.CENTER
        letterSpacing = 0.32f
    }
    private val taglinePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 10.5f * density
        typeface = android.graphics.Typeface.MONOSPACE
        textAlign = Paint.Align.CENTER
        color = ZmuxTheme.TEXT_DIM
    }

    private val zPath = Path()

    var tagline: String = "native terminal · python engine"
        set(value) {
            field = value
            invalidate()
        }

    private val ticker = object : Runnable {
        override fun run() {
            invalidate()
            postOnAnimation(this)
        }
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        postOnAnimation(ticker)
    }

    override fun onDetachedFromWindow() {
        removeCallbacks(ticker)
        super.onDetachedFromWindow()
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        setMeasuredDimension(
            resolveSize((180 * density).toInt(), widthMeasureSpec),
            resolveSize((150 * density).toInt(), heightMeasureSpec),
        )
    }

    override fun onDraw(canvas: Canvas) {
        val cx = width / 2f
        val glyphSize = 46f * density
        val top = height / 2f - glyphSize * 0.95f
        val left = cx - glyphSize * 0.72f
        val right = cx + glyphSize * 0.22f
        val bottom = top + glyphSize

        // Z drawn as three strokes: top bar, diagonal, bottom bar.
        zPath.reset()
        zPath.moveTo(left, top)
        zPath.lineTo(right, top)
        zPath.lineTo(left, bottom)
        zPath.lineTo(right, bottom)

        strokePaint.shader = LinearGradient(
            left, top, right, bottom,
            ZmuxTheme.EMBER, ZmuxTheme.TEAL,
            Shader.TileMode.CLAMP,
        )
        canvas.drawPath(zPath, strokePaint)

        // Blinking block cursor to the right of the Z — the prompt is alive.
        val phase = sin(SystemClock.uptimeMillis() / 300.0)
        cursorPaint.color = ZmuxTheme.alpha(ZmuxTheme.EMBER, if (phase > 0) 0.95f else 0.18f)
        val cw = 11f * density
        val ch = 22f * density
        canvas.drawRect(
            right + 12f * density,
            bottom - ch,
            right + 12f * density + cw,
            bottom,
            cursorPaint,
        )

        wordPaint.color = ZmuxTheme.TEXT
        canvas.drawText("Z M U X", cx, bottom + 34f * density, wordPaint)
        canvas.drawText(tagline, cx, bottom + 54f * density, taglinePaint)
    }
}
