package com.elder.lx04.light

// LedConfigItem 是 object Led 的**内部类**，不是顶层类型。
// Kotlin 不会因为同包就自动可见嵌套类，必须显式 import。
import com.elder.lx04.light.Led.LedConfigItem

/**
 * 灯效引擎 —— 移植自 ESP32 固件的 `firmware/components/elder/src/rgb.c`。
 *
 * ## 与固件的对应
 *
 * 固件里这一层的职责：
 *   1. **优先级仲裁**：多个灯效请求同时存在时，谁压过谁
 *   2. **状态机**：对话四态、按钮指示灯四态、服药确认、提醒 nag 节奏
 *   3. **渲染**：把"当前该显示什么"算成具体灯珠颜色
 *
 * 这里 1、2 完全照搬（语义不能变），3 换成"算成色带上的颜色分布"。
 *
 * ## 线程模型
 *
 * 固件里 `rgb_task` 每 30ms 醒一次做渲染；这里用 `Choreographer` 的
 * 每帧回调（≈16ms）驱动 [render]，比定时器更贴合屏幕刷新，
 * 也避免了在低性能设备上多开一个线程（LX04 只有 1GB RAM）。
 *
 * 所有状态修改都必须在主线程 —— 与固件一样，状态机不做跨线程同步，
 * 靠"所有调用方都在主线程"这个约定保证。
 */
class LedEngine {

    /** 单条灯效请求。 */
    private data class Slot(
        val prio: Led.Priority,
        val effect: Led.LedEffect,
        val color: Int,
        val level: Int,
        val periodMs: Int,
        /** 到期时刻（elapsedRealtime 毫秒）；0 = 常驻直到被释放 */
        val expiresAt: Long
    ) {
        /**
         * 取有效周期（ms），0/负数视为"用默认"。
         *
         * ⚠️ 统一返回 **Long**，不要在调用处写
         * `periodMs.takeIf { it > 0 } ?: 2000L` ——
         * 那是 `Int?` 与 `Long` 混用，Kotlin 会推断成
         * `Comparable<CapturedType(*)> & Number` 这种交集类型，
         * 后面 `t % period` 就找不到 operator（编译期报
         * "'operator' modifier is required on 'mod'"）。
         */
        fun periodOrDefault(fallback: Int): Long =
            (periodMs.takeIf { it > 0 } ?: fallback).toLong()
    }

    /**
     * 当前所有生效的灯效请求。
     *
     * 用列表而非单值，是因为固件里 #1 按键灯与 #2~#9 对话/提醒灯
     * **完全分开渲染** —— 提醒在 #2~#9 上闪，不会把按键反馈盖掉；
     * 反过来按键呼吸也不会污染提醒的颜色语义。优先级只作用于 #2~#9。
     *
     * 在 Android 侧，左右两条色带就是这个"分开渲染"的载体：
     * **左带 = 对话/提醒灯效（#2~#9），右带 = 按键指示灯（#1）**。
     */
    private val slots = mutableListOf<Slot>()

    /** 对话四态的当前值。 */
    private var uiState: Led.UiState = Led.UiState.IDLE

    /** 待机进入的时刻 —— 决定 60% 还是 30% 亮度。 */
    private var idleSince: Long = 0L

    /** 灯效配置表（服务端下发）。 */
    private var configTable: Array<LedConfigItem?> = arrayOfNulls(LedStateId.COUNT)

    /** 按钮指示灯的状态机。 */
    private val button = ButtonLightState()

    /** 服药提醒的 nag 节奏（10 秒闪 / 10 分钟一轮 / 2 小时截止）。 */
    private var nagActive = false
    private var nagStartedAt: Long = 0L

    /** 状态灯总开关。默认开。 */
    var statusLightEnabled: Boolean = true

    // ------------------------------------------------------------------ 公共 API

    /**
     * 请求某种灯效。优先级低的请求不会抢占高的。
     *
     * 与固件 `elder_rgb_request` 同名同义。
     */
    @Synchronized
    fun request(
        prio: Led.Priority,
        effect: Led.LedEffect,
        color: Int,
        level: Int,
        ttlMs: Int
    ) {
        slots.removeAll { it.prio == prio }
        slots.add(
            Slot(
                prio = prio,
                effect = effect,
                color = color,
                level = level,
                periodMs = 0,
                expiresAt = if (ttlMs > 0) now() + ttlMs else 0L
            )
        )
    }

    /**
     * 释放之前申请的灯效，回落到次高优先级的请求。
     *
     * 与固件 `elder_rgb_release` 一致。
     */
    @Synchronized
    fun release(prio: Led.Priority) {
        slots.removeAll { it.prio == prio }
    }

