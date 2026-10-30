"""静态闸门：固件「远程改 WiFi」的三条安全不变量（wifi_change.cc）。

为什么要有这个脚本
------------------
这一版（2026-10-31）把试连改成**自己独占 WiFi 栈、只连目标 SSID**，全部安全性
都建立在「验证成功之前不碰 SsidManager」这一条上。这条不变量**没有任何运行时
表现**能被冒烟脚本看到：

  * 违反了它，设备照样能联网、照样回报成功 —— 只是失败时无法回滚；
  * 现场后果是「老人家里设备失联，必须上门」，而 CI 一路绿灯。

所以只能做静态判据。判据一律按**代码位**判（先剥注释再匹配、按函数体切分），
不做整文件子串匹配 —— 注释里提到 `AddSsid` 不该算数，把调用注释掉也不该算数。

守的九条
--------
W1  `mgr.AddSsid(` 必须出现在 `trial_start(` **之后**（验证之前不改配置）
W2  `AddSsid` 全文件只出现一次（不允许第二处提前写入）
W3  `trial_start()` 里必须 `nvs_enable = false`（否则驱动会把试连密码写 NVS）
W4  `trial_start()` 里 `wc.sta.ssid` 只赋一次，且赋的是函数参数 `ssid`
    （不能从 SsidManager 遍历写 —— 那就又变成"能连上旧的"了）
W5  判"连上了哪个"必须用 `esp_wifi_sta_get_ap_info`；全文件不得出现
    `sta.GetSsid()`（那是 WifiStation 自己记的字符串，绕过它连网时不会更新）
W6  `announce_then_wait(` 必须出现在 `sta.Stop()` 之前，且
    `ELDER_WIFI_ANNOUNCE_MS` ≥ 60000（用户要求「1 分钟内切换」的预告）
W7  `elder_wifi_change_boot_recover()` 里不得出现 `AddSsid`
    （恢复旧密码 = 用户就是因为旧密码失效才来改时，永久失联）
W8  全文件不得 `nvs_set_str(..., K_LEGACY_...)`（旧版的明文密码 key 只许擦除）
W9  `persist_pending()` 里不得出现 `pwd`（密码不落 NVS）

用法：
    python tools/smoke_wifi_trial.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "firmware" / "components" / "elder" / "src" / "wifi_change.cc"
HDR = ROOT / "firmware" / "components" / "elder" / "include" / "elder_wifi_change.h"

_passed: list[str] = []
_failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    (_passed if ok else _failed).append(name)
    return ok


def strip_c_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释，但**保留字符串/字符字面量里的内容**。

    为什么不能直接 re.sub(r'//.*', ...)：本文件的字符串里有中文全角标点，
    但别的文件里可能有 `"https://..."` 这种，粗暴替换会把字符串切坏。
    这里用一个小状态机，逐字符走。
    """
    out: list[str] = []
    i, n = 0, len(text)
    state = "code"          # code | line_comment | block_comment | str | chr
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if c == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if c == '"':
                state = "str"
            elif c == "'":
                state = "chr"
            out.append(c)
            i += 1
            continue
        if state == "line_comment":
            if c == "\n":
                state = "code"
                out.append(c)
            i += 1
            continue
        if state == "block_comment":
            if c == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
            # 保留换行，行号才对得上
            if c == "\n":
                out.append(c)
            i += 1
            continue
        # str / chr：原样保留，处理转义
        if c == "\\" and nxt:
            out.append(c)
            out.append(nxt)
            i += 2
            continue
        if (state == "str" and c == '"') or (state == "chr" and c == "'"):
            state = "code"
        out.append(c)
        i += 1
    return "".join(out)


def func_body(code: str, name: str) -> str | None:
    """取出函数体（`name(...) {` 到配对的 `}` 之间），取不到返回 None。

    按花括号配平切，而不是按正则贪婪匹配 —— 后者会一路吃到文件末尾，
    让"这个函数里没有 X"这种断言永远为真（假绿）。
    """
    m = re.search(r"\b" + re.escape(name) + r"\s*\([^;{}]*\)\s*\{", code)
    if not m:
        return None
    start = m.end() - 1          # 指向 '{'
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    return None


def pos(code: str, needle: str) -> int:
    """needle 在 code 里第一次出现的位置；没有则 -1。"""
    i = code.find(needle)
    return i


