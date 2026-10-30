/*
 * wifi_change.cc —— 远程改 WiFi 的试连 / 回滚状态机（2026-10-30）
 *
 * ── 为什么是 .cc ────────────────────────────────────────────────────────
 * 配置的唯一真源是 `SsidManager`（managed_components/78__esp-wifi-connect），
 * 连接/重试逻辑在 `WifiStation`，两个都是 C++ 类（std::string / std::vector），
 * C 里 include 不了。所以实现放这里，对外只暴露 elder_wifi_change.h 的纯 C 接口，
 * 由 sync.c（C）调用。
 *
 * ── 为什么走 AddSsid + Stop/Start，而不是裸 esp_wifi_set_config ─────────
 * WifiStation 自己挂了 WIFI_EVENT_STA_DISCONNECTED 处理器做重连和「换下一个 AP」，
 * 绕过它直接 set_config 会变成两套逻辑抢同一个 wifi 栈 —— 现场表现是
 * 「时好时坏、日志里两个 ssid 交替出现」，最难查的一类故障。走 AddSsid 则复用了
 * 已验证的链路：新 SSID 连不上 → 5 次重试 → connect_queue_ 换下一个（旧 SSID）→
 * 旧网络照常连上。我们只在旁边看「最终连上的到底是不是目标 SSID」。
 *
 * ⚠️ 但 connect_queue_ 的顺序是**按 RSSI 排的**，不是 SsidManager 的顺序
 *    （HandleScanResult 里 std::sort 过）。所以「新 SSID 与旧 SSID 同时在信号范围
 *    内」时，设备可能先连上旧的那个 —— 这时 GetSsid() != 目标，按失败处理并回滚。
 *    这是**保守**的选择（宁可这次不生效，也不让设备停在一个没验证过的网络上），
 *    代价是「从 MyHome 换到同一路由器的 MyHome-5G」这种两个都在范围里的迁移，
 *    如果旧信号更强会报一次失败。用户重试即可（第二次大概率仍失败，见回报里的说明）。
 *
 * ── 状态机（全部在 elder_wifi_try 任务里跑，不碰心跳任务/音频通路）──────
 *
 *   SNAPSHOT ──► PERSIST_PEND ──► RESTART ──► WAIT_CONNECT ─┬─► COMMIT
 *       │              │(NVS 失败)                          └─► ROLLBACK
 *       └──────────────┴──► ABORT（什么都没改）
 *
 *   SNAPSHOT      GetSsidList() 拷到堆上的 vector；记录目标 SSID 是否已存在
 *                 （存在 ⇒ 同名覆盖 ⇒ 必须备份旧密码）。
 *   PERSIST_PEND  写 pend_ssid / pend_pwd / pend_req_id / pend_old_pwd 并 commit。
 *                 出口：写成功 → RESTART；写失败 → ABORT（此时 SsidManager 一个字没动）。
 *   RESTART       WifiStation::Stop() → SsidManager::AddSsid(new) → WifiStation::Start()。
 *                 ⚠️ 顺序是**先 Stop 再改 SsidManager**：HandleScanResult 会
 *                 `auto ssid_list = GetSsidList()` 拷一份 vector，和 AddSsid 的
 *                 写并发就是数据竞争。Stop() 已注销事件处理器，改完再 Start() 就没有并发。
 *   WAIT_CONNECT  每秒查一次 WaitForConnected(1000)，最长 45 秒。
 *                 出口：连上且 GetSsid()==目标 → COMMIT
 *                       连上但不是目标（回落到旧 SSID）→ ROLLBACK "fell_back:<ssid>"
 *                       45 秒到点 → ROLLBACK "timeout"
 *   COMMIT        清 pend_* → 落 ok=true 结果。
 *   ROLLBACK      Stop() → 恢复配置 → Start() → 清 pend_* → 落 ok=false 结果。
 *   ABORT         不碰配置，落 ok=false 结果（NVS 写不进去时连结果也写不进，
 *                 只能在日志里喊，下一轮心跳拿不到结果 —— 服务端会停在 delivered，
 *                 但设备配置是好的，不影响使用）。
 *
 * ── 断电（红线 4）──────────────────────────────────────────────────────
 * pend_* 是「试连正在进行」的唯一持久标记。开机由
 * elder_wifi_change_boot_recover() 收尾，挂在 elder_bind_guide_start() 里 ——
 * 那是全仓唯一在 app.Start()（⇒ StartNetwork()）**之前**执行的 elder 代码，
 * 所以恢复动作能影响这一次开机到底连哪个网络。
 */
