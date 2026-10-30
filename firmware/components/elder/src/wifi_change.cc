/*
 * wifi_change.cc —— 远程改 WiFi：先播报 → 只连目标的试连 → 失败零改动
 *                   （2026-10-31 重写；上一版见 git 历史 6ff6f53）
 *
 * ══ 这一版解决的两个现场问题（用户原话）════════════════════════════════
 *
 *   ①「切换 WiFi 时有活跃语音会话」—— 老人正说着话，设备突然断网，一句
 *      提示都没有。现在先播报「1 分钟内切换、切换过程中无法对话」，等 60 秒
 *      再动网络（见 announce_then_wait）。
 *
 *   ②「彻底解决两个 SSID 同时在信号范围内时的更换 WiFi 的有效性和准确性」
 *      —— 旧版试连会连上**旧的那个**，于是判失败回滚，改了等于没改。
 *
 * ② 的根因在组件里，**不在本文件**：
 *   `WifiStation::HandleScanResult()`（wifi_station.cc:145-187）把扫描结果按
 *   RSSI 从强到弱排好，再和 `SsidManager` 里的已知网络求交集，按这个顺序
 *   push 进 `connect_queue_`；`StartConnect()` 取队首。也就是说「先连哪个」
 *   完全由**信号强度**决定，与用户「我要换成哪个」无关 —— 旧网络信号更强时
 *   必然先连旧的，然后 GetSsid() != 目标 ⇒ 判失败 ⇒ 回滚。
 *   `connect_queue_` / `StartConnect()` 都是 private，`SsidManager` 的公开接口
 *   只有 Add / Remove / SetDefault / Clear —— 组件根本没有「只连某个 SSID」的
 *   能力（`SetDefaultSsid()` 只改列表顺序，对 RSSI 排序毫无影响）。
 *
 *   而 `xiaozhi/managed_components/78__esp-wifi-connect/` **不在 git 里**
 *   （被 .gitignore 掉，由 IDF 组件管理器按 dependencies.lock 拉取），改它下次
 *   构建就被覆盖；fork 一份（override_path）又会引入第二份真源。所以**不改组件**。
 *
 *   解法：**试连不经过 WifiStation**。`WifiStation::Stop()` 会把 WiFi 栈完整
 *   拆掉（注销事件处理器 → esp_wifi_stop/deinit → 销毁 netif），拆完之后这个
 *   栈就归我们独占，不存在「两套逻辑抢同一个 wifi 栈」（那正是上一版刻意避开
 *   裸 esp_wifi 的原因，现在通过 Stop() 先把对方请走，前提就成立了）。
 *   我们自己 init 一个 `nvs_enable=false` 的栈、**只写目标 SSID 一条配置**、
 *   连上后用 `esp_wifi_sta_get_ap_info()` 读**实际关联的 AP** 判成败：
 *     · 只连得上目标 ⇒ 不存在「连到旧的算失败」这种假阴性（有效性）；
 *     · 判据是实际关联的 AP，不是 WifiStation 记的那个字符串（准确性）；
 *     · 试连期间 `SsidManager` 一个字都没改 ⇒ 失败时**回滚是空操作**。
 *
 * ══ 状态机（全部在 elder_wifi_try 任务里跑，不碰心跳任务/音频通路）══════
 *
 *   ANNOUNCE ──► PERSIST_PEND ──► RESTART ──► TRIAL ─┬─► COMMIT ──► HANDBACK
 *        │             │(NVS 失败)                  └─► ROLLBACK ──► HANDBACK
 *        └─────────────┴──► ABORT（什么都没改）
 *
 *   ANNOUNCE      屏显 + 提示音 + TTS 播报「1 分钟内切换」，然后等 60 秒。
 *                 期间设备**完全正常**（心跳、对话都不受影响）。45 秒时补一句
 *                 「15 秒后开始切换」，免得老人被突然断网吓到。
 *   PERSIST_PEND  写 pend_ssid / pend_req_id 并 commit（**不写密码**，见下）。
 *                 出口：写成功 → RESTART；写失败 → ABORT（SsidManager 一个字没动）。
 *   RESTART       `WifiStation::Stop()` —— 交出 WiFi 栈。
 *   TRIAL         自建 netif + esp_wifi_init(nvs_enable=false) + set_config(只有目标)
 *                 + connect，等 GOT_IP，最长 30 秒。
 *                 出口：拿到 IP 且实际关联的 AP == 目标 → COMMIT
 *                       拿到 IP 但不是目标 → ROLLBACK "wrong_ap:<ssid>"
 *                       30 秒到点 → ROLLBACK "timeout"
 *   COMMIT        `SsidManager::AddSsid(目标, 密码)` —— **只有验证过才落盘**。
 *   ROLLBACK      什么都不用改（配置从头到尾没被碰过），直接 HANDBACK。
 *   HANDBACK      `WifiStation::Start()` 把网络交回去，然后确认它连的确实是目标。
 *                 它不是 —— 见下面「HANDBACK 为什么要多一步」。
 *
 * ══ HANDBACK 为什么要多一步 ════════════════════════════════════════════
 * `sta.Start()` 之后 WifiStation 会重新扫一遍、按 RSSI 排队。两个 SSID 都在
 * 范围里时它可能又挑回旧的那个 —— 那样「切换成功」就只写在库里、设备其实还在
 * 老网络上。所以交回之后还要看一眼：连上的不是目标，就
 * `esp_wifi_set_config(目标)` + `esp_wifi_disconnect()` —— WifiStation 自己的
 * DISCONNECTED 处理器会拿**当前配置**重连（也就是目标），队列为空时不会乱跳；
 * 目标连不上时它会照旧重试 5 次后重新扫描，自然回落旧网络（不会失联）。
 *
 * ⚠️ 已知残留（不影响联网，只是显示）：走这条"强制"分支时
 *    `WifiStation::ssid_` 仍是旧名字（它只在 StartConnect() 里赋值，是 private）。
 *    所以这里**不用** `GetSsid()` 判成败，也**主动补一条正确名字的屏显**去盖掉
 *    WifiStation 那条「已连接 <旧名字>」。
 *
 * ══ 为什么不把密码落 NVS（与上一版的关键差别）══════════════════════════
 * 上一版先把新配置 `AddSsid` 进去再试连，于是「只改密码、SSID 不变」时必须把
 * 旧密码备份到 `pend_old_pwd` 才能回滚。这一版**验证之前不碰配置**，密码只活在
 * RAM 里，`pend_pwd` / `pend_old_pwd` 都不再需要：
 *   · 断电保护从「靠备份把旧密码写回去」升级成「配置从来没被改过」——
 *     任何异常路径都天然回到旧配置（红线 1/2 的更强形式）；
 *   · 顺带消掉一个**真隐患**：如果用户就是因为旧密码失效才来改（换了路由器
 *     密码），开机恢复旧密码会把设备直接弄成永久失联。
 * 老版本可能已经在 NVS 里留了这两个 key，`clear_pending()` 会顺手擦掉。
 *
 * ══ 断电（红线 4）════════════════════════════════════════════════════
 * `pend_*` 是「一次试连正在进行」的唯一持久标记，开机由
 * `elder_wifi_change_boot_recover()` 收尾，挂在 `elder_bind_guide_start()` 里 ——
 * 那是全仓唯一在 `app.Start()`（⇒ `StartNetwork()`）**之前**执行的 elder 代码。
 * 因为配置从头到尾没被改过，收尾动作只有两件：清标记 + 给服务端一条终态
 * （否则它会永远停在 delivered，明文密码也一直留着）。
 */