def run() -> int:
    if not SRC.exists():
        check(f"{SRC.relative_to(ROOT)} 存在", False, "文件不见了，判据无从谈起")
        return 1
    raw = SRC.read_text(encoding="utf-8")
    code = strip_c_comments(raw)

    print(f"[{SRC.relative_to(ROOT)}]")

    # ---- W1 / W2：验证之前不改配置 ----
    i_trial = pos(code, "trial_start(&net")
    i_add = pos(code, "mgr.AddSsid(")
    check("W1 ★ `mgr.AddSsid(` 出现在试连之后（验证通过才落盘）",
          i_trial >= 0 and i_add > i_trial,
          f"trial_start@{i_trial} AddSsid@{i_add}")
    check("W2 ★ 全文件只有一处 AddSsid（没有第二处提前写入）",
          code.count("AddSsid(") == 1, f"count={code.count('AddSsid(')}")

    # ---- W3 / W4：试连只连目标 ----
    body = func_body(code, "trial_start")
    if not check("W3 能取到 trial_start() 的函数体（判据前提）", body is not None):
        return 1
    assert body is not None
    check("W3 ★ trial_start() 里 cfg.nvs_enable = false（试连配置绝不落 NVS）",
          re.search(r"nvs_enable\s*=\s*false", body) is not None, "")
    assigns = re.findall(r"wc\.sta\.ssid\s*,\s*([A-Za-z_][A-Za-z0-9_]*)", body)
    check("W4 ★ trial_start() 只写一条 SSID 配置，且写的是函数参数 ssid",
          assigns == ["ssid"], f"assigns={assigns}")
    check("W4 trial_start() 里没有从 SsidManager 遍历取 ssid（那会连上旧的）",
          "GetSsidList" not in body, "")

    # ---- W5：判据是实际关联的 AP ----
    check("W5 ★ 用 esp_wifi_sta_get_ap_info 判「连上了哪个」",
          "esp_wifi_sta_get_ap_info" in code, "")
    check("W5 ★ 全文件不出现 GetSsid()（那是 WifiStation 自己记的字符串）",
          ".GetSsid()" not in code,
          f"count={code.count('.GetSsid()')}")

    # ---- W6：先播报、后断网 ----
    i_ann = pos(code, "announce_then_wait(a->ssid)")
    i_stop = pos(code, "sta.Stop()")
    check("W6 ★ announce_then_wait() 出现在 sta.Stop() 之前（先预告再动网络）",
          i_ann >= 0 and i_stop > i_ann, f"announce@{i_ann} Stop@{i_stop}")
    m = re.search(r"#define\s+ELDER_WIFI_ANNOUNCE_MS\s+\((\d+)\s*\*\s*(\d+)\)", raw)
    if check("W6 能解析 ELDER_WIFI_ANNOUNCE_MS（判据前提）", m is not None):
        assert m is not None
        ms = int(m.group(1)) * int(m.group(2))
        check("W6 ★ 预告时长 ≥ 60 秒（用户要求「1 分钟内切换」）",
              ms >= 60_000, f"{ms} ms")

    # ---- W7：开机恢复不得改配置 ----
    boot = func_body(code, "elder_wifi_change_boot_recover")
    if check("W7 能取到 elder_wifi_change_boot_recover() 的函数体", boot is not None):
        assert boot is not None
        check("W7 ★ 开机恢复里没有 AddSsid（不恢复旧密码 = 不会把设备写死）",
              "AddSsid" not in boot, "")

    # ---- W8：旧版的明文密码 key 只许擦除 ----
    check("W8 ★ 不往 NVS 写 K_LEGACY_*（旧版明文密码只许 nvs_erase_key）",
          re.search(r"nvs_set_str\s*\(\s*h\s*,\s*K_LEGACY_", code) is None, "")

    # ---- W9：密码不落 NVS ----
    pend = func_body(code, "persist_pending")
    if check("W9 能取到 persist_pending() 的函数体", pend is not None):
        assert pend is not None
        # 判据用**小写** `pwd` 这个标识符：`K_LEGACY_PWD` 是大写常量，只被
        # nvs_erase_key 用（擦旧值），不该算成"写密码"。
        check("W9 ★ persist_pending() 里没有 pwd 这个变量（密码只活在 RAM）",
              re.search(r"\bpwd\b", pend) is None, "")

    # ---- 头文件与实现的一致性（防止只改一头）----
    hdr = HDR.read_text(encoding="utf-8") if HDR.exists() else ""
    check("W10 头文件里不再宣称 pend_old_pwd 备份（文档不能描述旧机制）",
          "pend_old_pwd" not in hdr or "不再写" in hdr, "")

    print()
    print(f"==== {len(_passed)} passed, {len(_failed)} failed ====")
    for name in _failed:
        print(f"  FAILED: {name}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(run())