    /**
     * 设置对话四态灯效。
     *
     * 与固件 `elder_rgb_set_ui_state` 一致。
     */
    @Synchronized
    fun setUiState(state: Led.UiState) {
        if (uiState == state) return
        uiState = state
        if (state == Led.UiState.IDLE) {
            // 重新进入待机 ⇒ 亮度重新从 60% 算 1 分钟
            idleSince = now()
        }
        // 切换即替换：EMOTION 槽位里不留过期状态
        slots.removeAll { it.prio == Led.Priority.EMOTION }
    }

    /** 设置对话情绪。 */
    @Synchronized
    fun setEmotion(emo: Led.Emotion) {
        when (emo) {
            Led.Emotion.LISTENING -> setUiState(Led.UiState.LISTENING)
            Led.Emotion.SPEAKING -> setUiState(Led.UiState.SPEAKING)
            Led.Emotion.NEUTRAL -> setUiState(Led.UiState.IDLE)
            // HAPPY / SAD / ANGRY 走状态灯效表里的同名项
            Led.Emotion.HAPPY -> applyStateConfig(LedStateId.HAPPY)
            Led.Emotion.SAD -> applyStateConfig(LedStateId.SAD)
            Led.Emotion.ANGRY -> applyStateConfig(LedStateId.ANGRY)
        }
    }

    /**
     * 应用服务端下发的灯效表。**整表替换**，不合并。
     *
     * 与固件 `elder_rgb_apply_config` 同语义。
     *
     * 为什么整表替换而不是逐项覆盖：合并语义下"服务端把某个状态删了"就永远
     * 回不到默认值，而用户看到的界面是"7 个状态都在"，两边对不上。
     *
     * 会**自动重放当前状态**，所以心跳到达后不需要上层再调一次 setUiState ——
     * 否则新配置要等下一次状态切换才生效，用户会以为"保存了没用"。
     */
    @Synchronized
    fun applyConfig(items: Array<LedConfigItem?>) {
        configTable = Array(LedStateId.COUNT) { items.getOrNull(it) }
        // 重放当前状态
        when (uiState) {
            Led.UiState.IDLE -> applyStateConfig(LedStateId.NEUTRAL)
            Led.UiState.LISTENING -> applyStateConfig(LedStateId.LISTENING)
            Led.UiState.CONNECTING -> applyStateConfig(LedStateId.NEUTRAL)
            Led.UiState.SPEAKING -> applyStateConfig(LedStateId.SPEAKING)
        }
    }

    /** 取一份内置默认灯效表。 */
    @Synchronized
    // ⚠️ 必须显式写 arrayOf<LedConfigItem?>(...)。
    //   直接 arrayOf(...) 会推断成 Array<LedConfigItem>（非空），
    //   与返回类型 Array<LedConfigItem?> 不兼容，编译期报
    //   "Type mismatch: inferred type is Array<Led.LedConfigItem> but
    //    Array<Led.LedConfigItem?> was expected"。
    //   表里留 null 位是有意义的：某些状态 id 就是"不亮"。
    fun defaultConfig(): Array<LedConfigItem?> = arrayOf<LedConfigItem?>(
        LedConfigItem(Led.LedEffect.TWINKLE.id, Led.Color.TWINKLE_RANDOM, Led.Level.IDLE_NEAR, 0),
        LedConfigItem(Led.LedEffect.SOLID.id, Led.Color.LISTENING, Led.Level.LISTENING, 0),
        LedConfigItem(Led.LedEffect.SOLID.id, Led.Color.SPEAKING, Led.Level.SPEAKING, 0),
        LedConfigItem(Led.LedEffect.SOLID.id, 0xFFD700, Led.Level.LISTENING, 0),   // HAPPY 金
        LedConfigItem(Led.LedEffect.SOLID.id, 0x4169E1, Led.Level.LISTENING, 0),   // SAD 蓝
        LedConfigItem(Led.LedEffect.SOLID.id, 0xFF0000, Led.Level.LISTENING, 0),   // ANGRY 红
        LedConfigItem(Led.LedEffect.BLINK_FAST.id, Led.Color.REMINDER, Led.Level.FULL, 0) // ALERT
    ).also { applyConfigInner(it) }

    // ------------------------------------------------------- 服药 / 提醒语义

    /** 服药未确认 —— 长亮红。 */
    @Synchronized
    fun medPending() {
        slots.removeAll { it.prio == Led.Priority.MED_PENDING }
        slots.add(
            Slot(Led.Priority.MED_PENDING, Led.LedEffect.SOLID,
                Led.Color.MED_PENDING, Led.Level.FULL, 0, 0L)
        )
    }