#include "elder_wifi_change.h"

#include <cstdio>
#include <cstring>
#include <string>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "elder_ui.h"          /* 播报：屏显 + 提示音 + TTS（hooks 由 main 注入） */
#include "ssid_manager.h"
#include "wifi_station.h"

static const char *TAG = "elder.wifi";

/* NVS namespace 与 SsidManager 共用 "wifi"（不新开 namespace：多一个 namespace
 * 就多一份「配置到底在哪」的歧义）。key 全部 15 字符以内（NVS 硬限制）：
 *   ssid / password / ssid1..9 / password1..9 / force_ap / max_tx_power /
 *   remember_bssid 是别人的；pend_ssid / pend_req_id / wres_* 是本模块的。
 *
 * K_PEND_PWD / K_PEND_OLD 是**上一版**的 key，本版不再写，但 clear_pending()
 * 仍会擦掉它们 —— 升级上来的设备 NVS 里可能还躺着一份明文密码。 */
#define NVS_NS        "wifi"
#define K_PEND_SSID   "pend_ssid"
#define K_PEND_REQ    "pend_req_id"
#define K_LEGACY_PWD  "pend_pwd"        /* 仅擦除，不写 */
#define K_LEGACY_OLD  "pend_old_pwd"    /* 仅擦除，不写 */
#define K_RES_REQ     "wres_req"
#define K_RES_OK      "wres_ok"
#define K_RES_SSID    "wres_ssid"
#define K_RES_RSN     "wres_reason"

