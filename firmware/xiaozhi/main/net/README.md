# net/ —— WiFi 侧网络栈（从 78/esp-ml307 拆出）

## 为什么在这里

上游组件 `78/esp-ml307` 把两件事放在同一个包里，19 个源文件一起编译：

| 类别 | 文件 | 本板是否需要 |
|------|------|--------------|
| ESP 原生网络栈 | `src/esp/esp_network.cc` / `esp_tcp` / `esp_ssl` / `esp_udp` / `esp_mqtt`、`src/http_client.cc`、`src/web_socket.cc` | **需要**（WiFi 的 HTTP / WS / TLS 全靠它） |
| Cat.1 4G 模组 | `src/at_uart.cc`、`src/at_modem.cc`、`src/ec801e/*`（5 个）、`src/ml307/*`（6 个） | **不需要**（本板是 WiFi 版，没有 4G 模组） |

关键在于 `main` 是用 `WHOLE_ARCHIVE` 注册的：

```cmake
idf_component_register(SRCS ${SOURCES} ... WHOLE_ARCHIVE)
```

WHOLE_ARCHIVE 意味着 main 里列出的每个 `.cc` 都**整个**进固件，
`-Wl,--gc-sections` 回收不掉。所以「引用了但没调用」的 4G 代码
不是"零成本放着"，而是实打实占着 flash。

## 做了什么

1. 把上表左列的 7 个源文件 + 8 个头文件**逐字**复制到这里；
2. 从 `main/idf_component.yml` 删掉 `78/esp-ml307` 依赖；
3. `main/CMakeLists.txt` 里把本目录加进 `SOURCES` 与 `INCLUDE_DIRS`，
   并把 `boards/common/{ml307,dual_network}_board.cc` 从 GLOB 结果里排除。

文件内容与上游 **MIT 许可的原版一致**，没有改任何一行逻辑 ——
只改了所在目录。上游：https://github.com/78/esp-ml307
（版本 3.5.3，许可见上游 LICENSE，MIT）。

## 改这里之前

* `esp_ssl.cc` 里挂着证书 bundle（`esp_crt_bundle_attach`）。删了或改错，
  HTTPS 会以 `No server verification option set` 建连失败 —— 表现为
  「设备一直离线」，日志却只说连不上。这一点踩过一次，别动。
* 不要把它改回组件依赖。真想恢复 4G，去上游拿完整包、把 `ml307_board.cc`
  和解排除掉的那两条一起恢复。
