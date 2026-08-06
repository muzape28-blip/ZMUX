package com.zmux.terminal

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.util.AttributeSet
import android.view.View
import java.util.Random

/**
 * Lightweight "cmatrix" rain rendered onto a persistent bitmap buffer.
 *
 * Self-contained: no WebView, no native deps, works from minSdk 26. A
 * translucent black wash is painted every frame so prior glyphs fade into the
 * classic trailing glow instead of being cleared outright.
 *
 * NOTE: this view is purely cosmetic. It never touches the Python / PRoot /
 * Chaquopy stack, so it cannot affect the W^X ("Permission denied") or
 * APP_DIR-alignment regression gates.
 */
class MatrixRainView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private val rng = Random()
    private val glyphs =
        "0123456789ABCDEFｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ".toCharArray()
    private val paint = Paint().apply {
        typeface = Typeface.MONOSPACE
        isAntiAlias = true
    }

    private var columnCount = 0
    private var fontSize = 0f
    private lateinit var drops: IntArray
    private lateinit var buffer: Bitmap
    private lateinit var bcanvas: Canvas

    private val headColor = Color.parseColor("#C8FFD0")
    private val tailColor = Color.parseColor("#22C55E")
    private val handler = Handler(Looper.getMainLooper())
    private val frameDelay = 28L // ~36 fps

    private val runner = object : Runnable {
        override fun run() {
            // Guard until the first layout pass has allocated the buffer.
            if (::buffer.isInitialized) drawFrame()
            handler.postDelayed(this, frameDelay)
        }
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        fontSize = (width / 44f).coerceAtLeast(14f)
        paint.textSize = fontSize
        columnCount = (width / fontSize).toInt() + 1
        drops = IntArray(columnCount) { rng.nextInt((height / fontSize).toInt().coerceAtLeast(1)) }
        if (::buffer.isInitialized) buffer.recycle()
        buffer = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        bcanvas = Canvas(buffer)
        bcanvas.drawColor(Color.BLACK)
    }

    fun start() { handler.post(runner) }
    fun stop() { handler.removeCallbacks(runner) }

    private fun randGlyph(): String = glyphs[rng.nextInt(glyphs.size)].toString()

    private fun drawFrame() {
        // Translucent black wash -> trails fade out over a few frames.
        bcanvas.drawColor(Color.argb(38, 0, 0, 0))
        for (c in 0 until columnCount) {
            val x = c * fontSize
            val y = drops[c] * fontSize
            paint.color = headColor
            bcanvas.drawText(randGlyph(), x, y, paint)
            paint.color = tailColor
            if (rng.nextFloat() < 0.55f) {
                bcanvas.drawText(randGlyph(), x, y - fontSize, paint)
            }
            drops[c] += 1
            if (y > height && rng.nextFloat() > 0.975f) {
                drops[c] = 0
            }
        }
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        if (::buffer.isInitialized) canvas.drawBitmap(buffer, 0f, 0f, null)
        else canvas.drawColor(Color.BLACK)
    }

    override fun onDetachedFromWindow() {
        stop()
        if (::buffer.isInitialized) buffer.recycle()
        super.onDetachedFromWindow()
    }
}
