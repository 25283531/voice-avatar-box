package com.elder.lx04.light

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.util.AttributeSet
import android.util.TypedValue
import android.view.Choreographer
import android.view.View

/**
 * 左右竖向色带 View —— 在屏幕两侧各画 5mm 宽的灯效区。
 *
 * ## 尺寸换算
 *
 * LX04 屏幕 **480×800**，物理 3.97 寸宽 ⇒ 约 121 dpi。
 * 5mm ≈ 5/25.4 × 121 ≈ **24px**（按短边 480px 折算）。
 *
 * ⚠️ 固件侧算的是 46px，那是按"480px 宽对应整个 3.97 寸"直接等比的粗算，
 * 与按物理尺寸精确折算有出入。**这里采用精确折算（≈24px）**，
 * 因为用户要的是"5mm 宽"这个物理尺寸，不是"46 个像素"。
 * 若要改成整像素且更宽的观感，改 [bandWidthPx] 即可。
 *
 * ## 布局策略
 *
 * 色带是**独立 View**，画在主内容两侧（而不是画在主内容里），好处是：
 *   · 主内容（字幕/头像）自己怎么排都不影响色带
 *   · 色带可以整条重绘，不用跟内容做 invalidate 协调
 *
 * ## 刷新驱动
 *
 * 用 [Choreographer] 的帧回调而不是 `postDelayed` 循环：
 *   · 与屏幕刷新同步，不会有撕裂
 *   · 不额外起线程（LX04 只有 1GB RAM）
 *   · 页面不可见时自动停（[onVisibilityChanged]）
 */
class LedBandView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    /** 色带宽度（px）。默认按 5mm 精确折算。 */
    var bandWidthPx: Int = defaultBandWidthPx(context)

    /** 左带（对话/提醒灯效，固件的 #2~#9）。 */
    var leftBand: BandRender.Band = BandRender.Band.Off

    /** 右带（按键指示灯，固件的 #1）。 */
    var rightBand: BandRender.Band = BandRender.Band.Off

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val segRect = Rect()

    private var running = false

    private val frameCallback = object : Choreographer.FrameCallback {
        override fun doFrame(frameTimeNanos: Long) {
            if (!running) return
            invalidate()
            Choreographer.getInstance().postFrameCallback(this)
        }
    }

    init {
        // 色带自绘，不需要背景
        setWillNotDraw(false)
        setBackgroundColor(Color.TRANSPARENT)
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        start()
    }

    override fun onDetachedFromWindow() {
        stop()
        super.onDetachedFromWindow()
    }

    override fun onVisibilityChanged(changedView: View, visibility: Int) {
        super.onVisibilityChanged(changedView, visibility)
        if (visibility == VISIBLE) start() else stop()
    }

    private fun start() {
        if (running) return
        running = true
        Choreographer.getInstance().postFrameCallback(frameCallback)
    }

    private fun stop() {
        if (!running) return
        running = false
        Choreographer.getInstance().removeFrameCallback(frameCallback)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = bandWidthPx
        val h = height

        // 左带
        drawBand(canvas, 0, w, h, leftBand)
        // 右带
        drawBand(canvas, width - w, width, h, rightBand)
    }

    /**
     * 画一条色带。
     *
     * 单色（Solid）：整条一个颜色 + 一个透明度。
     * 分段（Multi）：每段独立颜色与透明度，段高 = 色带高 / SEGMENTS。
     */
    private fun drawBand(
        canvas: Canvas,
        left: Int,
        right: Int,
        bandHeight: Int,
        band: BandRender.Band
    ) {
        when (band) {
            is BandRender.Band.Off -> {
                // 熄灭 —— 不画任何东西（底下是内容区的背景）
            }

            is BandRender.Band.Solid -> {
                paint.color = band.color
                paint.alpha = band.alpha
                canvas.drawRect(left.toFloat(), 0f, right.toFloat(), bandHeight.toFloat(), paint)
            }

            is BandRender.Band.Segment -> {
                paint.color = band.color
                paint.alpha = band.alpha
                val segH = bandHeight.toFloat() / LedEngine.SEGMENTS
                val top = segH * band.index
                canvas.drawRect(left.toFloat(), top, right.toFloat(), top + segH, paint)
            }

            is BandRender.Band.Multi -> {
                val segH = bandHeight.toFloat() / LedEngine.SEGMENTS
                for (s in band.segments) {
                    paint.color = s.color
                    paint.alpha = s.alpha
                    val top = segH * s.index
                    canvas.drawRect(left.toFloat(), top, right.toFloat(), top + segH, paint)
                }
            }
        }
    }

    companion object {
        /**
         * 默认色带宽度：5mm 折算成像素。
         *
         * 换算走 `TypedValue.applyDimension`（用真实 DisplayMetrics 的 density），
         * 不硬编码 121dpi —— 不同固件/分辨率下 density 可能不同，
         * 硬编码会让"5mm"变成"5mm 乘个错系数"。
         */
        fun defaultBandWidthPx(context: Context): Int {
            val mm5 = TypedValue.applyDimension(
                TypedValue.MM,
                5f,
                context.resources.displayMetrics
            )
            return mm5.toInt().coerceAtLeast(12)
        }
    }
}