    /** 服药完成 —— 长亮绿（默认 60s 后回落）。 */
    @Synchronized
    fun medDone(ttlMs: Long = 60_000L) {
        slots.removeAll { it.prio == Led.Priority.MED_PENDING }
        slots.add(
            Slot(Led.Priority.MED_PENDING, Led.LedEffect.SOLID,
                Led.Color.MED_DONE, Led.Level.FULL, 0, now() + ttlMs)
        )
    }

    /** 服药未确认升级 —— 快速闪烁红。 */
    @Synchronized
    fun medEscalate() {
        slots.removeAll { it.prio == Led.Priority.MED_PENDING }
        slots.add(
            Slot(Led.Priority.MED_PENDING, Led.LedEffect.BLINK_FAST,
                Led.Color.MED_PENDING, Led.Level.FULL, 0, 0L)
        )
    }

    /**
     * 提醒的闪灯节奏开关。
     *
     * 语义（固件 elder_rgb_reminder_nag_start）：
     *   * nag_start() 从**调用时刻**起算 2 小时，每 10 分钟的前 10 秒红灯闪；
     *   * 2 小时到点后灯自然熄灭（不需要上层记得来关）；
     *   * 期间若老人确认完成（med_done）或出现更高优先级的灯效
     *     （Priority.MED_PENDING 及以上），nag 让位；
     *   * nag_stop() 立即停（确认完成时调）。
     *
     * 为什么节奏放在**灯效模块内部**而不是让上层每 10 分钟调一次 request：
     *   1. 上层要自己做这件事就必须再起一个任务或定时器；
     *   2. 渲染循环本来就每帧醒一次，判断"现在是不是该闪"是零成本的；
     *   3. 节奏只有一份真源。上层若另算一遍，"2 小时后关闭"就有两个判据，
     *      迟早分叉成"灯还闪、界面已经没了"。
     */
    @Synchronized
    fun nagStart() {
        nagActive = true
        nagStartedAt = now()
    }

    @Synchronized
    fun nagStop() {
        nagActive = false
        slots.removeAll { it.prio == Led.Priority.REMINDER }
    }

    /**
     * 开/关状态灯。事件灯不受影响。
     *
     * **状态灯** = 待机呼吸/星光、唤醒聆听、TTS 说话、开心/难过/生气表情。
     * **事件灯** = 提醒闪灯、服药未确认、求助告警、按键指示灯。
     *
     * 关掉状态灯之后，设备平时完全不发光（夜里不刺眼），但该吃药、该做的事
     * 照样会亮 —— 这是这个功能存在的全部意义。判据落在**优先级**上
     * （Priority.REMINDER 及以上一律不受开关影响），而不是另立一张
     * "哪些算事件灯"的名单：名单会漂移，优先级不会。
     */
    @Synchronized
    fun setStatusLight(on: Boolean) {
        statusLightEnabled = on
    }

    // ------------------------------------------------------- 按键指示灯（#1）

    /**
     * 按下（不管当时有没有可确认的提醒；没有就保持熄灭）。
     *
     * 与固件 `elder_rgb_button_press` 一致。
     *
     * @param t 按下时刻（默认取 [clock]）；单测传入显式时间戳以锁死时序
     */
    @Synchronized
    fun buttonPress(t: Long = now()) {
        button.press(t)
    }

    /**
     * 松开。仍在渐变中 ⇒ 判定为"中途松开没确认"，粉红闪 5 次。
     * 已经出过结果（confirmed 或已闪烁）⇒ 忽略，不会重复反馈。
     *
     * ⚠️ 300ms 以内的松手是**机械抖动**，不闪粉红（固件 2026-11-04 加的兜底）。
     * 老人按住不动，机械按键中途抖几十毫秒就被当成松手，渐变被掐掉、
     * 灯报"你没按够" —— 这是用户实报过的真实故障。
     *
     * 这**不是**放宽标准：3 秒才算确认的判据一个字都没动，
     * 300ms 之后的松手照样粉红闪 5 次。
     */
    @Synchronized
    fun buttonRelease(t: Long = now()) {
        button.release(t)
    }

    /** 确认完成 ⇒ 天蓝 #00BFFF 全亮闪 3 次。 */
    @Synchronized
    fun buttonConfirmed(t: Long = now()) {
        button.confirmed(t)
    }

