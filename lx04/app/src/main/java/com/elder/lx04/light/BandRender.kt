package com.elder.lx04.light

/**
 * 灯效的渲染产物 —— 一帧里两条色带各画什么。
 *
 * 分层设计的原因：
 *   · [Band] 描述"这条带整体长什么样"（全灭 / 单色 / 单段 / 多段）
 *   · [Segment] 描述"某一段的颜色与透明度"
 *
 * 固件侧的对应物是"20 颗灯珠的 RGB 数组"，这里因为色带是连续像素，
 * 用"分段 + 每段颜色"来表达，段数 [LedEngine.SEGMENTS]。
 */
data class BandRender(
    /** 左带：对话 / 提醒灯效（固件 #2~#9）。 */
    val left: Band,

    /** 右带：按键指示灯（固件 #1）。 */
    val right: Band
) {
    /**
     * 单条色带的绘制指令。
     */
    sealed class Band {
        /** 熄灭。 */
        object Off : Band()

        /**
         * 整条同色。
         *
         * @param color 颜色（已套感知亮度）
         * @param alpha 透明度 0~255
         */
        data class Solid(val color: Int, val alpha: Int) : Band()

        /**
         * 只有一段亮。
         *
         * @param index 段序号 0~[LedEngine.SEGMENTS]-1（0 = 最下）
         */
        data class Segment(val index: Int, val color: Int, val alpha: Int) : Band()

        /**
         * 多段各自不同（流星拖尾、流水、按键进度条）。
         */
        data class Multi(val segments: List<Segment>) : Band()
    }
}
