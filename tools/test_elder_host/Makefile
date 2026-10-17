# elder 组件宿主单元测试 —— 断网提醒链路的「离线可用」证据
#
# 为什么需要它
# ------------
# reminder.c / elder_cmd.c 是本仓唯一能在**宿主机**上编译的 elder 源码：
# 它们的依赖（NVS / esp_timer / FreeRTOS 任务 / RGB）都能用桩替换，于是
# 「设备没有网络、只有本地时钟」这个最难在真机上复现的场景，可以在 CI 上
# 几秒钟跑完 —— 不烧板子、不用真的等 15 分钟。
#
# 覆盖的断言都对应医疗事故级的失效：
#   T1  提醒时间存本地（NVS）        —— 断网重启后提醒还在
#   T2  replace_all 不抹掉本地自建    —— 一次心跳同步不该让老人的服药提醒消失
#   T3  断网新建 + 断网确认 + 确认方式 —— 命令词分发器全链路
#   T4  时钟不可信时不触发            —— 响错时间比不提醒更危险
#
# 怎么跑
# ------
#   make            # 编译并运行
#   make clean
#
# 依赖：任意 C99 编译器 + make（CI 的 ubuntu-latest 自带 gcc）。
# ⚠️ 本机（C 盘满、无 IDF、无 gcc、WSL 被策略禁用）跑不了，
#    **请以 CI 结果为准**，不要只看这个文件就认为测试已通过。

CC      ?= cc
ELDER    = ../../firmware/components/elder
STUBS    = stubs

# -I stubs/include：提供 esp_err.h / esp_log.h / nvs.h / esp_timer.h / freertos/*
#   这些 ESP-IDF 头文件的宿主替身。
# 时间劫持在 stubs.c 里直接定义 time() 完成（不用 -Dtime=，那会连
# <time.h> 自己的声明一起改名，反而编不过）。
CFLAGS  ?= -std=c99 -Wall -Wextra -Wno-unused-parameter -O1 -g \
           -I$(STUBS)/include -I$(ELDER)/include

SRCS     = test_elder_host.c $(STUBS)/stubs.c \
           $(ELDER)/src/elder_cmd.c $(ELDER)/src/reminder.c
BUILD    = build

.PHONY: all run clean
all: run

$(BUILD)/test: $(SRCS) $(STUBS)/stubs.h
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -o $@ $(SRCS)

run: $(BUILD)/test
	@$(BUILD)/test

clean:
	@rm -rf $(BUILD)