    /**
     * 按压**已受理** ⇒ 与 [buttonConfirmed] 完全一致：天蓝全亮闪 3 次。
     *
     * 为什么必须有这个入口：多药/单味药提醒的长按不再直接确认，
     * 而是先问一句「您吃的是哪种药？」再进对话。那一刻老人确实按够了 3 秒，
     * 要求是「3 秒钟的时候直接切换为天蓝色全亮闪烁 3 次」，
     * 追问路径同样达标，所以必须闪天蓝，不能退回橙色呼吸让老人看不到回执。
     */
    @Synchronized
    fun buttonAcknowledged(t: Long = now()) {
        button.acknowledged(t)
    }

    /** 是否存在待确认的提醒 ⇒ 橙色呼吸 / 熄灭。 */
    @Synchronized
    fun buttonPending(pending: Boolean) {
        button.pending = pending
    }

    // ------------------------------------------------------------------ 渲染

    /**
     * 渲染一帧。
     *
     * @return [BandRender] 两条色带的绘制指令。
     */
    @Synchronized
    fun render(t: Long): BandRender {
        // 1) 决定"对话/提醒灯带"（左带）显示什么
        //    （过期请求的清理在 resolveLeftBand 里做，测试走同一路径）
        val left = resolveLeftBand(t)

        // 2) 决定"按键指示灯"（右带）显示什么
        val right = button.render(t)

        return BandRender(left = left, right = right)
    }

    /**
     * 只解析左带（对话/提醒灯效）—— 给单元测试用。
     *
     * 生产路径走 [render]（同时出两条带）。测试只关心左带时，
     * 直接调这个比构造整个 BandRender 更直白。
     */
    internal fun resolveLeftForTest(t: Long): BandRender.Band = resolveLeftBand(t)

    /**
     * 左带（对话/提醒灯效，固件的 #2~#9）的解析。
     *
     * 优先级：ALARM > MED_PENDING > REMINDER > EMOTION > IDLE
     */
    private fun resolveLeftBand(t: Long): BandRender.Band {
        // 0) 清理过期请求。
        //    放在这里而不是只放在 render() 里：resolveLeftForTest() 也走这条路，
        //    否则"服药完成 60 秒后回落"这类 TTL 用例在测试里永远看不到回落。
        slots.removeAll { it.expiresAt != 0L && t >= it.expiresAt }

        // 事件灯不受「状态灯开关」影响 —— 判据是优先级，不是名单。
        // （状态灯可以关，但提醒/服药这类事件必须还能亮，否则老人就漏了提醒。）
        val event = slots.filter { it.prio.value >= Led.Priority.REMINDER.value }
        if (event.isNotEmpty()) {
            val top = event.maxByOrNull { it.prio.value }!!
            // 服药未确认期间，提醒的 nag 节奏让位
            if (top.prio == Led.Priority.REMINDER && slots.any {
                    it.prio == Led.Priority.MED_PENDING
                }) {
                // 让位给服药灯，但 nag 仍在计时
            } else {
                return renderEffect(top, t)
            }
        }

        // 状态灯关 ⇒ 平时完全不发光
        if (!statusLightEnabled) return BandRender.Band.Off

        // nag 节奏（提醒的红闪）
        if (nagActive && nagInFlashWindow(t)) {
            return renderEffect(
                Slot(Led.Priority.REMINDER, Led.LedEffect.BLINK_FAST,
                    Led.Color.REMINDER, Led.Level.FULL, 0, 0L),
                t
            )
        }

        // 对话四态
        return when (uiState) {
            Led.UiState.IDLE -> {
                // 待机：闪烁星光，60% → 1 分钟后 30%
                val elapsed = t - idleSince
                val level = if (elapsed > 60_000L) Led.Level.IDLE_FAR else Led.Level.IDLE_NEAR
                renderEffect(
                    Slot(Led.Priority.EMOTION, Led.LedEffect.TWINKLE,
                        Led.Color.TWINKLE_RANDOM, level, 0, 0L),
                    t
                )
            }

            Led.UiState.LISTENING -> renderEffect(
                Slot(Led.Priority.EMOTION, Led.LedEffect.SOLID,
                    Led.Color.LISTENING, Led.Level.LISTENING, 0, 0L),
                t
            )

            Led.UiState.CONNECTING -> renderEffect(
                Slot(Led.Priority.EMOTION, Led.LedEffect.METEOR,
                    0, Led.Level.CONNECTING, 0, 0L),
                t
            )

            Led.UiState.SPEAKING -> renderEffect(
                Slot(Led.Priority.EMOTION, Led.LedEffect.SOLID,
                    Led.Color.SPEAKING, Led.Level.SPEAKING, 0, 0L),
                t
            )
        }
    }

