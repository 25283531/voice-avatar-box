# elder 组件宿主单元测试 —— 断网提醒链路的「离线可用」证据
#
# 为什么需要它
# ------------
# reminder.c 是本仓唯一能在**宿主机**上编译的 elder 源码（elder_cmd.c 已随
# 2026-10-20 回归上游小智删除，离线命令词全删）：
# 它们的依赖（NVS / esp_timer / FreeRTOS 任务 / RGB）都能用桩替换，于是
# 「设备没有网络、只有本地时钟」这个最难在真机上复现的场景，可以在 CI 上
# 几秒钟跑完 —— 不烧板子、不用真的等 15 分钟。
#
# 覆盖的断言都对应医疗事故级的失效：
#   T1  提醒时间存本地（NVS）        —— 断网重启后提醒还在
#   T2  replace_all 不抹掉本地自建    —— 一次心跳同步不该让老人的服药提醒消失
#   T3  （已删除）断网新建 + 断网确认 —— 2026-10-20 离线命令词全删
#   T4  时钟不可信时不触发            —— 响错时间比不提醒更危险
#   T5  本地日边界不错判              —— 08:00 不该被当成「新的一天」
#   T6  跨天真的重置                  —— 第二天提醒仍要响（否则从此再不响）
#   T7  漏服 15 分钟升级 ESCALATED    —— 「人要介入」的唯一信号不能静默失效
#   T8  确认窗 30 秒超时回退          —— 长按后又不管了必须回到未确认态
#   T9  NVS CRC 校验                  —— 坏数据绝不能被当成有效提醒表
#   T10 上电首 tick 不做跨天重置      —— 否则每次上电都产生一次假「漏服」
#   T11 重启后不得二次响铃（T11-1~6）—— 见下
#   T12 到点后很久才开机 —— 不补响、不升级，判 SKIPPED（与 T11-6 反向成对）
# T5/T6 是一对：T5 守「同一天内别乱重置」，T6 守「跨天必须重置」。
# 只写 T5 会漏掉「重置逻辑恒不生效 → 提醒从第二天起永远不响」这个更坏的形态。
# T10 与 T5/T6 同源：日界守卫一旦失效，每次上电都会误判成跨天。
#
# T11 是「设备重启后当天已响过的提醒二次响铃并立刻升级告警」这一族，
# 分六个方向互为反向断言：
#   T11-1 当天已确认服药后重启   —— 绝不二次响铃、绝不升级、done 不被清
#   T11-2 未到点就重启           —— 不得提前响；到点后仍要正常响
#   T11-3 跨天重启 / 跨天重置后重启 —— 新的一天要响，且不能被旧 fired 位锁死
#   T11-4 老设备升级（旧格式 blob）—— 提醒/服药记录一条都不丢，CRC 不误判
#   T11-5 replace_all 之后        —— fired_today 位图必须跟着新下标重算
#   T11-6 到点时设备是关机的      —— 开机后必须补响（别修成「永不响」）
# T11-1 与 T11-6 是一对反向：前者守「响过的不再响」，后者守「没响过的要响」。
# 只写一边就会把修复做偏——要么重复服药（医疗事故），要么彻底漏服（同样事故）。
#
# T7~T11 都是靠**变异测试**逼出来的：把 reminder.c 对应分支改坏后重跑，
# 必须真的变红，否则这组断言就是装饰品。改动 reminder.c 后请重跑变异验证。
# 已知「两道闸门各自独立」：T11-1 里 state[] 恢复与 fired_today 位图都能单独
# 挡住二次响铃，因此**必须把两道都去掉**（等价于退回修复前）才会红 ——
# 这是刻意的双保险，但也意味着只破坏其中一道时 T11-1 不会红，
# 该行为由 M3（fired_today 不落盘）与 M4（跨天漏清位图）覆盖。
#
# 怎么跑
# ------
#   make            # 编译并运行
#   make clean
#
# 依赖：任意 C99 编译器 + make（CI 的 ubuntu-latest 自带 gcc）。
# 本工作流 server-ci.yml 的 elder-host-test job 每天在 Ubuntu 上真跑它，
# **以那个 job 的日志为准**（本地无 C 编译器时尤其要看 CI）。

CC      ?= cc
ELDER    = ../../firmware/components/elder
STUBS    = stubs

# -I $(STUBS)/include：提供 esp_err.h / esp_log.h / nvs.h / esp_timer.h /
#   freertos/* 这些 ESP-IDF 头文件的宿主替身。
#   注意 stubs.c / stubs.h 本身就在本目录（桩的**实现**），
#   只有「ESP-IDF 头的替身」才放在 stubs/include/ 下。
# 时间劫持在 stubs.c 里直接定义 time() 完成（不用 -Dtime=，那会连
# <time.h> 自己的声明一起改名，反而编不过）。
#
# -D_POSIX_C_SOURCE=200809L 不是可选项：reminder.c 用
# localtime_r()，测试用 setenv()+tzset() 固定时区，这三个都是 **POSIX**
# 而非 C99 标准函数。纯 -std=c99 下它们不会被声明，
# gcc 会报 implicit-function-declaration（C99 起不再是合法隐式声明），
# 于是整个测试**编译不过** —— 这是本 Makefile 首次真正被编译时暴露的问题。
CFLAGS  ?= -std=c99 -D_POSIX_C_SOURCE=200809L \
           -Wall -Wextra -Wno-unused-parameter -O1 -g \
           -I$(STUBS)/include -I$(ELDER)/include

SRCS     = test_elder_host.c stubs.c \
           $(ELDER)/src/reminder.c
BUILD    = build

.PHONY: all run clean
all: run

$(BUILD)/test: $(SRCS) stubs.h
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -o $@ $(SRCS)

run: $(BUILD)/test
	@$(BUILD)/test

clean:
	@rm -rf $(BUILD)