#include "elder_wifi_change.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "ssid_manager.h"
#include "wifi_station.h"

static const char *TAG = "elder.wifi";

/* NVS namespace 与 SsidManager 共用 "wifi"（不新开 namespace：多一个 namespace
 * 就多一份「配置到底在哪」的歧义）。key 全部 15 字符以内（NVS 硬限制）：
 *   ssid / password / ssid1..9 / password1..9 / force_ap / max_tx_power /
 *   remember_bssid 是别人的，pend_* / wres_* 是本模块的。 */
#define NVS_NS        "wifi"
#define K_PEND_SSID   "pend_ssid"
#define K_PEND_PWD    "pend_pwd"
#define K_PEND_REQ    "pend_req_id"
#define K_PEND_OLD    "pend_old_pwd"
#define K_RES_REQ     "wres_req"
#define K_RES_OK      "wres_ok"
#define K_RES_SSID    "wres_ssid"
#define K_RES_RSN     "wres_reason"

/* 试连等待上限。**刻意不用 60 秒**：WifiBoard::StartNetwork() 的兜底超时正好是
 * 60 秒（WaitForConnected(60*1000) → 失败就进配网 AP 模式），两个 60 秒撞在一起时
 * 日志里分不清「是试连超时还是开机兜底超时」，状态也难判。45 秒留出 15 秒余量，
 * 足够扫 3~4 轮（一轮扫描约 3~5 秒 + 5 次重连）。 */
#define ELDER_WIFI_TRIAL_TIMEOUT_MS (45 * 1000)

/* 任务栈 6144：与 elder_bind_guide 同级。这里没有 HTTP/TLS，但有 std::string 链
 * 和 snprintf；4096 大概够，但试连是低频动作，多 2KB 换「不会因为栈爆而重启」。 */
#define ELDER_WIFI_TASK_STACK 6144

static struct {
    TaskHandle_t    task;
    volatile bool   busy;   /* 试连进行中。只由 sync 任务读、试连任务写 */
} s;

typedef struct {
    uint32_t req_id;
    char     ssid[ELDER_WIFI_SSID_MAX];
    char     pwd[ELDER_WIFI_PWD_MAX];
} trial_arg_t;

/* ---------------------------------------------------------------- NVS */

/* 写 pend_*。old_pwd 非 NULL 表示「同名 SSID 被覆盖」，需要备份旧密码。 */
static bool persist_pending(uint32_t req_id, const char *ssid, const char *pwd,
                            const char *old_pwd)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "NVS namespace %s 打不开，放弃本次试连", NVS_NS);
        return false;
    }

    esp_err_t e = nvs_set_str(h, K_PEND_SSID, ssid);
    if (e == ESP_OK) e = nvs_set_str(h, K_PEND_PWD, pwd);
    if (e == ESP_OK) e = nvs_set_u32(h, K_PEND_REQ, req_id);
    if (e == ESP_OK) {
        if (old_pwd) {
            e = nvs_set_str(h, K_PEND_OLD, old_pwd);
        } else {
            e = nvs_erase_key(h, K_PEND_OLD);
            if (e == ESP_ERR_NVS_NOT_FOUND) e = ESP_OK;   /* 本来就没有，等于成功 */
        }
    }
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);

    if (e != ESP_OK) {
        ESP_LOGE(TAG, "pend_* 落盘失败（%s），放弃本次试连（配置未动）", esp_err_to_name(e));
        return false;
    }
    return true;
}

