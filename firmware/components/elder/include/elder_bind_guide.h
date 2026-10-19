#ifndef ELDER_BIND_GUIDE_H
#define ELDER_BIND_GUIDE_H

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 未绑定设备的引导（2026-10-19 用户实测提的需求）
 *
 * 现象：配网完成后，屏幕一直显示「云服务断开」标志 + 没有设备连接序列号，
 *       老人完全不知道下一步该干什么。要求此时语音播报一句
 *       「请使用手机 APP 或者登录网站进行设备绑定」。
 *
 * 为什么单独一个模块而不是塞进 elder_bridge.cc：
 *   elder_bridge_start() 是在 `app.Start()` **之后**由 elder_boot 任务调用的，
 *   而未绑定设备的 app.Start() 会在 CheckNewVersion() 的 while(true) 里
 *   **永久阻塞**（ShowActivationCode → Activate 连试 10 次 → 回到循环），
 *   永远不返回 ⇒ elder_boot 那个任务根本不会被创建/执行。
 *   所以引导必须由一个**在 app.Start() 之前就建好**的任务驱动，
 *   且只依赖 Board::GetInstance().GetDisplay()（Board 在构造函数里就绪）。
 */

/**
 * @brief 启动未绑定引导任务（幂等：重复调用只生效一次）
 *
 * 必须在 app.Start() **之前**调用。未绑定设备上 app.Start() 不返回，
 * 任何放在它之后的初始化代码都不会执行。
 */
esp_err_t elder_bind_guide_start(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_BIND_GUIDE_H */