/* 播报后到真正动网络之间的等待。用户要求「提示用户设备即将在 1 分钟内进行
 * WiFi 切换」，所以这里就是字面上的 60 秒 —— 播报完 60 秒才 `Stop()`。 */
#define ELDER_WIFI_ANNOUNCE_MS (60 * 1000)
/* 第二次（收尾）提示的提前量：到点前 15 秒再提醒一次。 */
#define ELDER_WIFI_FINAL_REMIND_MS (15 * 1000)

/* 试连等待上限。**比上一版的 45 秒短**：这一版不做扫描，直接
 * `esp_wifi_set_config` + `connect`，正常 3~6 秒拿到 IP；30 秒已经覆盖
 * 「AP 慢 + DHCP 慢」的极端情况，再长只是让家属多等。
 * 刻意仍然不用 60 秒：`WifiBoard::StartNetwork()` 的开机兜底超时正好 60 秒，
 * 两个 60 秒撞在一起时日志里分不清是谁超时。 */
#define ELDER_WIFI_TRIAL_TIMEOUT_MS (30 * 1000)

/* 交回 WifiStation 后等它连上（它会重新扫描 + 重试 5 次，比试连慢）。 */
#define ELDER_WIFI_HANDBACK_TIMEOUT_MS (25 * 1000)
/* 强制改连目标后再等一次（配置已经写好，只等握手 + DHCP）。 */
#define ELDER_WIFI_FORCE_TIMEOUT_MS (20 * 1000)

/* 任务栈 8192：这里比上一版多了两条走 LVGL 的路径
 * （elder_ui_notice → Display::ShowNotification 要抢锁 + 重排整屏）。
 * elder_bridge.cc 里同类调用给的是 5120，这里再加上 esp_wifi/netif 的初始化
 * 调用，留到 8192 换「不会因为栈爆而随机重启」。 */
#define ELDER_WIFI_TASK_STACK 8192

/* 试连期间最多重连几次（对齐 WifiStation 的 MAX_RECONNECT_COUNT）。 */
#define TRIAL_MAX_RETRY 5

/* 事件位自己定义，不用 BIT0/BIT1 —— 那两个宏来自 soc/esp_bit_defs.h，
 * 本组件没有 REQUIRES soc，靠别处间接带进来是运气，不是契约。 */
#define TRIAL_BIT_GOT_IP  (1u << 0)
#define TRIAL_BIT_GAVE_UP (1u << 1)

static struct {
    TaskHandle_t    task;
    volatile bool   busy;   /* 试连进行中（含播报等待）。只由 sync 任务读、试连任务写 */
} s;

typedef struct {
    uint32_t req_id;
    char     ssid[ELDER_WIFI_SSID_MAX];
    char     pwd[ELDER_WIFI_PWD_MAX];
} trial_arg_t;

/* ---------------------------------------------------------------- NVS */

/* 写 pend_*（**不写密码**，理由见文件头）。 */
static bool persist_pending(uint32_t req_id, const char *ssid)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "NVS namespace %s 打不开，放弃本次试连", NVS_NS);
        return false;
    }

    esp_err_t e = nvs_set_str(h, K_PEND_SSID, ssid);
    if (e == ESP_OK) e = nvs_set_u32(h, K_PEND_REQ, req_id);
    /* 升级上来的设备可能还留着上一版的明文密码，这里顺手擦掉。 */
    if (e == ESP_OK) {
        nvs_erase_key(h, K_LEGACY_PWD);
        nvs_erase_key(h, K_LEGACY_OLD);
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
    nvs_erase_key(h, K_PEND_REQ);
    nvs_erase_key(h, K_LEGACY_PWD);
    nvs_erase_key(h, K_LEGACY_OLD);
    nvs_commit(h);
    nvs_close(h);
}