static void clear_pending(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_erase_key(h, K_PEND_SSID);
    nvs_erase_key(h, K_PEND_PWD);
    nvs_erase_key(h, K_PEND_REQ);
    nvs_erase_key(h, K_PEND_OLD);
    nvs_commit(h);
    nvs_close(h);
}

/* 读 pend_*。返回 false = 没有未完成的试连。 */
static bool load_pending(uint32_t *req_id, char *ssid, size_t ssid_len,
                         char *old_pwd, size_t old_len, bool *has_old)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;

    uint32_t req = 0;
    bool found = (nvs_get_u32(h, K_PEND_REQ, &req) == ESP_OK);
    size_t n = ssid_len;
    if (found && nvs_get_str(h, K_PEND_SSID, ssid, &n) != ESP_OK) found = false;

    *has_old = false;
    if (found) {
        size_t m = old_len;
        if (nvs_get_str(h, K_PEND_OLD, old_pwd, &m) == ESP_OK) *has_old = true;
    }
    nvs_close(h);

    if (!found) return false;
    *req_id = req;
    return true;
}

/* 落一条待回报的终态。写不进去只影响回报，不影响设备可用性。 */
static void save_result(uint32_t req_id, bool ok, const char *ssid, const char *reason)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "结果落盘失败（NVS 打不开）：req=%u 无法回报服务端",
                 (unsigned)req_id);
        return;
    }
    nvs_set_u32(h, K_RES_REQ, req_id);
    nvs_set_u8(h, K_RES_OK, ok ? 1 : 0);
    nvs_set_str(h, K_RES_SSID, ssid ? ssid : "");
    nvs_set_str(h, K_RES_RSN, reason ? reason : "");
    nvs_commit(h);
    nvs_close(h);
}

/* ---------------------------------------------------------- SsidManager */

static int find_index(const std::string &ssid)
{
    const auto &list = SsidManager::GetInstance().GetSsidList();
    for (size_t i = 0; i < list.size(); i++) {
        if (list[i].ssid == ssid) return (int)i;
    }
    return -1;
}

/*
 * 回滚：把 SsidManager 恢复成快照的样子。**不用 Clear() + 重写**——
 * Clear() 会先 nvs_erase 掉全部 10 条再逐条写回，中间断电就一条都不剩
 * （那才是真正的失联）。这里只做「撤销本次新增的那一条」这一个动作。
 */
static void rollback(const std::vector<SsidItem> &snap, const std::string &target,
                     const char *old_pwd)
{
    auto &mgr = SsidManager::GetInstance();

    if (old_pwd) {
        /* 同名覆盖：AddSsid 命中同名只改密码，不会新增条目，一步复原 */
        mgr.AddSsid(target, old_pwd);
        ESP_LOGW(TAG, "回滚：已把 [%s] 的密码恢复成试连前那一条", target.c_str());
        return;
    }

    /* 新增条目：删掉它，旧条目一个都没被碰过 */
    int idx = find_index(target);
    if (idx >= 0) {
        mgr.RemoveSsid(idx);
        ESP_LOGW(TAG, "回滚：已从列表移除新增的 [%s]", target.c_str());
    }

    /* 满 10 条时 AddSsid 会 pop_back 掉最后一条 —— 补回来。
     * 补回来的条目会插到队首（AddSsid 的既有语义），顺序与原来不同，但**内容**
     * 一条不少；顺序只影响扫描时的尝试次序，不影响可用性。 */
    for (int i = (int)snap.size() - 1; i >= 0; i--) {
        if (find_index(snap[i].ssid) < 0) {
            ESP_LOGW(TAG, "回滚：补回被挤掉的旧条目 [%s]", snap[i].ssid.c_str());
            mgr.AddSsid(snap[i].ssid, snap[i].password);
        }
    }
}

/* ---------------------------------------------------------------- 状态机 */

