package com.zmux.terminal.widget

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.os.SystemClock
import android.view.MotionEvent
import android.view.View
import com.zmux.terminal.ZmuxTheme

/**
 * A session tab drawn by hand, so hold-to-close can be a real gesture with real feedback
 * instead of a long-press that fires invisibly.
 *
 * Behaviour (an idea worth keeping from the WebView UI, rebuilt natively):
 *  - **tap** switches to the session
 *  - **hold** fills a rose progress arc around the tab; at 100% it closes
 *  - **release early, or slide off** cancels
 *
 * The visual language is ZMUX Ember: the active tab is ember-outlined with a warm glow,
 * inactive tabs are quiet, and a busy session shows a slow teal pulse.
 */
class SessionTabView @JvmOverloads constructor(
    context: Context,
    attrs: android.util.AttributeSet? = null,
) : View(context, attrs) {

    var sessionId: String = ""
        set(value) {
            field = value
            requestLayout()
            invalidate()
        }

    var isActiveTab: Boolean = false
        set(value) {
            field = value
            invalidate()
        }

    var isBusy: Boolean = false
        set(value) {
            field = value
            invalidate()
        }

    var onTap: (() -> Unit)? = null
    var onHoldComplete: (() -> Unit)? = null

    /** How long the finger must stay down to close the session. */
    var holdDurationMs: Long = 900L

    private val density = resources.displayMetrics.density
    private val radius = 8f * density

    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val strokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1.2f * density
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 12f * density
        typeface = android.graphics.Typeface.MONOSPACE
        textAlign = Paint.Align.CENTER
    }
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }

    private val bounds = RectF()

    private var holdStart = 0L
    private var holding = false
    private var holdFraction = 0f

    private val holdTicker = object : Runnable {
        override fun run() {
            if (!holding) return
            val elapsed = SystemClock.uptimeMillis() - holdStart
            holdFraction = (elapsed.toFloat() / holdDurationMs).coerceIn(0f, 1f)
            invalidate()
            if (holdFraction >= 1f) {
                holding = false
                performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS)
                onHoldComplete?.invoke()
                holdFraction = 0f
                invalidate()
            } else {
                postOnAnimation(this)
            }
        }
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val label = displayLabel()
        val textWidth = textPaint.measureText(label)
        val width = (textWidth + 34f * density).toInt()
        val height = (34f * density).toInt()
        setMeasuredDimension(
            resolveSize(width, widthMeasureSpec),
            resolveSize(height, heightMeasureSpec),
        )
    }

    private fun displayLabel(): String = sessionId

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                holding = true
                holdStart = SystemClock.uptimeMillis()
                holdFraction = 0f
                postOnAnimation(holdTicker)
                isPressed = true
                return true
            }

            MotionEvent.ACTION_MOVE -> {
                // Sliding off the tab cancels the close, same as the web UI.
                val inside = event.x >= 0 && event.x <= width && event.y >= 0 && event.y <= height
                if (!inside && holding) {
                    cancelHold()
                }
                return true
            }

            MotionEvent.ACTION_UP -> {
                val wasHolding = holding
                val fraction = holdFraction
                cancelHold()
                isPressed = false
                if (wasHolding && fraction < 1f) {
                    performClick()
                    onTap?.invoke()
                }
                return true
            }

            MotionEvent.ACTION_CANCEL -> {
                cancelHold()
                isPressed = false
                return true
            }
        }
        return super.onTouchEvent(event)
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    private fun cancelHold() {
        holding = false
        holdFraction = 0f
        removeCallbacks(holdTicker)
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val inset = 3f * density
        bounds.set(inset, inset, width - inset, height - inset)

        val closing = holdFraction > 0f

        // --- body -----------------------------------------------------------
        fillPaint.color = when {
            closing -> ZmuxTheme.alpha(ZmuxTheme.ROSE, 0.16f)
            isActiveTab -> ZmuxTheme.alpha(ZmuxTheme.EMBER, 0.14f)
            else -> ZmuxTheme.SURFACE_SUNK
        }
        canvas.drawRoundRect(bounds, radius, radius, fillPaint)

        // --- hold-to-close fill sweeps left to right -------------------------
        if (closing) {
            canvas.save()
            canvas.clipRect(bounds.left, bounds.top, bounds.left + bounds.width() * holdFraction, bounds.bottom)
            fillPaint.color = ZmuxTheme.alpha(ZmuxTheme.ROSE, 0.42f)
            canvas.drawRoundRect(bounds, radius, radius, fillPaint)
            canvas.restore()
        }

        // --- outline ---------------------------------------------------------
        strokePaint.color = when {
            closing -> ZmuxTheme.ROSE
            isActiveTab -> ZmuxTheme.EMBER
            else -> ZmuxTheme.OUTLINE
        }
        canvas.drawRoundRect(bounds, radius, radius, strokePaint)

        // --- busy dot (teal = the machine is working) -------------------------
        var textShift = 0f
        if (isBusy) {
            dotPaint.color = ZmuxTheme.TEAL
            val cx = bounds.left + 11f * density
            canvas.drawCircle(cx, bounds.centerY(), 3f * density, dotPaint)
            textShift = 6f * density
        }

        // --- label -------------------------------------------------------------
        textPaint.color = when {
            closing -> ZmuxTheme.ROSE
            isActiveTab -> ZmuxTheme.EMBER
            else -> ZmuxTheme.TEXT_DIM
        }
        val label = displayLabel()
        val baseline = bounds.centerY() - (textPaint.descent() + textPaint.ascent()) / 2f
        canvas.drawText(label, bounds.centerX() + textShift / 2f, baseline, textPaint)
    }
}