/* 读 pend_*。返回 false = 没有未完成的试连。 */
static bool load_pending(uint32_t *req_id, char *ssid, size_t ssid_len)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;

    uint32_t req = 0;
    bool found = (nvs_get_u32(h, K_PEND_REQ, &req) == ESP_OK);
    size_t n = ssid_len;
    if (found && nvs_get_str(h, K_PEND_SSID, ssid, &n) != ESP_OK) found = false;
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

/* -------------------------------------------------------- 播报（用户第 1 点） */

/*
 * 切换前播报。三层一起上，因为老人可能：看得见听不见 / 听得见看不见 /
 * 正在打盹：
 *   · 屏显（elder_ui_notice）—— 不发声，走 LVGL，老人和旁边的子女都能看到；
 *   · 提示音（EXCLAMATION）—— 立刻出声，把注意力拉过来；
 *   · TTS（elder_ui_speak）—— 在线且待机时才会念（见 elder_bridge.cc 的
 *     OnSpeak：非 idle 时刻意不插话，避免打断正在进行的对话 —— 这是全仓
 *     统一的行为，提醒播报也走同一条路）。
 *
 * ⚠️ 提示音和 TTS **不能同时发**：两路 PCM 抢同一个输出通道，实测是
 *    「音乐断续 + 语音只出半句」（见 elder_bridge.cc 里 kChimeMs 那段注释）。
 *    所以先响提示音，隔 1.2 秒再发 TTS 请求。
 */
static void announce_then_wait(const char *ssid)
{
    char line[160];

    snprintf(line, sizeof(line), "即将切换无线网络，切换过程中无法对话（约 1 分钟）");
    elder_ui_notice(line, "thinking");

    elder_ui_play_tone(ELDER_TONE_EXCLAMATION);
    vTaskDelay(pdMS_TO_TICKS(1200));

    /* 口令里带上目标 SSID：家属在旁边听着能确认"切的是不是我要的那个"。
     * SSID 已判 ≤31 字节，加固定话术后仍远小于 TTS 单次上限。
     * 分两个 snprintf 而不是拼两个 %s：括号必须紧跟在 SSID 后面，
     * 用「尾部补一个」的写法会把「」」放到整句末尾去。 */
    if (ssid && ssid[0]) {
        snprintf(line, sizeof(line),
                 "设备将在1分钟内切换到新的无线网络「%s」，切换过程中暂时无法对话，请不要关闭电源。",
                 ssid);
    } else {
        snprintf(line, sizeof(line),
                 "设备将在1分钟内切换无线网络，切换过程中暂时无法对话，请不要关闭电源。");
    }
    elder_ui_speak(line);

    ESP_LOGI(TAG, "已播报切换预告，%d 秒后开始动网络", ELDER_WIFI_ANNOUNCE_MS / 1000);

    /* 45 秒 + 15 秒两段，中间补一句收尾提醒。分成两段而不是一次 sleep 60 秒，
     * 是为了让「还有 15 秒」这句落在正确的位置上。 */
    vTaskDelay(pdMS_TO_TICKS(ELDER_WIFI_ANNOUNCE_MS - ELDER_WIFI_FINAL_REMIND_MS));
    elder_ui_notice("15 秒后切换无线网络，请稍等", "thinking");
    elder_ui_speak("15秒后开始切换无线网络。");
    vTaskDelay(pdMS_TO_TICKS(ELDER_WIFI_FINAL_REMIND_MS));
}

/* ------------------------------------------------------- 试连用的独立 WiFi 栈 */

typedef struct {
    esp_netif_t                *netif;
    EventGroupHandle_t          evt;
    esp_event_handler_instance_t h_wifi;
    esp_event_handler_instance_t h_ip;
    int                         attempts;
    bool                        inited;
    bool                        started;
} trial_net_t;

