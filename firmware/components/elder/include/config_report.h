/*
 * config_report.h —— 开机配置自检（声明）
 *
 * 在 app.Start() 之前的最早时机调用一次，把编译期真正生效的关键配置
 * （目标芯片 / 唤醒词 / 显示风格 / 后端地址）打进串口，并对异常大声报错。
 *
 * 动机：sdkconfig 优先级高于 sdkconfig.defaults 且不覆盖不告警，
 * 「改了配置固件却没变」在设备上没有任何线索。这里把线索补上。
 */
#ifndef ELDER_CONFIG_REPORT_H
#define ELDER_CONFIG_REPORT_H

#ifdef __cplusplus
extern "C" {
#endif

/** 打印开机配置自检日志。纯读配置，无副作用，可重复调用。 */
void elder_config_report(void);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_CONFIG_REPORT_H */