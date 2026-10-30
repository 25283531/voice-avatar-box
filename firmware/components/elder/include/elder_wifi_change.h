#ifndef ELDER_WIFI_CHANGE_H
#define ELDER_WIFI_CHANGE_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 已配网设备「远程改 WiFi」—— 播报预告 + 只连目标的试连 + 失败零改动
 *
 * 需求：服务端心跳下发 ``{"wifi_change":{"req_id":3,"ssid":"...","password":"..."}}``，
 * 设备先播报「1 分钟内切换」，再试连新网络；连上（且**实际关联的 AP 就是目标
 * SSID**）才把新配置写进 ``SsidManager``，否则什么都不改，下一次心跳回报结果。
 *
 * ── 安全红线（违反任何一条 = 老人设备失联 = 必须上门）────────────────
 * 1. **验证成功之前 ``SsidManager`` 一个字都不改**。新配置只活在 RAM 里，
 *    连密码都不落 NVS。所以「失败回滚」是空操作，任何异常路径都天然回到旧配置。
 * 2. 任何异常路径都必须能回到旧配置。宁可放弃这次改动。
 * 3. ⚠️ 不要把密码先 ``AddSsid`` 进去再试连。``AddSsid`` 对**同名 SSID 是
 *    覆盖密码**，一旦先写进去，回滚就必须靠"备份旧密码再写回"—— 而如果用户
 *    正是因为旧密码失效才来改（换了路由器密码），写回旧密码会让设备**永久
 *    失联**。本模块 2026-10-31 起不再这么做，``pend_pwd`` / ``pend_old_pwd``
 *    两个 key 也不再写（老设备留下的会被 ``clear_pending()`` 擦掉）。
 * 4. 断电保护：``pend_*`` 带 req_id，开机时若 ``pend_*`` 还在，说明上次试连
 *    没走完（见 ``elder_wifi_change_boot_recover``）。因为配置从未被提前修改，
 *    收尾只需要"清标记 + 回报终态"。
 *
 * ── 为什么单独一个文件、且跑在独立任务里 ─────────────────────────────
 * 一次改 WiFi 最长约 60 秒播报 + 30 秒试连 + 25 秒交回网络，期间 WiFi 会被
 * ``WifiStation::Stop()`` 掉。绝不能塞进心跳任务（会卡住整个同步）或 MCP 工具的
 * app.Schedule（会卡住对话）。所以 ``elder_wifi_change_apply()`` 只做参数校验
 * + 起任务，立刻返回。
 *
 * ── 为什么试连不经过 ``WifiStation``（2026-10-31）──────────────────────
 * ``WifiStation::HandleScanResult()`` 把扫描结果按 RSSI 排序后与已知网络求交集，
 * ``StartConnect()`` 取队首 —— "先连哪个"完全由信号强度决定。两个 SSID 都在
 * 范围里时，旧网络信号更强就必然先连旧的，于是 ``GetSsid() != 目标`` 被判失败
 * 回滚，改了等于没改（用户 2026-10-31 报的「有效性和准确性」问题）。
 * 组件没有"只连某个 SSID"的能力，且 ``managed_components/`` 不在 git 里（改它
 * 下次构建就被覆盖），所以改成 ``Stop()`` 之后**自己独占 WiFi 栈**、只写目标
 * 一条配置、用 ``esp_wifi_sta_get_ap_info()`` 读实际关联的 AP 判成败。
 * 细节见 ``src/wifi_change.cc`` 文件头。
 *
 * ── 实现落在 .cc ──────────────────────────────────────────────────────
 * ``SsidManager`` / ``WifiStation`` 是 C++ 类（std::string / std::vector），
 * C 里 include 不了，所以实现是 ``src/wifi_change.cc``，对外只暴露本头文件的
 * 纯 C 接口，供 sync.c 调用。
 */

/** SSID 上限（802.11 规定 32 字节 + NUL） */
#define ELDER_WIFI_SSID_MAX   33
/** 密码上限（WPA2 passphrase 63 字符 + NUL；开放网络允许空串） */
#define ELDER_WIFI_PWD_MAX    65
/** 失败原因串上限。服务端只是存日志，不需要长文本 */
#define ELDER_WIFI_REASON_MAX 32