static void trial_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)data;
    trial_net_t *n = (trial_net_t *)arg;

    if (base == WIFI_EVENT) {
        if (id == WIFI_EVENT_STA_DISCONNECTED) {
            if (n->attempts < TRIAL_MAX_RETRY) {
                n->attempts++;
                /* 配置里只有目标 SSID，所以这里的重连永远是重连目标。 */
                esp_wifi_connect();
                ESP_LOGI(TAG, "试连重试 %d/%d", n->attempts, TRIAL_MAX_RETRY);
            } else {
                ESP_LOGW(TAG, "试连重试 %d 次仍未连上，放弃", TRIAL_MAX_RETRY);
                xEventGroupSetBits(n->evt, TRIAL_BIT_GAVE_UP);
            }
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(n->evt, TRIAL_BIT_GOT_IP);
    }
}

/*
 * 起一个**只有目标 SSID** 的临时 WiFi 栈。
 *
 * 调用前提：`WifiStation::Stop()` 已经执行（它注销了自己的事件处理器并
 * esp_wifi_deinit 了），所以这里不会和任何人抢栈。
 *
 * `cfg.nvs_enable = false` 是硬要求：否则驱动会把试连的 SSID/密码写进
 * "nvs.net80211"，试连失败也会留一份明文在 NVS 里 —— 本模块的全部安全
 * 前提是「验证之前配置一个字都不落盘」。
 */
static bool trial_start(trial_net_t *n, const char *ssid, const char *pwd)
{
    memset(n, 0, sizeof(*n));
    n->evt = xEventGroupCreate();
    if (n->evt == nullptr) {
        ESP_LOGE(TAG, "事件组创建失败");
        return false;
    }

    esp_err_t e = esp_netif_init();
    if (e != ESP_OK) { ESP_LOGE(TAG, "esp_netif_init: %s", esp_err_to_name(e)); return false; }

    n->netif = esp_netif_create_default_wifi_sta();
    if (n->netif == nullptr) { ESP_LOGE(TAG, "创建 STA netif 失败"); return false; }

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    cfg.nvs_enable = false;   /* 见函数头注释 */
    e = esp_wifi_init(&cfg);
    if (e != ESP_OK) { ESP_LOGE(TAG, "esp_wifi_init: %s", esp_err_to_name(e)); return false; }
    n->inited = true;

    e = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                            trial_event, n, &n->h_wifi);
    if (e != ESP_OK) { ESP_LOGE(TAG, "注册 WIFI_EVENT: %s", esp_err_to_name(e)); return false; }
    e = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                            trial_event, n, &n->h_ip);
    if (e != ESP_OK) { ESP_LOGE(TAG, "注册 IP_EVENT: %s", esp_err_to_name(e)); return false; }

    e = esp_wifi_set_mode(WIFI_MODE_STA);
    if (e != ESP_OK) { ESP_LOGE(TAG, "set_mode: %s", esp_err_to_name(e)); return false; }
    e = esp_wifi_start();
    if (e != ESP_OK) { ESP_LOGE(TAG, "esp_wifi_start: %s", esp_err_to_name(e)); return false; }
    n->started = true;

    /* **只写目标这一条**。不设 bssid/channel：同一个 SSID 可能有多台 AP
     * （mesh / 双频同名），让驱动自己挑最强的那台。
     * threshold.authmode 保持默认 0（WIFI_AUTH_OPEN）= 不设门槛，
     * 否则开放网络会被自己的门槛挡掉。 */
    wifi_config_t wc;
    memset(&wc, 0, sizeof(wc));
    strncpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, pwd, sizeof(wc.sta.password) - 1);
    e = esp_wifi_set_config(WIFI_IF_STA, &wc);
    if (e != ESP_OK) { ESP_LOGE(TAG, "set_config: %s", esp_err_to_name(e)); return false; }

    e = esp_wifi_connect();
    if (e != ESP_OK) { ESP_LOGE(TAG, "esp_wifi_connect: %s", esp_err_to_name(e)); return false; }
    return true;
}

static void trial_stop(trial_net_t *n)
{
    if (n->h_ip) {
        esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, n->h_ip);
        n->h_ip = nullptr;
    }
    if (n->h_wifi) {
        esp_event_handler_instance_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, n->h_wifi);
        n->h_wifi = nullptr;
    }
    if (n->started) {
        esp_wifi_stop();
        n->started = false;
    }
    if (n->inited) {
        esp_wifi_deinit();
        n->inited = false;
    }
    if (n->netif) {
        esp_netif_destroy(n->netif);
        n->netif = nullptr;
    }
    if (n->evt) {
        vEventGroupDelete(n->evt);
        n->evt = nullptr;
    }
}