static void finish(trial_arg_t *a, bool ok, const char *reason)
{
    save_result(a->req_id, ok, a->ssid, reason);
    if (ok) {
        ESP_LOGI(TAG, "改 WiFi 成功：req=%u ssid=[%s]（新配置已落 NVS，pend_* 已清）",
                 (unsigned)a->req_id, a->ssid);
    } else {
        ESP_LOGW(TAG, "改 WiFi 失败并已回滚：req=%u ssid=[%s] reason=%s",
                 (unsigned)a->req_id, a->ssid, reason);
    }
    s.busy = false;
}

static void trial_run(trial_arg_t *a)
{
    auto &sta = WifiStation::GetInstance();
    auto &mgr = SsidManager::GetInstance();

    /* ---- SNAPSHOT ---- */
    std::vector<SsidItem> snap = mgr.GetSsidList();   /* 堆上的拷贝，不是栈数组 */
    bool same_ssid = false;
    std::string old_pwd;
    for (const auto &it : snap) {
        if (it.ssid == a->ssid) {
            same_ssid = true;
            old_pwd = it.password;
            break;
        }
    }

    /* ---- PERSIST_PEND ---- */
    if (!persist_pending(a->req_id, a->ssid, a->pwd,
                         same_ssid ? old_pwd.c_str() : nullptr)) {
        finish(a, false, "nvs_write_failed");
        return;
    }
    if (same_ssid) {
        /* 红线 3：这是「只改密码」场景唯一能救命的一条 —— AddSsid 会把这条的
         * 密码直接顶掉，旧密码从此只存在于 pend_old_pwd 和这份 RAM 快照里。 */
        ESP_LOGW(TAG, "目标 SSID 已存在同名条目 ⇒ AddSsid 将覆盖密码；"
                      "旧密码（%u 字符）已备份到 pend_old_pwd",
                 (unsigned)old_pwd.size());
    }

    /* ---- RESTART ---- */
    sta.Stop();
    mgr.AddSsid(a->ssid, a->pwd);
    sta.Start();

    /* ---- WAIT_CONNECT ---- */
    bool ok = false;
    char reason[ELDER_WIFI_REASON_MAX] = "";
    const int64_t t0 = esp_timer_get_time();
    const int64_t deadline = t0 + (int64_t)ELDER_WIFI_TRIAL_TIMEOUT_MS * 1000;

    while (esp_timer_get_time() < deadline) {
        if (!sta.WaitForConnected(1000)) continue;

        std::string cur = sta.GetSsid();
        if (cur == a->ssid) {
            ok = true;
        } else {
            /* 回落到旧 SSID 也是「连上了」—— 判据必须是**连上的 SSID 等于目标**，
             * 只看 IsConnected() 会把「啥都没改成功」当成成功，然后把新密码
             * 永久留在 NVS 里。 */
            snprintf(reason, sizeof(reason), "fell_back:%.20s", cur.c_str());
            ESP_LOGW(TAG, "试连连上的是 [%s] 而不是目标 [%s] ⇒ 判定失败",
                     cur.c_str(), a->ssid);
        }
        break;
    }
    if (!ok && reason[0] == '\0') {
        snprintf(reason, sizeof(reason), "timeout");
        ESP_LOGW(TAG, "试连 %d 秒未连上目标 [%s]",
                 ELDER_WIFI_TRIAL_TIMEOUT_MS / 1000, a->ssid);
    }

    /* ---- COMMIT / ROLLBACK ---- */
    if (!ok) {
        sta.Stop();
        rollback(snap, a->ssid, same_ssid ? old_pwd.c_str() : nullptr);
        sta.Start();
    }
    clear_pending();
    ESP_LOGI(TAG, "试连耗时 %d 毫秒", (int)((esp_timer_get_time() - t0) / 1000));
    finish(a, ok, reason);
}

static void trial_task(void *arg)
{
    trial_arg_t *a = (trial_arg_t *)arg;
    trial_run(a);
    heap_caps_free(a);
    vTaskDelete(NULL);
}