/** 一次改 WiFi 的终态，由心跳请求体带回服务端（``wifi_result`` 对象） */
typedef struct {
    uint32_t req_id;                          /* 服务端下发的请求序号，原样回传 */
    bool     ok;                              /* true = 已切到新网络并落 NVS */
    char     ssid[ELDER_WIFI_SSID_MAX];       /* 目标 SSID（不是回落到的那一个） */
    char     reason[ELDER_WIFI_REASON_MAX];   /* ok=false 时的短原因，ok=true 时为空串 */
} elder_wifi_result_t;

/**
 * @brief 受理一次改 WiFi 请求（非阻塞，立刻返回）。
 *
 * 校验参数 → 起 ``elder_wifi_try`` 任务 → 返回。任务里依次做：播报预告并等
 * 60 秒 → 交出 WiFi 栈 → 只连目标试连（最长 30 秒）→ 成功才落 NVS → 交回
 * 网络并确认确实在目标上。结果落 NVS，由下一次 ``/api/device/sync`` 的请求体
 * 带回。
 *
 * @param req_id   服务端请求序号（回传用）
 * @param ssid     目标 SSID，非空且 **≤31 字节**（802.11 上限是 32，但整条链路
 *                 上还有别的坑，现实中 32 字节 SSID 极罕见，直接拒掉并回报）
 * @param password 目标密码，**≤63 字符**（WPA2 passphrase 硬上限；写 64 会在
 *                 strncpy 里被静默截断成 63，表现是"密码明明对、永远认证失败"。
 *                 开放网络传 ""）
 * @return true  = 已受理（不代表成功）
 *         false = 参数非法 / 已有改 WiFi 在进行 / 任务创建失败。前三种会落一条
 *                 ``ok=false`` 的终态，由下一次心跳回报（服务端才不会永远停在
 *                 delivered）；「已有在进行」不落终态，等服务端下一轮重发。
 */
bool elder_wifi_change_apply(uint32_t req_id, const char *ssid, const char *password);

/** 是否有一次试连正在跑（调试/日志用；不影响调度） */
bool elder_wifi_change_busy(void);

/**
 * @brief 取「待回报」的终态（只读，不清除）。
 *
 * 心跳在拼请求体前调用；POST 拿到 2xx 之后再调
 * ``elder_wifi_change_result_ack()`` 清除。分两步是为了「网络失败不丢结果」：
 * 请求没发出去时结果还在 NVS 里，下一轮心跳接着带。
 *
 * @param out 出参；无待回报结果时**不写**
 * @return true = 有结果
 */
bool elder_wifi_change_pending_result(elder_wifi_result_t *out);

/**
 * @brief 确认结果已送达（清 NVS）。只在心跳收到 2xx 后调用。
 *
 * 带 req_id 比对：期间若又产生了新结果（理论上不会，试连是串行的），
 * 不误删新的那条。
 */
void elder_wifi_change_result_ack(uint32_t req_id);

/**
 * @brief 开机断电恢复。**必须在 app.Start() 之前调用**（见实现里的说明）。
 *
 * 判据与动作（2026-10-31 起只剩两条）：
 *   * 没有 ``pend_*`` → 什么都不做（99.99% 的开机走这条）；
 *   * 有 ``pend_*`` → 上次改 WiFi 没走完。**配置不需要恢复**：验证成功之前
 *     ``SsidManager`` 一个字都没改，所以要么是原样、要么已经是验证过的新配置，
 *     两种都能用；动它反而可能把设备弄坏（例如用户就是因为旧密码失效才来改，
 *     写回旧密码 = 永久失联）。只清标记 + 落一条
 *     ``ok=false / reason="reboot_during_trial"`` 的结果。
 *
 * 落终态的目的是让服务端不至于永远停在 ``delivered``（否则 ``wifi_req_pwd``
 * 明文会一直留着）。
 */
void elder_wifi_change_boot_recover(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_WIFI_CHANGE_H */
