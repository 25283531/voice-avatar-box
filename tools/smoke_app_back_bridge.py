"""安卓返回键桥一致性冒烟（静态扫描，不起服务、不占端口）。

用法：
    python tools/smoke_app_back_bridge.py

为什么要这个门禁
----------------
2026-10-30 用户报「手机 APP 的『创建我的角色』页面没有取消按钮，按安卓返回键
直接退出 APP」。

根因不在页面：本项目手机端**不是** DCloud 的 app-vue 原生运行时，而是自写的
Android WebView 薄壳（``android/app/src/main/java/com/elder/companion/MainActivity.kt``）
加载编译好的 H5。壳里的返回处理原本只有两行：

    if (web.canGoBack()) web.goBack() else super.onBackPressed()

tabBar 各页之间切换不产生 WebView history ⇒ ``canGoBack()`` 恒为 false
⇒ 弹层开着时按返回键，H5 连 popstate 都收不到，Activity 直接被 finish。
框架的 ``onBackPress`` 靠 popstate 触发，在这条链路上是**死代码**；
``plus.key.addEventListener('backbutton')`` 同样没有 ``plus`` 对象
（APK 产物里 ``plus.`` 出现 0 次）。

修法是在壳里 ``evaluateJavascript`` 主动询问页面，两侧靠一个**字符串协议**
握手：``window.__elderHandleBack()`` 返回 1 = 页面已消化，0 = 交回默认行为。

⚠️ 这正是最容易静默失效的形态：**两侧改名只改一边**时，页面永远返回 0，
症状原封不动退回「按返回键直接退出」，而且不报任何错、构建也全绿。
所以这里按「代码位」判，不整文件子串匹配：

B1. 壳的 ``onBackPressed()`` 必须先询问页面，不能只判 ``canGoBack()``；
B2. 壳里 ``BACK_PROBE_JS`` 拼出来的字符串，必须真的引用 ``window.<fn>``；
B3. 页面侧 ``installBackBridge()`` 挂上去的全局名必须与 B2 逐字相同；
B4. 页面侧 ``handleBack()`` 必须返回 1/0 两种取值（只返回 1 会让返回键彻底失灵）；
B5. **凡是画了 ``class="mask"`` 弹层的页面都必须注册 ``pushBackCloser``**
    —— 漏一个页面，那个页面的弹层就还是「按返回键退出应用」。
    B1~B4 全绿也挡不住这条，因为它不是协议问题，是**漏接线**。

⚠️ 判据一律先剥注释再匹配：本脚本守的这些名字，防回归注释天生会反复引用
（例如 MainActivity 里就写着 ``window.__elderHandleBack`` 的说明）。
不剥注释，注释自己就把断言踩绿/踩红。假绿比没有断言更坏。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTIVITY = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "elder" / "companion" / "MainActivity.kt"
GUARD = ROOT / "app" / "src" / "utils" / "back-guard.ts"
PAGES = ROOT / "app" / "src" / "pages"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} {name}")
    if not ok and detail:
        for line in detail.splitlines():
            print(f"      {line}")


def strip_c_comments(src: str) -> str:
    """剥掉 ``//`` 与 ``/* */`` 注释，**不动字符串字面量**（Kotlin / TS 通用）。

    为什么要尊重字符串：``BACK_PROBE_JS`` 与 ``__elderHandleBack`` 恰好
    **就是字符串内容**，粗暴按行删 ``//`` 会把它们一起切掉。
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        # Kotlin 三引号字符串
        if src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(src[i:j])
            i = j
            continue
        if c in "\"'":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def kotlin_function_body(code: str, signature: str) -> str | None:
    """取 ``signature`` 之后第一对花括号内的内容（按括号配平，跳过字符串）。"""
    start = code.find(signature)
    if start < 0:
        return None
    i = code.find("{", start + len(signature))
    if i < 0:
        return None
    depth, n = 0, len(code)
    quote: str | None = None
    while i < n:
        c = code[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
        i += 1
    return None


def probe_function_name(code: str) -> str | None:
    """从 ``BACK_PROBE_JS = "..." + "..."`` 拼出完整 JS，再取 ``window.<fn>``。"""
    m = re.search(r"BACK_PROBE_JS\s*=", code)
    if not m:
        return None
    tail = code[m.end():m.end() + 800]
    # 只取到表达式结束（下一个成员声明/空行后的 val/private/fun/}）
    end = re.search(r"\n\s*(?:private|public|internal|val|var|fun|\}|companion)", tail)
    expr = tail[:end.start()] if end else tail
    lits = re.findall(r'"((?:[^"\\]|\\.)*)"', expr)
    if not lits:
        return None
    js = "".join(lits)
    mm = re.search(r"window\.(\w+)", js)
    return mm.group(1) if mm else None


def guard_installed_name(code: str) -> str | None:
    """``installBackBridge()`` 里 ``w.<name> = ...`` 的 name。"""
    body = None
    m = re.search(r"function\s+installBackBridge\s*\(", code)
    if m:
        body = kotlin_function_body(code, "function installBackBridge")
    if not body:
        return None
    mm = re.search(r"\.(\w+)\s*=\s*handleBack", body)
    return mm.group(1) if mm else None


def main() -> int:
    print("=" * 64)
    print("安卓返回键桥一致性（壳 onBackPressed ↔ 页面 __elderHandleBack）")
    print("=" * 64)

    missing = [p for p in (ACTIVITY, GUARD) if not p.exists()]
    if missing:
        print(f"[fatal] 找不到文件: {[str(m) for m in missing]}")
        return 1

    kt = strip_c_comments(ACTIVITY.read_text(encoding="utf-8"))
    ts = strip_c_comments(GUARD.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- B1
    body = kotlin_function_body(kt, "override fun onBackPressed()")
    if body is None:
        check("B1 壳的 onBackPressed() 存在且先询问页面", False,
              "MainActivity.kt 里找不到 `override fun onBackPressed()`。"
              "若签名被改（例如换成 onBackPressedDispatcher），本门禁的判据也要跟着改，"
              "不要直接删断言。")
    else:
        ok = "evaluateJavascript" in body and "BACK_PROBE_JS" in body
        check("B1 壳的 onBackPressed() 先 evaluateJavascript 询问页面", ok,
              "onBackPressed() 里没有走 BACK_PROBE_JS。只判 canGoBack() 的话，"
              "tabBar 页 history 为空 ⇒ 弹层开着按返回键直接退出应用（2026-10-30 事故）。")

    # ---------------------------------------------------------------- B2
    probe = probe_function_name(kt)
    check("B2 壳里 BACK_PROBE_JS 真的引用 window.<fn>", probe is not None,
          "从 BACK_PROBE_JS 的字符串字面量里解析不出 `window.<name>`。"
          "注意字符串被拆成多段 `+` 拼接，解析器按字面量拼接后再匹配。")

    # ---------------------------------------------------------------- B3
    installed = guard_installed_name(ts)
    check("B3 页面侧 installBackBridge() 挂上了全局名", installed is not None,
          "back-guard.ts 的 installBackBridge() 里找不到 `.<name> = handleBack`。")

    if probe and installed:
        check("B4 两侧全局名逐字一致", probe == installed,
              f"壳里问的是 window.{probe}，页面挂的是 window.{installed}。"
              "两侧改名只改一边 ⇒ 页面永远返回 0 ⇒ 症状原封不动退回"
              "「按返回键直接退出应用」，而且不报错、构建全绿。")

    # ---------------------------------------------------------------- B5
    handle = kotlin_function_body(ts, "function handleBack()")
    if handle is None:
        check("B5 页面侧 handleBack() 存在", False, "back-guard.ts 里找不到 handleBack()。")
    else:
        has1 = re.search(r"return\s+1\b", handle) is not None
        has0 = re.search(r"return\s+0\b", handle) is not None
        check("B5 handleBack() 同时返回 1 和 0", has1 and has0,
              f"返回 1 出现={has1}，返回 0 出现={has0}。"
              "只返回 1 ⇒ 返回键彻底失灵（永远退不出）；只返回 0 ⇒ 桥形同虚设。")

    # ---------------------------------------------------------------- B6 漏接线
    if not PAGES.exists():
        check("B6 页面目录存在", False, f"{PAGES} 不存在，确认路径")
    else:
        masked: list[Path] = []
        unregistered: list[Path] = []
        for p in sorted(PAGES.rglob("*.vue")):
            raw = p.read_text(encoding="utf-8")
            if 'class="mask"' not in raw:
                continue
            masked.append(p)
            # ⚠️ 只扫 <script> 段再剥注释，**不整文件子串匹配**：
            #   `pushBackCloser` 还留在 import 行里，注释掉调用照样命中
            #   —— 第一版就是这么假绿的（2026-10-30 实测：把
            #   `pushBackCloser(closeTopOverlay)` 整行注释掉，B6 仍全绿）。
            #   所以必须判「有调用」（名字后面跟左括号），不是判「出现过」。
            m = re.search(r"<script[^>]*>(.*?)</script>", raw, re.S)
            code = strip_c_comments(m.group(1)) if m else ""
            called = (re.search(r"\bpushBackCloser\s*\(", code) is not None
                      and re.search(r"\binstallBackBridge\s*\(", code) is not None)
            if not called:
                unregistered.append(p)
        check("B6 所有画了 class=\"mask\" 弹层的页面都接了返回键桥", not unregistered,
              "这些页面有自绘弹层但没**调用** pushBackCloser(...) / installBackBridge()，"
              "它们的弹层开着时按返回键仍会直接退出应用：\n"
              + "\n".join("  " + str(p.relative_to(ROOT)) for p in unregistered))
        print(f"      （扫描到 {len(masked)} 个含 mask 弹层的页面）")

    passed = sum(1 for _n, ok, _d in results if ok)
    failed = [n for n, ok, _d in results if not ok]
    print("\n" + "=" * 64)
    print(f"共 {len(results)} 条断言：通过 {passed}，失败 {len(failed)}")
    for name in failed:
        print(f"  ✗ {name}")
    print("=" * 64)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
