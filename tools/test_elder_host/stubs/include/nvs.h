/* nvs.h —— 宿主测试桩（函数体在 stubs.c）。
 * 语义必须与固件一致：blob 写入后跨「重启」仍在，否则 T1 测的是假东西。 */
#ifndef STUB_NVS_H
#define STUB_NVS_H

#include <stddef.h>
#include <stdint.h>

typedef int nvs_handle_t;

#define NVS_READONLY  0
#define NVS_READWRITE 1

int  stub_nvs_open(const char *ns, int mode, nvs_handle_t *out);
int  stub_nvs_get_blob(nvs_handle_t h, const char *key, void *buf, size_t *len);
int  stub_nvs_set_blob(nvs_handle_t h, const char *key, const void *buf, size_t len);
int  stub_nvs_commit(nvs_handle_t h);
void stub_nvs_close(nvs_handle_t h);

/* 抹掉所有持久化数据，模拟「换了一块新 Flash」 */
void stub_nvs_reset(void);

#define nvs_open      stub_nvs_open
#define nvs_get_blob  stub_nvs_get_blob
#define nvs_set_blob  stub_nvs_set_blob
#define nvs_commit    stub_nvs_commit
#define nvs_close     stub_nvs_close

#endif