/* ---------------------------------------------------------------- 对外 */

/*
 * 直接拒掉一次下发，但仍然**落一条终态**。
 *
 * 为什么不干脆 return false 了事：服务端在 state==delivered 之后不会重发，
 * 如果设备什么都不回报，服务端会永远停在 delivered —— 那条明文密码也永远
 * 留在库里（契约要求「拿到终态回报后立刻清空」）。所以拒收也必须留痕。
 */
static bool reject(uint32_t req_id, const char *ssid, const char *why)
{
    char safe[ELDER_WIFI_SSID_MAX] = "";
    if (ssid) snprintf(safe, sizeof(safe), "%s", ssid);
    ESP_LOGW(TAG, "拒收改 WiFi：req=%u ssid=[%s] 原因=%s", (unsigned)req_id, safe, why);
    save_result(req_id, false, safe, why);
    return false;
}

bool elder_wifi_change_apply(uint32_t req_id, const char *ssid, const char *password)
{
    if (!ssid || !ssid[0]) {
        return reject(req_id, "", "empty_ssid");
    }
    size_t sl = strlen(ssid);
    /* ⚠️ 上限是 **31** 而不是 32：上游 WifiStation::StartConnect() 用
     * `strcpy((char*)wifi_config.sta.ssid, ssid.c_str())` 写进 `uint8_t ssid[32]`，
     * 32 字符的 SSID 会连 NUL 一起写 33 字节 —— 多出来的那个 0 正好落在
     * `password[0]` 上，把密码首字节清成 0。表现是「密码明明对，永远认证失败」，
     * 而且串口上一点线索都没有。这里直接拒掉并回报，比让用户对着一个
     * 永远连不上的网络反复重试强。32 字节 SSID 现实中极罕见。 */
    if (sl > 31) {
        return reject(req_id, ssid, "ssid_too_long");
    }
    const char *pwd = password ? password : "";
    if (strlen(pwd) > ELDER_WIFI_PWD_MAX - 1) {
        return reject(req_id, ssid, "pwd_too_long");
    }

    if (s.busy) {
        /* 服务端在 state==delivered 之后不会重复下发，所以这里只是防重放/防连点。
         * **刻意不落终态**：正在跑的那次会带自己的 req_id 回报；如果这是服务端
         * 的新请求（req 更大），它下一轮心跳会重发，等这一次跑完再受理。 */
        ESP_LOGW(TAG, "已有试连在进行，本轮忽略 req=%u（ssid=[%s]）",
                 (unsigned)req_id, ssid);
        return false;
    }

    trial_arg_t *a = (trial_arg_t *)heap_caps_malloc(sizeof(*a), MALLOC_CAP_8BIT);
    if (!a) {
        ESP_LOGE(TAG, "内存不足（%u 字节），无法试连", (unsigned)sizeof(*a));
        return reject(req_id, ssid, "no_mem");
    }
    memset(a, 0, sizeof(*a));
    a->req_id = req_id;
    memcpy(a->ssid, ssid, sl);                    /* 已判 ≤31，缓冲区 33 */
    snprintf(a->pwd, sizeof(a->pwd), "%s", pwd);

    s.busy = true;
    if (xTaskCreate(trial_task, "elder_wifi_try", ELDER_WIFI_TASK_STACK, a, 5, &s.task) != pdPASS) {
        s.busy = false;
        heap_caps_free(a);
        ESP_LOGE(TAG, "试连任务创建失败（栈 %d 字节）", ELDER_WIFI_TASK_STACK);
        return reject(req_id, ssid, "no_task");
    }

    /* 密码永远不进日志 */
    ESP_LOGI(TAG, "受理改 WiFi：req=%u ssid=[%s] pwd=%s，试连最长 %d 秒",
             (unsigned)req_id, a->ssid, pwd[0] ? "***" : "(空=开放网络)",
             ELDER_WIFI_TRIAL_TIMEOUT_MS / 1000);
    return true;
}