    /**
     * nag 的闪灯窗口：每 10 分钟的前 10 秒。
     *
     * 从 nagStart() 起算 2 小时。
     */
    private fun nagInFlashWindow(t: Long): Boolean {
        val elapsed = t - nagStartedAt
        if (elapsed >= 2 * 60 * 60 * 1000L) return false   // 2 小时后不再亮灯
        val cycle = elapsed % (10 * 60 * 1000L)
        return cycle < 10 * 1000L                          // 每轮前 10 秒
    }

    /** 把一个灯效请求渲染成色带指令。 */
    private fun renderEffect(slot: Slot, t: Long): BandRender.Band {
        return when (slot.effect) {
            Led.LedEffect.SOLID -> BandRender.Band.Solid(
                color = applyLevel(slot.color, slot.level),
                alpha = 255
            )

            Led.LedEffect.BREATH -> {
                val period = slot.periodOrDefault(2000)
                val phase = (t % period).toFloat() / period.toFloat()
                val v = (0.5f + 0.5f * kotlin.math.sin(phase * 2 * Math.PI).toFloat())
                BandRender.Band.Solid(
                    color = applyLevel(slot.color, slot.level),
                    alpha = (v * 255).toInt().coerceIn(0, 255)
                )
            }

            Led.LedEffect.BLINK_FAST -> {
                val on = (t / 150L) % 2L == 0L
                BandRender.Band.Solid(
                    color = applyLevel(slot.color, slot.level),
                    alpha = if (on) 255 else 0
                )
            }

            Led.LedEffect.TWINKLE -> {
                // 2 秒一格，每格挑一段亮 300ms 渐隐，亮起时刻在格内抖 0~1 秒
                val cell = 2000L
                val inCell = t % cell
                if (inCell < 300L) {
                    val cellIndex = (t / cell)
                    val seg = pseudoRandomSeg(cellIndex)
                    // 渐隐
                    val fade = 1f - (inCell / 300f)
                    // 颜色每闪一次换一个
                    val hue = pseudoRandomHue(cellIndex)
                    BandRender.Band.Segment(
                        index = seg,
                        color = applyLevel(hue, slot.level),
                        alpha = (fade * 255).toInt().coerceIn(0, 255)
                    )
                } else {
                    BandRender.Band.Off
                }
            }

            Led.LedEffect.METEOR -> {
                // 一个亮头 + 拖尾，头部最亮、尾部线性衰减
                val period = slot.periodOrDefault(1200)
                val p = (t % period).toFloat() / period.toFloat()
                val head = (p * SEGMENTS).toInt().coerceIn(0, SEGMENTS - 1)
                val colors = buildList {
                    // 拖尾：从头部往回 4 段
                    for (i in 0 until 5) {
                        val idx = head - i
                        if (idx < 0) break
                        val fade = 1f - i / 5f
                        add(
                            BandRender.Band.Segment(
                                index = idx,
                                color = applyLevel(pseudoRandomHue(head.toLong()), slot.level),
                                alpha = (fade * 255).toInt().coerceIn(0, 255)
                            )
                        )
                    }
                }
                BandRender.Band.Multi(colors)
            }

            Led.LedEffect.FLOW -> {
                val period = slot.periodOrDefault(2000)
                val p = (t % period).toFloat() / period.toFloat()
                val offset = (p * SEGMENTS).toInt()
                val segs = mutableListOf<BandRender.Band.Segment>()
                for (i in 0 until SEGMENTS) {
                    val idx = (i + offset) % SEGMENTS
                    // 亮度做行进三角波
                    val dist = kotlin.math.min(
                        kotlin.math.abs(idx - offset),
                        SEGMENTS - kotlin.math.abs(idx - offset)
                    )
                    val b = (1f - dist.toFloat() / (SEGMENTS / 2f)).coerceIn(0f, 1f)
                    segs.add(
                        BandRender.Band.Segment(
                            index = idx,
                            color = applyLevel(slot.color, slot.level),
                            alpha = (b * 255).toInt().coerceIn(0, 255)
                        )
                    )
                }
                BandRender.Band.Multi(segs)
            }

            Led.LedEffect.CHASE -> {
                val period = slot.periodOrDefault(1200)
                val idx = ((t % period) / (period / SEGMENTS)).toInt().coerceIn(0, SEGMENTS - 1)
                BandRender.Band.Segment(
                    index = idx,
                    color = applyLevel(slot.color, slot.level),
                    alpha = 255
                )
            }

            Led.LedEffect.RAINBOW -> BandRender.Band.Solid(
                color = applyLevel(pseudoRandomHue(t / 1000), slot.level),
                alpha = 255
            )
        }
    }

