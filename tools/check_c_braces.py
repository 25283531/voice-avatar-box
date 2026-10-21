#!/usr/bin/env python3
"""C/C++ 花括号配平检查 —— 在没有本地 IDF 的前提下拦住"编译期才发现"的低级错误。

为什么需要它（2026-10-22 真事故）
--------------------------------
提交 8b18c9d 把提醒播报从 ``esp_timer`` 改成专用任务时，多留了一个 ``}``。
后果链：

  1. 本地所有冒烟脚本**全绿** —— 它们读文本做断言（"函数体里有没有
     PlaySound"），根本不编译 C++，多一个花括号对它们不可见；
  2. 固件编译在 CI 上跑，跑到 ``elder_bridge.cc:397`` 才报
     ``expected declaration before '}' token``；
  3. 于是"固件编译完成后写固件"这件事整条卡住，而用户等的是设备行为。

用户已拍板「固件一律 CI 编，本机不编」，所以本机必须有一道**不依赖工具链**
的机械闸门。花括号配平是其中性价比最高的一条：它不需要头文件、不需要
交叉编译器，几毫秒就能把「多/少一个 ``}``」这类错误挡在推送之前。

判据（只报两种错，不做风格检查）
--------------------------------
1. **深度变负**：遇到 ``}`` 时深度已是 0 ⇒ 一定多了右括号。
   报出该行号 —— 那是**第一个**出问题的位置，比"文件最后不配平"有用得多。
2. **结束时深度 != 0**：少了右括号（或多了左括号）。报出最后一个未闭合
   ``{`` 的行号（用栈记录）。

刻意不做的
----------
* 不检查 ``#if 0`` 里的死代码（预处理还没跑，理论上死分支里的括号会误报）；
* **默认只扫我们自己的组件** ``firmware/components/elder``（源树 + 注入副本）。
  上游代码里 ``#if LVGL_VERSION_MAJOR >= 8 ... #else ... #endif`` 这种
  "两套分支各带花括号"的写法，在词法层面必然配不平（实测
  boards/otto-robot/otto_icon_font.c 会误报 2 个 ``{``）。要扫全树请用
  ``--all``，此时只报"深度变负"这一种**确定错**，不报残留深度；
* 不解析原始字符串 ``R"(...)"``（本仓未使用；用到时加文件首行开关）。
* 不检查小/中括号 —— 那两类在本仓从未出过事，加了只是徒增误报面。

用法
----
    python tools/check_c_braces.py            # 检查默认目录（firmware + server 的 C）
    python tools/check_c_braces.py FILE...    # 只检查指定文件
退出码：0 = 全部配平；1 = 有文件不配平（逐条打印）。
"""
from __future__ import annotations

import sys
from pathlib import Path

#: 行内豁免：出现在同一行的注释里时跳过本文件（见文件头"刻意不做的"）
_OFF_MARKER = "brace-check: off"

_DEFAULT_ROOTS = ("firmware/components/elder", "firmware/xiaozhi/components/elder")
_ALL_ROOTS = ("firmware/components/elder", "firmware/xiaozhi/main")
_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp"}


def scan(src: str) -> tuple[list[str], int, list[int]]:
    """扫描源码，返回 (错误说明列表, 最终深度, 未闭合 '{' 的行号栈)。

    只做词法层面的剥离：``//`` 行注释、``/* */`` 块注释、``"..."`` 字符串、
    ``'...'`` 字符常量。剥离之后的花括号才计入深度 ——
    否则 ``ESP_LOGI(TAG, "}")`` 这种日志文本会被当成右括号。
    """
    errors: list[str] = []
    depth = 0
    stack: list[int] = []
    i = 0
    line = 1
    n = len(src)
    while i < n:
        ch = src[i]
        # ---- 注释 / 字符串 / 字符常量：整段跳过，里面的花括号不算 ----
        if ch == "/" and i + 1 < n:
            nxt = src[i + 1]
            if nxt == "/":
                j = src.find("\n", i)
                i = n if j < 0 else j          # 保留换行，让 line 计数正常走
                continue
            if nxt == "*":
                j = src.find("*/", i + 2)
                if j < 0:
                    errors.append(f"第 {line} 行：块注释未闭合")
                    return errors, depth, stack
                line += src.count("\n", i, j)
                i = j + 2
                continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            while i < n:
                if src[i] == "\\":             # 转义：连跳两个字符
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                if src[i] == "\n":             # 未闭合的字符串字面量
                    errors.append(f"第 {line} 行：{quote} 字符串未闭合")
                    return errors, depth, stack
                i += 1
            continue
        # ---- 花括号计数 ----
        if ch == "{":
            depth += 1
            stack.append(line)
        elif ch == "}":
            depth -= 1
            if depth < 0:
                # 第一个"多出来的右括号"就是最有用的信息，直接停
                errors.append(f"第 {line} 行：多余的 '}}'（此前已无 '{{' 可配）")
                return errors, depth, stack
            stack.pop()
        elif ch == "\n":
            line += 1
        i += 1
    return errors, depth, stack


def check_file(path: Path) -> list[str]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{path}: 读取失败 {exc}"]
    if _OFF_MARKER in src:
        return []
    errors, depth, stack = scan(src)
    if errors:
        return [f"{path}: {e}" for e in errors]
    if depth != 0:
        where = f"，最后一个未闭合的 '{{' 在第 {stack[-1]} 行" if stack else ""
        return [f"{path}: 花括号不配平（结束时仍多 {depth} 个 '{{'{where}）"]
    return []


def iter_targets(roots: list[str]) -> list[Path]:
    out: list[Path] = []
    for r in roots:
        p = Path(r)
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(q for q in p.rglob("*")
                              if q.is_file() and q.suffix in _SUFFIXES))
    return out


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--all"]
    only_negative = "--all" in argv[1:]
    files = ([Path(a) for a in args] or
             iter_targets(list(_ALL_ROOTS if only_negative else _DEFAULT_ROOTS)))
    problems: list[str] = []
    for f in files:
        found = check_file(f)
        if only_negative:
            # 全树扫描时只保留"确定错"：变了负的深度。残留深度在带
            # ``#if/#else`` 的上游文件里必然误报（见文件头）。
            found = [e for e in found if "多余的 '}'" in e]
        problems.extend(found)
    if problems:
        print(f"花括号配平检查：{len(problems)} 处问题")
        for p in problems:
            print("  " + p)
        return 1
    print(f"花括号配平检查：{len(files)} 个文件全部配平")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
