package com.zmux.terminal.widget

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.os.SystemClock
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import com.zmux.terminal.ZmuxKeys
import com.zmux.terminal.ZmuxTheme

/**
 * A single virtual key, drawn rather than themed.
 *
 * Android's stock `Button` cannot express the three states this bar needs without a pile of
 * selector XML, and each one is a real affordance:
 *
 *  - **latched** (CTRL held for the next keystroke) — ember fill, so the modifier is impossible
 *    to lose track of. This is the state the WebView marked with a CSS class.
 *  - **repeating** (arrows, backspace) — a thin ember underline grows while auto-repeat runs,
 *    so you can see the key firing rather than guessing.
 *  - **danger** (^C, ^D) — rose text; destructive keys should never look like `/` or `~`.
 *
 * Auto-repeat timings match `terminal.html`: 400 ms before the first repeat, 55 ms between.
 */
class KeyCapView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    var label: String = ""
        set(value) {
            field = value
            requestLayout()
            invalidate()
        }

    /** Destructive key (^C / ^D): rose tint. */
    var danger: Boolean = false
        set(value) {
            field = value
            invalidate()
        }

    /** Sticky modifier that is currently armed. */
    var latched: Boolean = false
        set(value) {
            field = value
            invalidate()
        }

    /** Hold-to-repeat enabled (arrows, backspace). */
    var repeatable: Boolean = false

    var onFire: (() -> Unit)? = null

    private val density = resources.displayMetrics.density
    private val radius = 7f * density

    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val strokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1f * density
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 13f * density
        typeface = android.graphics.Typeface.MONOSPACE
        textAlign = Paint.Align.CENTER
    }
    private val bounds = RectF()

    private var pressed = false
    private var repeating = false
    private var repeatStart = 0L

    private val repeater = object : Runnable {
        override fun run() {
            if (!pressed) return
            repeating = true
            onFire?.invoke()
            invalidate()
            postDelayed(this, ZmuxKeys.REPEAT_INTERVAL_MS)
        }
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val w = (textPaint.measureText(label) + 22f * density).coerceAtLeast(34f * density)
        val h = 38f * density
        setMeasuredDimension(
            resolveSize(w.toInt(), widthMeasureSpec),
            resolveSize(h.toInt(), heightMeasureSpec),
        )
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                pressed = true
                repeating = false
                repeatStart = SystemClock.uptimeMillis()
                onFire?.invoke()
                if (repeatable) postDelayed(repeater, ZmuxKeys.REPEAT_INITIAL_DELAY_MS)
                invalidate()
                return true
            }

            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                pressed = false
                repeating = false
                removeCallbacks(repeater)
                if (event.actionMasked == MotionEvent.ACTION_UP) performClick()
                invalidate()
                return true
            }
        }
        return super.onTouchEvent(event)
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    override fun onDetachedFromWindow() {
        removeCallbacks(repeater)
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        val inset = 2.5f * density
        bounds.set(inset, inset, width - inset, height - inset)

        val accent = when {
            latched -> ZmuxTheme.EMBER
            danger -> ZmuxTheme.ROSE
            else -> ZmuxTheme.TEXT
        }

        fillPaint.color = when {
            latched -> ZmuxTheme.alpha(ZmuxTheme.EMBER, 0.22f)
            pressed -> ZmuxTheme.alpha(accent, 0.16f)
            else -> ZmuxTheme.SURFACE_SUNK
        }
        canvas.drawRoundRect(bounds, radius, radius, fillPaint)

        strokePaint.color = when {
            latched -> ZmuxTheme.EMBER
            pressed -> ZmuxTheme.alpha(accent, 0.55f)
            danger -> ZmuxTheme.alpha(ZmuxTheme.ROSE, 0.40f)
            else -> ZmuxTheme.OUTLINE
        }
        canvas.drawRoundRect(bounds, radius, radius, strokePaint)

        // Auto-repeat tell: an ember underline that appears once repeating starts.
        if (repeating) {
            fillPaint.color = ZmuxTheme.EMBER
            val y = bounds.bottom - 3f * density
            canvas.drawRoundRect(
                RectF(bounds.left + 8f * density, y, bounds.right - 8f * density, y + 2f * density),
                1f * density, 1f * density, fillPaint,
            )
        }

        textPaint.color = when {
            latched -> ZmuxTheme.EMBER
            danger -> ZmuxTheme.ROSE
            else -> ZmuxTheme.TEXT
        }
        val baseline = bounds.centerY() - (textPaint.descent() + textPaint.ascent()) / 2f
        canvas.drawText(label, bounds.centerX(), baseline, textPaint)
    }
}