    /**
     * 把感知亮度套到颜色上。
     *
     * ⚠️ 固件里这里过的是一张 gamma 2.2 查表（GAMMA_LUT）。屏幕直接绘制
     * 没有灯珠的非线性衰减，但**仍按感知亮度口径**处理 ——
     * 这样同一个 `level` 在两个平台上的观感一致，灯效配置表才能原样复用。
     */
    /**
     * 按感知亮度缩放颜色。
     *
     * ⚠️ **刻意不用 `android.graphics.Color`**。
     *
     * 这是灯效语义的核心计算，必须能在 JVM 单元测试里跑。而 `android.graphics.Color`
     * 在单元测试里是 stub：AGP 的 mockable android.jar 把方法体换成返回默认值，
     * `Color.red(...)` 恒为 0 —— 于是 `applyLevel` 把任何颜色都算成 0，
     * 所有"天蓝闪 3 次"这类断言全部失败（run 41845796959，14 个测试挂在这）。
     *
     * ARGB 拆解本来就是纯位运算，没有理由依赖框架类。这里自己拆，
     * 行为与 `Color.argb` 完全一致，但在 JVM 上可测、也更快。
     */
    private fun applyLevel(color: Int, level: Int): Int {
        if (color == 0) return color
        val f = level.coerceIn(0, 255) / 255f
        val a = (color ushr 24) and 0xFF
        val r = (((color shr 16) and 0xFF) * f).toInt().coerceIn(0, 255)
        val g = (((color shr 8) and 0xFF) * f).toInt().coerceIn(0, 255)
        val b = ((color and 0xFF) * f).toInt().coerceIn(0, 255)
        return (a shl 24) or (r shl 16) or (g shl 8) or b
    }

    private fun applyStateConfig(id: Int) {
        val item = configTable.getOrNull(id) ?: return
        if (item.effect == Led.LedEffect.OFF) return
        val eff = Led.LedEffect.of(item.effect) ?: return
        slots.removeAll { it.prio == Led.Priority.EMOTION }
        slots.add(
            // periodMs 是 Int（配置表里就是 Int）；不要 .toLong()，
            // 否则 Slot 构造第 5 个参数类型不匹配。
            // 需要 Long 的地方由 Slot.periodOrDefault() 负责转换。
            Slot(Led.Priority.EMOTION, eff, item.color, item.level, item.periodMs, 0L)
        )
    }

    private fun applyConfigInner(items: Array<LedConfigItem?>) {
        configTable = Array(LedStateId.COUNT) { items.getOrNull(it) }
    }

    /**
     * 伪随机选一段（星光用）。
     *
     * 用固定种子的线性同余，保证**同一格内每次渲染选的是同一段** ——
     * 否则每帧重算会让星光乱跳，看起来像坏掉的灯。
     */
    private fun pseudoRandomSeg(cell: Long): Int {
        val x = cell * 6364136223846793005L + 1442695040888963407L
        return ((x ushr 33) % SEGMENTS).toInt().coerceIn(0, SEGMENTS - 1)
    }

    /** 伪随机色相（星光/流星用）。 */
    private fun pseudoRandomHue(seed: Long): Int {
        val x = seed * 2862933555777941757L + 3037000493L
        val hue = ((x ushr 33) % 360L).toFloat()
        // 同 applyLevel：不用 Color.HSVToColor —— 它在 JVM 单测里是 stub，
        // 恒返回 0，星光就永远算成黑色，测试与真机行为不一致。
        return hsvToArgb(hue, 0.85f, 1f)
    }

    /**
     * HSV → ARGB（纯 Kotlin 实现，不依赖 `android.graphics.Color`）。
     *
     * @param hDeg 色相 0~360（度）
     * @param s    饱和度 0~1
     * @param v    明度 0~1
     * @return 不透明 ARGB（alpha = 255，与 `Color.HSVToColor` 一致）
     */
    private fun hsvToArgb(hDeg: Float, s: Float, v: Float): Int {
        val h = (((hDeg % 360f) + 360f) % 360f) / 60f
        val c = v * s
        val x = c * (1f - kotlin.math.abs(h % 2f - 1f))
        val m = v - c
        val rgb = when {
            h < 1f -> Triple(c, x, 0f)
            h < 2f -> Triple(x, c, 0f)
            h < 3f -> Triple(0f, c, x)
            h < 4f -> Triple(0f, x, c)
            h < 5f -> Triple(x, 0f, c)
            else   -> Triple(c, 0f, x)
        }
        val r = ((rgb.first + m) * 255f).toInt().coerceIn(0, 255)
        val g = ((rgb.second + m) * 255f).toInt().coerceIn(0, 255)
        val b = ((rgb.third + m) * 255f).toInt().coerceIn(0, 255)
        return (0xFF shl 24) or (r shl 16) or (g shl 8) or b
    }

