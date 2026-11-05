package com.elder.lx04.reminder

/**
 * 提醒表的序列化 —— 纯 Kotlin，不依赖 `org.json`、也不依赖 Android。
 *
 * ## 为什么不用 JSON
 *
 * 两个原因，都跟"能不能测"有关：
 *
 * 1. **JVM 单测里拿不到 `org.json` 的实现**。Gradle 单测的 `org.json` 是个
 *    stub，方法全返回 null（`isReturnDefaultValues`）。于是序列化会成为
 *    唯一没法在本地验证的一环 —— 而"提醒表读写坏了"恰恰是最难在现场复现
 *    的故障（老人当天所有提醒消失，日志里什么都没有）。
 * 2. 这份数据是本机私有状态，不需要跨语言/跨端读取，JSON 的互操作性换不到好处。
 *
 * ## 格式
 *
 * ```
 * 行 1：fmt=1;n=3;done=0;pend=0;fired=0;ord=20521
 * 行 2..：每条一行，\t 分隔 18 个字段；字符串字段 Base64（避免分隔符与换行冲突）
 * ```
 *
 * ## 容错原则
 *
 * [decode] **任何异常都返回 null，绝不抛**。上层拿到 null 就当"没有表"，
 * 由服务端下一轮心跳重新下发 —— 老人最多晚 5 分钟收到提醒，而不是整机起不来。
 */
object RmdCodec {

    private const val FMT = 1
    private const val FIELD_COUNT = 18

    /** 表 → 文本。 */
    fun encode(t: ReminderTable): String {
        val sb = StringBuilder(256)
        sb.append("fmt=").append(FMT)
            .append(";n=").append(t.items.size)
            .append(";done=").append(t.doneToday)
            .append(";pend=").append(t.pendingToday)
            .append(";fired=").append(t.firedToday)
            .append(";ord=").append(t.lastDayOrdinal)
            .append('\n')

        for (it in t.items) {
            sb.append(it.serverId).append('\t')
                .append(it.hour).append('\t')
                .append(it.minute).append('\t')
                .append(it.type).append('\t')
                .append(it.rtype).append('\t')
                .append(it.scheduleKind).append('\t')
                .append(it.repeatMask).append('\t')
                .append(it.onceYmd).append('\t')
                .append(it.monthMask).append('\t')
                .append(it.intervalDays).append('\t')
                .append(it.anchorOrdinal).append('\t')
                .append(it.startYmd).append('\t')
                .append(it.endYmd).append('\t')
                .append(it.firedDueAt).append('\t')
                .append(if (it.enabled) 1 else 0).append('\t')
                .append(b64(it.label)).append('\t')
                .append(b64(it.medLine)).append('\t')
                .append(b64(it.medicines.joinToString("\u0001")))
                .append('\n')
        }
        return sb.toString()
    }

    /** 文本 → 表。坏数据返回 null。 */
    fun decode(text: String): ReminderTable? = runCatching {
        val lines = text.split('\n').filter { it.isNotEmpty() }
        if (lines.isEmpty()) return null

        val head = lines[0].split(';').mapNotNull {
            val kv = it.split('=', limit = 2)
            if (kv.size == 2) kv[0] to kv[1] else null
        }.toMap()

        if (head["fmt"]?.toIntOrNull() != FMT) return null

        val table = ReminderTable()
        table.doneToday = head["done"]?.toIntOrNull() ?: 0
        table.pendingToday = head["pend"]?.toIntOrNull() ?: 0
        table.firedToday = head["fired"]?.toIntOrNull() ?: 0
        table.lastDayOrdinal = head["ord"]?.toIntOrNull() ?: 0

        for (line in lines.drop(1)) {
            val f = line.split('\t')
            if (f.size < FIELD_COUNT) continue      // 半截行跳过，不整表作废
            val item = ReminderItem(
                serverId = f[0].toLongOrNull() ?: 0L,
                hour = f[1].toIntOrNull() ?: 0,
                minute = f[2].toIntOrNull() ?: 0,
                type = f[3].toIntOrNull() ?: RmdType.SCHEDULE,
                rtype = f[4].toIntOrNull() ?: ReminderType.SCHEDULE,
                scheduleKind = f[5].toIntOrNull() ?: Schedule.KIND_DAILY,
                repeatMask = f[6].toIntOrNull() ?: 0,
                onceYmd = f[7].toIntOrNull() ?: 0,
                monthMask = f[8].toIntOrNull() ?: 0,
                intervalDays = f[9].toIntOrNull() ?: 1,
                anchorOrdinal = f[10].toIntOrNull() ?: 0,
                startYmd = f[11].toIntOrNull() ?: 0,
                endYmd = f[12].toIntOrNull() ?: 0,
                firedDueAt = f[13].toLongOrNull() ?: 0L,
                enabled = f[14] == "1",
                label = unb64(f[15]),
                medLine = unb64(f[16]),
                medicines = unb64(f[17])
                    .split('\u0001')
                    .filter { it.isNotEmpty() }
            )
            if (table.items.size >= RmdConst.MAX_ITEMS) break
            table.items.add(item)
        }

        table.clampMasks()
        table
    }.getOrNull()

    private fun b64(s: String): String =
        java.util.Base64.getEncoder().encodeToString(s.toByteArray(Charsets.UTF_8))

    private fun unb64(s: String): String = runCatching {
        String(java.util.Base64.getDecoder().decode(s), Charsets.UTF_8)
    }.getOrDefault("")
}
