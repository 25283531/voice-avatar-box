#ifndef ELDER_WIFI_CHANGE_H
#define ELDER_WIFI_CHANGE_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 已配网设备「远程改 WiFi」—— 试连 + 失败回滚（2026-10-30）
 *
 * 需求：服务端心跳下发 ``{"wifi_change":{"req_id":3,"ssid":"...","password":"..."}}``，
 * 设备试连新网络；连上（且**连上的就是目标 SSID**）才保留新配置，否则把配置
 * 恢复成试连之前的样子，下一次心跳回报结果。
 *
 * ── 安全红线（违反任何一条 = 老人设备失联 = 必须上门）────────────────
 * 1. **试连期间原配置一个字都不改**。旧条目留在 ``SsidManager``（NVS namespace
 *    ``"wifi"``）里不动；新配置先落 ``pend_*`` 独立 key 当临时区。
 * 2. 任何异常路径都必须能回到旧配置。宁可放弃这次改动。
 * 3. ⚠️ ``SsidManager::AddSsid()`` 对**同名 SSID 是覆盖密码**。所以「只改密码、
 *    SSID 不变」这个最常见场景，试连前必须把旧密码记下来（``pend_old_pwd``），
 *    失败时用它写回去 —— 否则旧密码被新密码顶掉、又连不上，就永久失联。
 * 4. 断电保护：``pend_*`` 带 req_id，开机时若 ``pend_*`` 还在，说明上次试连
 *    没走完（见 ``elder_wifi_change_boot_recover``）。
 *
 * ── 为什么单独一个文件、且跑在独立任务里 ─────────────────────────────
 * 一次试连最长 45 秒（``ELDER_WIFI_TRIAL_TIMEOUT_MS``），期间 WiFi 是被 Stop()
 * 掉的。绝不能塞进心跳任务（会卡住整个同步）或 MCP 工具的 app.Schedule（会卡住
 * 对话）。所以 ``elder_wifi_change_apply()`` 只做参数校验 + 起任务，立刻返回。
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
 * 校验参数 → 起 ``elder_wifi_try`` 任务 → 返回。真正的试连/回滚/回报都在那个
 * 任务里完成，结果落 NVS，由下一次 ``/api/device/sync`` 的请求体带回。
 *
 * @param req_id   服务端请求序号（回传用）
 * @param ssid     目标 SSID，非空且 **≤31 字节**（802.11 上限是 32，但上游
 *                 WifiStation::StartConnect() 的 strcpy 会多写一个 NUL 踩掉
 *                 密码首字节，所以 32 字节一律拒收，见实现里的注释）
 * @param password 目标密码，≤63 字符（开放网络传 ""）
 * @return true  = 已受理（不代表成功）
 *         false = 参数非法 / 已有试连在跑 / 任务创建失败。前三种会落一条
 *                 ``ok=false`` 的终态，由下一次心跳回报（服务端才不会永远停在
 *                 delivered）；「已有试连在跑」不落终态，等服务端下一轮重发。
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
 * 判据与动作：
 *   * 没有 ``pend_*`` → 什么都不做（99.99% 的开机走这条）；
 *   * 有 ``pend_*`` 但 ``SsidManager`` 里没有该 SSID → 试连还没走到落盘那一步，
 *     原配置完好，只清标记；
 *   * 有 ``pend_*``、SSID 在、且 ``pend_old_pwd`` 也在 → 同名覆盖过密码，
 *     **把旧密码写回去**（旧密码在请求发起时是能上网的，否则设备收不到心跳）；
 *   * 有 ``pend_*``、SSID 在、没有 ``pend_old_pwd`` → 新条目是追加的，旧条目
 *     一个都没动，保留两条（连不上会自动回落），只清标记。
 *
 * 无论哪条分支，都会落一条 ``ok=false / reason="reboot_during_trial"`` 的结果，
 * 让服务端不至于永远停在 ``delivered``（否则 ``wifi_req_pwd`` 明文会一直留着）。
 */
void elder_wifi_change_boot_recover(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_WIFI_CHANGE_H */