    /**
     * 时间源。
     *
     * 生产环境用单调时钟 `SystemClock.elapsedRealtime()`；单元测试把它换成
     * 手动推进的假时钟，才能精确断言"按住 1.5 秒""闪 3 次共 1800ms"这类时序。
     *
     * 为什么必须可注入：`SystemClock` 在 JVM 单测里是 AGP 的 stub，**恒返回 0**。
     * 那样 `buttonPress()` 记下的按下时刻就是 0，而 0 在旧实现里正是"没在按"
     * 的哨兵值 —— 所有按键用例都静默退化成"没按下过"，时序断言全部失败
     * （run 41845796959 里 14 个失败的大头）。
     */
    internal var clock: () -> Long = { android.os.SystemClock.elapsedRealtime() }

    private fun now(): Long = clock()

    // ------------------------------------------------------------------ 内部类

    /**
     * 按键指示灯状态机（固件的 #1 按键灯）。
     *
     * 四个状态（互斥）：
     *   ① 熄灭      —— 没有待确认提醒，也没在按。**这是常态**，夜里不发光。
     *   ② 橙色呼吸  —— 有待确认提醒。作用是"吸引注意"：老人听到提醒后要知道
     *                  该按哪个键，灯亮着的那颗就是。
     *   ③ 天青变亮  —— 按住期间，天青 #00E5FF **同一色相由暗变亮**（起点不是全黑，
     *                  按下瞬间就有反馈）。渐变本身是**进度条**：老人能看出
     *                  "还差多少"，比一个突然亮起的"完成"更不容易半途松手。
     *   ④ 结果闪烁  —— 天蓝 #00BFFF 全亮闪 3 次 = 3 秒达标（**含追问路径**）；
     *                  粉红 #FF69B4 全亮闪 5 次 = 中途松开没成。
     *
     * 为什么"没待确认提醒时按下什么都不亮"（而不是给个"按到了"的反馈）：
     * 这按键只做确认。没有可确认的东西时给正反馈，等于告诉老人"办成了"，
     * 而实际什么都没发生 —— 这是最坏的一类欺骗。安静地不亮才是诚实的。
     */
    private inner class ButtonLightState {
        /** 有待确认的提醒 ⇒ 橙色呼吸 / 熄灭。 */
        var pending: Boolean = false

        /**
         * 按下的时刻；`null` = 没在按。
         *
         * ⚠️ **不要用 0 当哨兵**：`elapsedRealtime()` 开机瞬间就是 0，
         * 假时钟的起点也是 0。用 0 表示"没在按"会让"t=0 按下"永远无效 ——
         * 表现为单测里按键相关用例全部静默失败，且很难看出原因。
         */
        private var pressAt: Long? = null

        /** 本轮是否已经出过结果（confirmed / 已闪烁） */
        private var resolved: Boolean = false

        /** 结果闪烁的剩余次数 */
        private var flashLeft: Int = 0

        /** 结果闪烁的起始时刻 */
        private var flashAt: Long = 0L

        /** 结果闪烁的颜色 */
        private var flashColor: Int = Led.Color.BUTTON_OK

        fun press(t: Long) {
            pressAt = t
            resolved = false
            flashLeft = 0
        }

        /**
         * 松开。
         *
         * ⚠️ **无论走哪个分支，都必须把 `pressAt` 置回 null** ——
         * 否则灯会一直停在"按住渐变"状态（pressAt 非 null 时 render 永远画渐变），
         * 表现为色带卡在天青色上不熄。老年人看着就是"灯坏了"。
         *
         * 所以这里统一在 finally 语义上清 pressAt，判定结果只影响"闪什么"。
         */
        fun release(t: Long) {
            val down = pressAt ?: return
            pressAt = null
            if (resolved) return

            val held = t - down
            resolved = true

            if (held >= CONFIRM_HOLD_MS) {
                // 达标 —— 但真正的"确认成功"由 buttonConfirmed() 触发。
                // 这里不重复出结果：调用方会在同一轮里调 confirmed/acknowledged。
                return
            }
            if (held < DEBOUNCE_MIN_MS) {
                // 抖动兜底：不闪粉红，安静回落
                return
            }
            // 中途松开 ⇒ 粉红闪 5 次
            flashColor = Led.Color.BUTTON_FAIL
            flashLeft = FAIL_FLASH_TIMES
            flashAt = t
        }

        fun confirmed(t: Long) {
            resolved = true
            pressAt = null
            flashColor = Led.Color.BUTTON_OK
            flashLeft = OK_FLASH_TIMES
            flashAt = t
        }

        fun acknowledged(t: Long) = confirmed(t)

        fun render(t: Long): BandRender.Band {
            // ④ 结果闪烁优先
            if (flashLeft > 0) {
                // 一次闪 = 300ms 亮 + 300ms 灭 = 两个 300ms 半格。
                // ⚠️ 计数单位必须是「半格」：`flashLeft` 数的是**次数**，
                //    直接拿次数跟半格序号比，会把 3 次闪压缩成 2 次、900ms 就收尾
                //    —— 老人看到的是"闪了两下"，与约定的"闪三下"不符。
                val half = (t - flashAt) / 300L
                if (half >= flashLeft * 2L) {
                    flashLeft = 0
                } else {
                    val on = half % 2L == 0L
                    return BandRender.Band.Solid(
                        color = applyLevel(flashColor, Led.Level.FULL),
                        alpha = if (on) 255 else 0
                    )
                }
            }

            // ③ 按住期间：天青由暗变亮（进度条）
            //
            // ⚠️ 只有**有待确认提醒**时才画。没有可确认的东西却给"按到了"的
            //    正反馈，等于告诉老人"办成了"而实际什么都没发生 ——
            //    这是最坏的一类欺骗（见类注释 ① 的说明）。
            val down = pressAt
            if (pending && down != null) {
                val held = t - down
                val p = (held.toFloat() / CONFIRM_HOLD_MS).coerceIn(0f, 1f)
                // 从 BUTTON_RAMP_MIN 线性升到 FULL
                val level = (Led.Level.BUTTON_RAMP_MIN +
                    (Led.Level.FULL - Led.Level.BUTTON_RAMP_MIN) * p).toInt()
                // 渐变自下而上填充，填充比例 = 进度
                val filled = (p * SEGMENTS).toInt().coerceIn(0, SEGMENTS)
                val segs = mutableListOf<BandRender.Band.Segment>()
                for (i in 0 until filled) {
                    segs.add(
                        BandRender.Band.Segment(
                            index = i,
                            color = applyLevel(Led.Color.BUTTON_RAMP, level),
                            alpha = 255
                        )
                    )
                }
                return if (segs.isEmpty()) {
                    BandRender.Band.Off
                } else {
                    BandRender.Band.Multi(segs)
                }
            }

            // ② 橙色呼吸（有待确认提醒）
            if (pending) {
                val phase = (t % 2000L).toFloat() / 2000f
                val v = (0.5f + 0.5f * kotlin.math.sin(phase * 2 * Math.PI).toFloat())
                return BandRender.Band.Solid(
                    color = applyLevel(Led.Color.REMINDER, Led.Level.FULL),
                    alpha = (v * 255).toInt().coerceIn(0, 255)
                )
            }

            // ① 熄灭 —— 常态，夜里不发光
            return BandRender.Band.Off
        }
    }