bool elder_wifi_change_busy(void)
{
    return s.busy;
}

bool elder_wifi_change_pending_result(elder_wifi_result_t *out)
{
    if (!out) return false;

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;

    uint32_t req = 0;
    if (nvs_get_u32(h, K_RES_REQ, &req) != ESP_OK) {
        nvs_close(h);
        return false;
    }

    memset(out, 0, sizeof(*out));
    out->req_id = req;

    uint8_t ok = 0;
    nvs_get_u8(h, K_RES_OK, &ok);
    out->ok = (ok != 0);

    /* nvs_get_str 在 key 缺失时不动缓冲区、在超长时返回 INVALID_LENGTH 也不动，
     * 两种情况都靠 memset 出来的空串兜底 —— ssid 为空时服务端只会记日志。 */
    size_t n = sizeof(out->ssid);
    nvs_get_str(h, K_RES_SSID, out->ssid, &n);
    n = sizeof(out->reason);
    nvs_get_str(h, K_RES_RSN, out->reason, &n);
    nvs_close(h);
    return true;
}

void elder_wifi_change_result_ack(uint32_t req_id)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;

    uint32_t cur = 0;
    if (nvs_get_u32(h, K_RES_REQ, &cur) == ESP_OK && cur == req_id) {
        nvs_erase_key(h, K_RES_REQ);
        nvs_erase_key(h, K_RES_OK);
        nvs_erase_key(h, K_RES_SSID);
        nvs_erase_key(h, K_RES_RSN);
        nvs_commit(h);
        ESP_LOGI(TAG, "wifi_result 已送达服务端（req=%u），本地清除", (unsigned)req_id);
    }
    nvs_close(h);
}

void elder_wifi_change_boot_recover(void)
{
    uint32_t req = 0;
    char ssid[ELDER_WIFI_SSID_MAX] = {0};
    char old_pwd[ELDER_WIFI_PWD_MAX] = {0};
    bool has_old = false;

    if (!load_pending(&req, ssid, sizeof(ssid), old_pwd, sizeof(old_pwd), &has_old)) {
        return;   /* 正常开机：没有未完成的试连 */
    }

    auto &mgr = SsidManager::GetInstance();
    int idx = find_index(std::string(ssid));

    if (idx < 0) {
        /* 红线 4 的字面要求：pend_* 在、SSID 不在 ⇒ 试连没走到 AddSsid（或已经
         * 回滚完），原配置完好，清标记、按原配置启动即可。 */
        ESP_LOGW(TAG, "开机发现未完成的试连（req=%u ssid=[%s]），但配置里没有它 ⇒ "
                      "原配置完好，只清标记", (unsigned)req, ssid);
    } else if (has_old) {
        /* 同名覆盖：旧密码已被新密码顶掉。写回去 —— 旧密码在请求发起时是能上网的
         * （否则设备根本收不到这条心跳），所以这一步一定能把设备带回已知可用的状态。
         * 这里**不猜**新密码对不对：宁可放弃这次改动，也不能让设备停在一个
         * 没验证过的网络上。 */
        ESP_LOGW(TAG, "开机发现未完成的试连（req=%u ssid=[%s]）且同名条目的密码被覆盖过 ⇒ "
                      "恢复旧密码后按原配置启动", (unsigned)req, ssid);
        mgr.AddSsid(ssid, old_pwd);
    } else {
        /* 新增条目：旧条目一条都没动，两条都留着 —— WifiStation 会按信号强弱尝试，
         * 新的连不上自然回落旧的，删掉反而可能删掉唯一能用的那一个。 */
        ESP_LOGW(TAG, "开机发现未完成的试连（req=%u ssid=[%s]），是追加的新条目 ⇒ "
                      "保留（旧配置都在，连不上会自动回落），只清标记", (unsigned)req, ssid);
    }

    clear_pending();
    /* 给服务端一个终态，否则它会永远停在 delivered、明文密码也一直留着。 */
    save_result(req, false, ssid, "reboot_during_trial");
}