/* 读**实际关联**的 AP 名字。这是全模块唯一的"连上了哪个"判据：
 * `WifiStation::GetSsid()` 只是它自己记的字符串，绕过它连网时不会更新。 */
static bool get_ap_ssid(char *out, size_t n)
{
    wifi_ap_record_t ap;
    memset(&ap, 0, sizeof(ap));
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
        if (n) out[0] = '\0';
        return false;
    }
    snprintf(out, n, "%s", (const char *)ap.ssid);
    return true;
}

/* 等 WifiStation 的连接位变成 want。它自己的事件处理器在管这个位。 */
static bool wait_bit(bool want, int timeout_ms)
{
    auto &sta = WifiStation::GetInstance();
    const int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (esp_timer_get_time() < deadline) {
        if (sta.IsConnected() == want) return true;
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    return sta.IsConnected() == want;
}

/* ---------------------------------------------------------------- 交回网络 */

/*
 * 把网络交回 WifiStation，并确保它连的是目标。
 *
 * 为什么不能只 `sta.Start()` 了事：Start() 之后它会重新扫描，`HandleScanResult`
 * 按 RSSI 排队 —— 两个 SSID 都在范围里、旧信号更强时，它又会连回旧的。
 * 那时库里写着「已切换」，设备其实还在老网络上（用户说的「准确性」）。
 *
 * 强制改连的做法：把 STA 的配置改成目标，再 `esp_wifi_disconnect()`。
 * WifiStation 的 DISCONNECTED 处理器会调 `esp_wifi_connect()`，用的是**当前
 * 配置**（目标）；此时 `connect_queue_` 在 GOT_IP 时已被清空，所以它不会
 * 插队到别的网络。目标连不上时它会重试 5 次后重新扫描 ⇒ 自然回落旧网络，
 * 设备不会失联。
 *
 * @return true = 设备确实在目标上
 */
static bool handback_ensure_target(const char *target, char *reason, size_t rlen)
{
    auto &sta = WifiStation::GetInstance();

    if (!wait_bit(true, ELDER_WIFI_HANDBACK_TIMEOUT_MS)) {
        snprintf(reason, rlen, "handback_timeout");
        ESP_LOGW(TAG, "交回网络后 %d 秒仍未连上任何网络", ELDER_WIFI_HANDBACK_TIMEOUT_MS / 1000);
        return false;
    }

    char cur[ELDER_WIFI_SSID_MAX] = "";
    if (get_ap_ssid(cur, sizeof(cur)) && strcmp(cur, target) == 0) {
        return true;
    }

    ESP_LOGW(TAG, "交回网络后连的是 [%s] 而不是目标 [%s]（两个 SSID 都在范围内、"
                  "旧信号更强）⇒ 强制改连目标", cur, target);

    wifi_config_t wc;
    memset(&wc, 0, sizeof(wc));
    strncpy((char *)wc.sta.ssid, target, sizeof(wc.sta.ssid) - 1);
    /* 密码：目标已经在 SsidManager 里（COMMIT 刚写的），从那里取，不在 RAM 里
     * 多留一份副本。 */
    for (const auto &it : SsidManager::GetInstance().GetSsidList()) {
        if (it.ssid == target) {
            strncpy((char *)wc.sta.password, it.password.c_str(),
                    sizeof(wc.sta.password) - 1);
            break;
        }
    }

    if (esp_wifi_set_config(WIFI_IF_STA, &wc) != ESP_OK) {
        snprintf(reason, rlen, "force_cfg_failed");
        return false;
    }
    /* 先等连接位掉下来，再等它重新置位 —— 否则 IsConnected() 可能还是刚才那个
     * true，一次都没等就误判成功。掉不下来说明 disconnect 没生效（极端情况），
     * 这时不能假装成功。 */
    esp_wifi_disconnect();
    if (!wait_bit(false, 5000)) {
        snprintf(reason, rlen, "force_stuck");
        ESP_LOGW(TAG, "强制改连后连接位 5 秒内没有掉下来，放弃本次判定");
        return false;
    }

    if (!wait_bit(true, ELDER_WIFI_FORCE_TIMEOUT_MS)) {
        snprintf(reason, rlen, "fell_back:%.20s", cur[0] ? cur : "none");
        return false;
    }
    if (!get_ap_ssid(cur, sizeof(cur)) || strcmp(cur, target) != 0) {
        snprintf(reason, rlen, "fell_back:%.20s", cur[0] ? cur : "none");
        ESP_LOGW(TAG, "强制改连后仍不是目标（当前 [%s]）", cur);
        return false;
    }

    /* WifiStation::ssid_ 此时还是旧名字（private，且只在 StartConnect 里赋值），
     * 它那条「已连接 <旧名字>」的屏显是错的 —— 补一条正确名字的盖掉。 */
    char line[96];
    snprintf(line, sizeof(line), "已连接到 %s", target);
    elder_ui_notice(line, "happy");
    return true;
}

/* ---------------------------------------------------------------- 状态机 */

static void finish(trial_arg_t *a, bool ok, bool committed, const char *reason)
{
    save_result(a->req_id, ok, a->ssid, reason);
    if (ok) {
        ESP_LOGI(TAG, "改 WiFi 成功：req=%u ssid=[%s]（新配置已落 NVS，pend_* 已清）",
                 (unsigned)a->req_id, a->ssid);
    } else {
        /* 两种失败要分清，否则排障时会被日志带偏：
         *   committed=false —— 试连就没过，SsidManager 一个字没动；
         *   committed=true  —— 试连过了、配置也写了，但交回 WifiStation 之后
         *                      设备没能停在目标上（多半是目标突然消失）。
         *                      这时新条目**故意保留**：它是验证过能用的，
         *                      删掉只会让下一次改 WiFi 重新输密码。 */
        ESP_LOGW(TAG, "改 WiFi 失败：req=%u ssid=[%s] reason=%s（新配置%s）",
                 (unsigned)a->req_id, a->ssid, reason,
                 committed ? "已写入，等设备下次扫描时自然生效" : "未写入，原配置未改动");
    }
    s.busy = false;
}

static void trial_run(trial_arg_t *a)
{
    auto &sta = WifiStation::GetInstance();
    auto &mgr = SsidManager::GetInstance();

    /* ---- PERSIST_PEND ----
     * 放在播报之前：播报要占 60 秒，这 60 秒里断电也要能被 boot_recover 收尾。 */
    if (!persist_pending(a->req_id, a->ssid)) {
        finish(a, false, false, "nvs_write_failed");
        return;
    }

    /* ---- ANNOUNCE（用户第 1 点：切换前 1 分钟预告）---- */
    announce_then_wait(a->ssid);

    /* ---- RESTART：交出 WiFi 栈 ---- */
    sta.Stop();

    /* ---- TRIAL：只连目标 ---- */
    trial_net_t net;
    bool ok = false;
    char reason[ELDER_WIFI_REASON_MAX] = "";
    char cur[ELDER_WIFI_SSID_MAX] = "";
    const int64_t t0 = esp_timer_get_time();

    if (!trial_start(&net, a->ssid, a->pwd)) {
        snprintf(reason, sizeof(reason), "wifi_init_failed");
        trial_stop(&net);
    } else {
        EventBits_t bits = xEventGroupWaitBits(
            net.evt, TRIAL_BIT_GOT_IP | TRIAL_BIT_GAVE_UP,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(ELDER_WIFI_TRIAL_TIMEOUT_MS));

        if (bits & TRIAL_BIT_GOT_IP) {
            /* 拿到 IP 还不够：判据必须是**实际关联的 AP == 目标**。
             * 只看"拿到 IP"会把"啥都没换成功"当成功，然后把新密码永久留在 NVS 里。 */
            if (get_ap_ssid(cur, sizeof(cur)) && strcmp(cur, a->ssid) == 0) {
                ok = true;
            } else {
                snprintf(reason, sizeof(reason), "wrong_ap:%.20s", cur[0] ? cur : "?");
                ESP_LOGW(TAG, "试连连上的是 [%s] 而不是目标 [%s] ⇒ 判定失败",
                         cur, a->ssid);
            }
        } else if (bits & TRIAL_BIT_GAVE_UP) {
            snprintf(reason, sizeof(reason), "auth_or_not_found");
        } else {
            snprintf(reason, sizeof(reason), "timeout");
            ESP_LOGW(TAG, "试连 %d 秒未拿到 IP（目标 [%s]）",
                     ELDER_WIFI_TRIAL_TIMEOUT_MS / 1000, a->ssid);
        }
        trial_stop(&net);
    }

    ESP_LOGI(TAG, "试连耗时 %d 毫秒，结果=%s", (int)((esp_timer_get_time() - t0) / 1000),
             ok ? "ok" : reason);

    /* ---- COMMIT / ROLLBACK ---- */
    bool committed = false;
    if (ok) {
        /* **只有验证过才落盘。** 这一步之后 `SsidManager` 才有目标这一条；
         * 在这之前它一个字都没被改过，所以失败路径不需要任何"恢复"。 */
        mgr.AddSsid(a->ssid, a->pwd);
        committed = true;
        sta.Start();
        ok = handback_ensure_target(a->ssid, reason, sizeof(reason));
    } else {
        /* 回滚 = 空操作：配置从头到尾没动过，直接把网络交回去。
         * 交回去要几秒，这期间没网；心跳会因此失败一次，下一轮自然恢复。 */
        sta.Start();
    }

    clear_pending();
    finish(a, ok, committed, reason);
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
    /* ⚠️ 上限是 **31** 而不是 32。虽然本版不再走
     * `WifiStation::StartConnect()` 的 `strcpy`，但 32 字节 SSID 在整条链路上
     * 还有别的坑（服务端入参、NVS 往返、日志格式化），而且现实中极罕见。
     * 直接拒掉并回报，比让用户对着一个永远连不上的网络反复重试强。 */
    if (sl > 31) {
        return reject(req_id, ssid, "ssid_too_long");
    }
    const char *pwd = password ? password : "";
    /* 63 是 WPA2 passphrase 的硬上限。写 63 而不是 ELDER_WIFI_PWD_MAX-1(64)：
     * 64 字符的密码会在 strncpy 里被**静默截断**成 63，表现是「密码明明对、
     * 永远认证失败」。 */
    if (strlen(pwd) > 63) {
        return reject(req_id, ssid, "pwd_too_long");
    }

    if (s.busy) {
        /* 服务端在 state==delivered 之后不会重复下发，所以这里只是防重放/防连点。
         * **刻意不落终态**：正在跑的那次会带自己的 req_id 回报；如果这是服务端
         * 的新请求（req 更大），它下一轮心跳会重发，等这一次跑完再受理。 */
        ESP_LOGW(TAG, "已有试连在进行（含 60 秒播报等待），本轮忽略 req=%u（ssid=[%s]）",
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
    ESP_LOGI(TAG, "受理改 WiFi：req=%u ssid=[%s] pwd=%s；先播报 %d 秒，再试连（最长 %d 秒）",
             (unsigned)req_id, a->ssid, pwd[0] ? "***" : "(空=开放网络)",
             ELDER_WIFI_ANNOUNCE_MS / 1000, ELDER_WIFI_TRIAL_TIMEOUT_MS / 1000);
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

    if (!load_pending(&req, ssid, sizeof(ssid))) {
        return;   /* 正常开机：没有未完成的试连 */
    }

    /*
     * 只有一件事要做：清标记 + 给服务端一条终态。
     *
     * **不需要"恢复旧配置"** —— 本版在验证成功之前不碰 `SsidManager`，所以
     * 断电时配置要么是原样（试连/播报阶段断的），要么已经是验证过的新配置
     * （COMMIT 之后断的）。两种都是"能用"的状态，动它反而会变坏：
     *   · 若用户就是因为旧密码失效才来改（换了路由器密码），把旧密码写回去
     *     会让设备**永久失联** —— 上一版靠 pend_old_pwd 恢复，正是这个隐患。
     *
     * 上一版这里会 `AddSsid(ssid, old_pwd)`，本版删除；`pend_old_pwd` 已不再写。
     */
    ESP_LOGW(TAG, "开机发现未完成的试连（req=%u ssid=[%s]）⇒ 配置未改动，只清标记",
             (unsigned)req, ssid);

    clear_pending();
    /* 给服务端一个终态，否则它会永远停在 delivered、明文密码也一直留着。 */
    save_result(req, false, ssid, "reboot_during_trial");
}