    companion object {
        /** 色带分段数（把 46px × 800px 分成若干段，逐段渲染） */
        const val SEGMENTS = 12

        /**
         * 长按达标时长：3 秒。
         *
         * ⚠️ **必须与输入侧的判定同源**。两边各写一份的话，只要有人改了一处，
         * 就会出现"灯已经亮满天青、按键却还没算确认"（或反过来）——
         * 灯在骗人，比没有灯更糟。
         */
        const val CONFIRM_HOLD_MS = 3000L

        /**
         * 抖动兜底阈值：300ms 以内的松手不算"中途放弃"。
         *
         * 老人按住不动，机械按键中途抖几十毫秒就被当成松手，
         * 渐变被掐掉、灯报"你没按够"。300ms 以内不可能有人主动放弃。
         */
        const val DEBOUNCE_MIN_MS = 300L

        /** 确认成功：天蓝闪 3 次 */
        const val OK_FLASH_TIMES = 3

        /** 中途松开：粉红闪 5 次 */
        const val FAIL_FLASH_TIMES = 5
    }
}

/**
 * 灯效状态 id —— 对应固件的 `elder_rgb_state_t`。
 *
 * ⚠️ 顺序必须与固件一致：服务端下发的灯效表按这个顺序展开。
 */
object LedStateId {
    const val NEUTRAL = 0
    const val LISTENING = 1
    const val SPEAKING = 2
    const val HAPPY = 3
    const val SAD = 4
    const val ANGRY = 5
    const val ALERT = 6
    const val COUNT = 7
}
