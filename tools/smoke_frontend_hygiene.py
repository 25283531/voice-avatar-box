"""前端模板卫生冒烟（静态扫描，不起服务、不占端口）。

用法：
    python tools/smoke_frontend_hygiene.py

覆盖
----
L1. **没有把方法当值插值**（本脚本存在的核心理由）：
    ``{{apiKeyLabel}}`` 这类**不带括号**的裸标识符插值，如果标识符定义在
    ``methods:`` 里，Vue 3 会把方法 ``bind(publicThis)``，而 bound 函数的
    ``toString()`` 在所有引擎上都恰好是 ``"function () { [native code] }"``
    —— 页面上就会显示那串乱码。2026-10-18 线上事故就是这么来的（用户截图
    里的 API Key 标签；``apiKeyLabel`` 当时确实定义在 methods 段里）。
    computed/data 的插值是安全的；方法要么搬进 computed，要么写成
    ``{{apiKeyHint()}}`` 显式调用。
L2. **模板里没有孤儿引用**：``@click="xxx"`` / ``v-model="xxx"`` /
    ``{{xxx}}`` / ``v-if="xxx"`` 里出现的**根标识符**，必须在
    ``data()`` / ``computed:`` / ``methods:`` 里有定义。孤儿引用在 Vue 3 里
    **不报错**，只是渲染成空字符串或点击没反应 —— 属于"看着像功能在，
    其实没了"的静默失效，正是最难发现的那类。
L3. **data() 里没有重复键**：同一行里写两遍同名键，后者静默覆盖前者，
    而两处往往隔得很远（多人改同一文件时高发）。
L4. **卡片里的变量属于自己那张卡**：扫描 ``<template>`` 中每个 ``v-if="x"``
    守卫所辖的区域，断言区域内不出现**别的卡片的同名语义变量**。
    2026-10-19 线上事故就是这么来的：邮件测试卡片复用了 AI 配置弹窗的
    ``testResult``（而不是自己的 ``mailTestResult``），点「发送测试邮件」
    后 ``testResult`` 仍是 ``null`` ⇒ ``null.ok`` ⇒ Vue 渲染抛异常 ⇒
    **整个 SPA 白屏**，而控制台只有一行 ``TypeError``。
    L1/L2/L3 全部抓不到它：``testResult`` 在 data() 里有定义（不孤儿），
    也不是重复键，插值带三元表达式（不是裸方法名）。它属于「用错了但合法」
    ——**只有按区域归属比对才能发现**。

为什么不起服务：这几类缺陷全是**静态可判**的，起服务只会让 CI 多等几十秒，
却一条断言都多不了。端口表（tools/smoke_ports.py）因此不收本脚本。

⚠️ 扫描器的三个坑（第一版全踩过，改法写在这里免得再踩）：
   1. **HTML 注释必须先剥掉** —— 注释里常写着示例代码（``{{apiKeyLabel}}``），
      不剥就会把示例当成真实引用，报出不存在的孤儿。
   2. **字符串字面量必须跳过** —— ``:class="tab==='home'"`` 里的 ``'home'``
      不是标识符；不跳的话每个三元表达式都会造出一堆假孤儿。
   3. **只取成员表达式的根** —— ``d.api_key`` 里 ``api_key`` 是属性名，
      ``d`` 才是引用；全抓的话每个字段名都会变成假孤儿。对象字面量的键
      （``:style="{height:...}"`` 里的 ``height``）同理，按花括号深度跳过。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
INDEX = SERVER / "app" / "static" / "index.html"
USER = SERVER / "app" / "static" / "user.html"

results: list[tuple[str, bool, str]] = []

#: JS 关键字与语言内置，不算"未定义"
BUILTINS = {
    "true", "false", "null", "undefined", "new", "typeof", "instanceof",
    "in", "of", "this", "delete", "void", "if", "else", "return", "const",
    "let", "var", "function", "async", "await", "for", "while", "switch",
    "case", "default", "break", "continue", "try", "catch", "finally",
    "throw", "do",
    "Math", "String", "Number", "Object", "Array", "JSON", "Date",
    "parseInt", "parseFloat", "isNaN", "Boolean", "Infinity", "NaN",
}
#: 模板局部变量与 Vue 特殊对象，不是 app 成员
LOCAL_VARS = {
    "$event", "$refs", "index", "key", "value", "name", "id", "label",
}


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"\n         -> {detail}" if detail else ""))
    return bool(ok)


def parse_app(script: str) -> tuple[set[str], set[str], set[str], str,
                                    list[tuple[str, int]]]:
    """从 createApp({...}) 源码里取 data / computed / methods 顶层键 + data 段原文。

    用正则而不是 exec：exec 会真的跑一遍 Vue 应用代码（碰 localStorage、
    fetch），在这个纯静态脚本里既没必要也不安全。
    """
    dm = re.search(r"data\(\)\s*\{\s*return\s*\{(.*?)\n    \};", script, re.S)
    data_src = dm.group(1) if dm else ""
    # data() 里常有多个键写在同一行（devFilter: '', devBusy: false, ...），
    # 且混着嵌套对象（loginForm/tabs/...）—— 用深度感知的扫描只取第一层。
    data_pairs = top_level_keys(data_src)
    data_keys = {name for name, _ln in data_pairs}
    computed_keys: set[str] = set()
    methods_keys: set[str] = set()
    for sec, sink in (("computed", computed_keys), ("methods", methods_keys)):
        sm = re.search(r"\n  " + sec + r"\s*:\s*\{\n(.*?)\n  \},", script, re.S)
        if sm:
            sink.update(re.findall(r"^\s{4}(?:async\s+)?([a-zA-Z_$][\w$]*)\s*\(",
                                   sm.group(1), re.M))
    return data_keys, computed_keys, methods_keys, data_src, data_pairs


def top_level_keys(src: str) -> list[tuple[str, int]]:
    """取对象字面量**第一层**的键（带行号）。

    为什么不能直接用 ``name:`` 全文抓：``data()`` 里混着嵌套对象
    （``loginForm: { username:'', password:'' }``、``tabs: [{k:'home',n:'概
'}
    }]``），全文抓会把 ``username`` / ``k`` 这些**嵌套键**当成 data 成员。
    后果有两个方向：L2 的"已定义"集合被污染（真孤儿被放过），L3 把嵌套键
    误报成重复键。所以必须按花括号深度只取第一层。
    """
    out: list[tuple[str, int]] = []
    depth = 1  # src 是对象内部（data(){ return { 之后的内容）
    i, n = 0, len(src)
    line = 1
    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\n":
                    line += 1
                if src[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if (ch.isalpha() or ch in "_$") and depth == 1:
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            k = j
            while k < n and src[k] in " \t":
                k += 1
            if k < n and src[k] == ":" and not (k + 1 < n and src[k + 1] == ":"):
                out.append((src[i:j], line))
            i = j
            continue
        i += 1
    return out


def vfor_aliases(tpl: str) -> set[str]:
    """v-for 的循环变量是模板局部的，不算孤儿。

    ``v-for="d in devices"`` 的 ``d``、``v-for="(a, b) in pairs"`` 的
    ``a``/``b`` 都不在 app 成员里，但确实是合法引用。
    """
    aliases: set[str] = set()
    for expr in re.findall(r'v-for="([^"]+)"', tpl):
        head = expr.split(" in ", 1)[0] if " in " in expr else ""
        for name in re.findall(r"[a-zA-Z_$][\w$]*", head):
            aliases.add(name)
    return aliases


def strip_literals(expr: str) -> str:
    """把表达式里的字符串字面量替换成空串，位置（长度以外）保持不变。

    给 L1b 用：``:class="{'presetLock':modelLocked}"`` 里的 ``'presetLock'``
    是字符串，不是标识符；不剥的话只要某个方法名恰好与某个 CSS 类名同名，
    就会造出一条假失败。同理 ``v-if="kind==='showApiKey'"`` 这种也不该误报。
    """
    for q in ("'", '"', "`"):
        expr = re.sub(rf"{q}(?:[^\\{q}]|\\.)*{q}", "''", expr)
    return expr


def root_identifiers(expr: str) -> list[str]:
    """取一段 Vue/JS 表达式里的**根标识符**。

    规则（对应模块docstring 里的三个坑）：
      * 跳过 '...' / "..." / `...` 字符串字面量（三元里的 'ok' 不是标识符）；
      * ``a.b.c`` 只取 ``a``（b/c 是属性名）；
      * 花括号内的 ``name:`` 是对象字面量的键（CSS 属性名），跳过。
    """
    out: list[str] = []
    i, n = 0, len(expr)
    depth = 0
    expect_property = False
    while i < n:
        ch = expr[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n and expr[i] != quote:
                if expr[i] == "\\":
                    i += 1
                i += 1
            i += 1
            expect_property = False
            continue
        if ch == "{":
            depth += 1
            expect_property = False
            i += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            expect_property = False
            i += 1
            continue
        if ch == "/" and i + 1 < n and expr[i + 1] == "/":
            break  # 行注释：表达式结束
        if ch.isalpha() or ch in "_$":
            j = i
            while j < n and (expr[j].isalnum() or expr[j] in "_$"):
                j += 1
            name = expr[i:j]
            k = j
            while k < n and expr[k] in " \t\n":
                k += 1
            is_object_key = (k < n and expr[k] == ":" and depth > 0
                             and not (k + 1 < n and expr[k + 1] == ":"))
            is_property_chain = k < n and expr[k] == "."
            if not expect_property and not is_object_key:
                out.append(name)
            if is_property_chain:
                # a.b.c：跳到链尾，链上后续标识符都是属性名
                while k < n and expr[k] in " \t\n.":
                    k += 1
                while k < n and (expr[k].isalnum() or expr[k] in "_$"):
                    k += 1
                expect_property = False
                i = k
                continue
            expect_property = False
            i = j
            continue
        if ch == ".":
            # 点号后永远是属性名：a.b 与 (a||[]).length 两种形态都靠它。
            # 少了这一条，链尾的 length/join/toFixed 会被当成根标识符
            # （第一版就是这么冒出十几个假孤儿的）。
            expect_property = True
            i += 1
            continue
        if ch == "?":
            # ?. 可选链：问号本身重置，紧随的点号由上面接管
            expect_property = False
            i += 1
            continue
        expect_property = False
        i += 1
    return out


#: L4 卡片归属表：``守卫变量 -> 该卡片区域内允许出现的同前缀变量``。
#: **只列已知的、真实踩过的坑**，不是"要求每个文件都有这张卡"。
#:
#: 形态是「同一语义域的变量名共享一个词根」。邮件测试这张卡的守卫是
#: ``mailTestResult``，区域内只准出现 ``mail*`` / ``smtp*`` / ``test*Busy`` /
#: ``testTo`` 这些同域名字；一旦出现裸 ``testResult``（AI 配置弹窗的键，
#: 见 index.html:1354 的注释：同名会静默覆盖），就是 2026-10-19 那个白屏。
#:
#: 为什么只钉这一张卡而不是自动推断归属：自动推断要理解「语义」，
#: 静态脚本做不到；写死白名单虽然不通用，但**明确、可 review、零误报**，
#: 而这类事故的代价是整站白屏。宁可漏检，不可误报。
#:
#: 某文件里**找不到**这张卡时直接跳过（user.html 就没有邮件配置功能）——
#: 断言必须约束"已存在的东西"，而不是"必须新增东西"，否则每个新页面
#: 都会被这条与它无关的红灯拦住。
CARD_OWNERS = {
    "mailTestResult": {"mailTestResult", "testTo", "testBusy",
                       "smtpForm", "smtpErr", "smtpMsg"},
}


def card_region(tpl: str, guard: str) -> tuple[str, int] | None:
    """截取 ``v-if="<guard>"`` 所在元素闭合范围内的模板片段。

    用**标签深度**而不是行数：``<div v-if="x">`` 到同缩进层级的 ``</div>``
    之间的全部内容，就是这张卡管辖的区域。找不到闭合标签时返回 None
    （宁可漏检，不报错——静态脚本不该因模板写法变化而崩）。
    """
    m = re.search(r'<(\w+)[^>]*\bv-if="' + re.escape(guard) + r'"[^>]*>', tpl)
    if not m:
        return None
    tag = m.group(1)
    start = m.end()
    # 从开标签之后开始数同名标签的出现次数，见到配平的闭合标签即止。
    depth = 1
    for t in re.finditer(r'<(/?)' + tag + r'[\s>]', tpl[start:]):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            line = tpl.count("\n", 0, start + t.start()) + 1
            return tpl[start:start + t.start()], line
    return None


def check_card_ownership(tpl: str) -> None:
    for guard, allowed in CARD_OWNERS.items():
        got = card_region(tpl, guard)
        if got is None:
            print(f"  [SKIP] L4 卡片 [{guard}]：本文件没有这张卡，不检查")
            continue
        region, line = got
        # 区域内出现的所有根标识符，减去合法白名单，剩下的都是「外来变量」
        used: set[str] = set()
        for expr in re.findall(r"\{\{(.*?)\}\}", region):
            used |= set(root_identifiers(expr))
        for expr in re.findall(
                r'(?:v-if|v-else-if|v-show|v-for|:[\w-]+|v-bind:[\w-]+)="([^"]+)"', region):
            used |= set(root_identifiers(expr))
        for attr in re.findall(r'@[\w.]+="([^"]+)"', region):
            used |= set(root_identifiers(attr))
        for vm in re.findall(r'v-model(?::[\w.]+)?="([^"]+)"', region):
            used |= set(root_identifiers(vm))
        foreign = sorted(n for n in used
                         if n not in allowed and n not in BUILTINS
                         and n not in LOCAL_VARS and n not in vfor_aliases(region))
        check(
            f"L4 卡片 [{guard}] 的区域归属（用错别人的变量会白屏）",
            not foreign,
            f"第 {line} 行起的 [{guard}] 卡片内出现了不属于它的变量: {foreign}。"
            f"允许的: {sorted(allowed)}。这些名字在 data() 也有定义，"
            f"L1/L2/L3 抓不到——它们只会在这里变成 null.xxx 而让整页白屏。"
        )


def strip_comments(html: str) -> str:
    """剥掉 HTML 注释（里面有示例代码，不剥会造出假孤儿）。"""
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def strip_js_comments(src: str) -> str:
    """剥掉 JS 的 ``//`` 与 ``/* */`` 注释，**不动字符串里的内容**。

    为什么必须有这一步：本文件的断言大多是"某段代码必须存在 / 必须不存在"，
    而**防回归注释天生会引用被禁的那段代码** —— 例如删掉某个方法后，按惯例要
    在原地留一句"xxx() 已删，原因见…"。不剥注释，这句注释自己就把断言踩红。

    这条坑踩过不止一次（判据要按"代码位"判，不能整文件子串匹配）。假红比没有
    断言更坏：它会教人忽略红灯。

    已知局限：不识别正则字面量里的 ``//``（如 ``/a\\/\\/b/``）。本仓库的内联
    script 里没有这种写法；真出现了也不会造成漏判，最多把一段字符串误当注释。
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:          # 转义：整对吞掉
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
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


def scan_file(path: Path) -> None:
    print(f"\n==== {path.relative_to(SERVER.parent)} ====")
    html = path.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*)</script>", html, re.S)
    if not m:
        check(f"{path.name} 含内联 script", False, "没找到 <script> 块")
        return
    script = m.group(1)
    tpl = strip_comments(re.sub(r"<script>.*?</script>", "", html, flags=re.S))

    data_keys, computed_keys, methods_keys, data_src, data_pairs = parse_app(script)
    defined = data_keys | computed_keys | methods_keys

    # ---------------------------------------------------------- L1 方法插值
    # 只抓「整个插值就是一个裸标识符」的形态：带括号（{{foo()}}）是调用，安全；
    # 带点号/运算符的走 L2 的根标识符通道，那里只要求"有定义"。
    bare_interp = re.findall(r"\{\{\s*([a-zA-Z_$][\w$]*)\s*\}\}", tpl)
    bad = sorted({n for n in bare_interp if n in methods_keys})
    check("L1 没有被插值的方法名（Vue 3 bind 后会显示 function () { [native code] }）",
          not bad,
          f"这些名字定义在 methods: 里却被当成值插值: {bad}。"
          f"搬进 computed:，或在模板里写成 名字() 显式调用")

    # ------------------------------------------------- L1b 值位上的方法引用
    # 2026-10-25 线上事故（用户报「所有接入类型都填不了模型名，点自定义接入点
    # 没反应」）：一整组派生量（modelHidden / modelLocked / showApiKey /
    # endpointFromPreset / apiStyleHidden / localService / providerPreset …）
    # 被写在 methods 里，而模板是**当值**用的：
    #
    #     v-if="!modelHidden"   :disabled="modelLocked"   v-if="showApiKey"
    #
    # Vue 3 里方法不带括号就是函数对象，**恒为真**。后果：
    #   v-if="!modelHidden"  -> !函数 -> 永远 false -> 那一栏永不渲染
    #   :disabled="modelLocked" -> 函数 -> 永远禁用
    #   apiKeyHint 里 if (this.localService) -> 恒真 -> 永远走本地服务分支
    # 而且**点「改成自定义」也不会变**（绑的是函数对象，状态变了它还是那个函数）。
    #
    # L1 抓不到它：L1 只看 {{ }} 插值。L2 也抓不到：这些名字确实有定义，
    # 只是定义在 methods 里。所以要单独扫「值位」。
    #
    # 为什么**不含** @event / v-on:：那里写方法名正是正确用法（事件处理器）。
    value_exprs = []
    for expr in re.findall(
            r'(?:v-if|v-else-if|v-show|v-model(?::[\w.]+)?|v-for'
            r'|:[\w-]+|v-bind:[\w-]+)="([^"]+)"', tpl):
        value_exprs.append(expr)
    for expr in re.findall(r"\{\{(.*?)\}\}", tpl):
        value_exprs.append(expr)

    bad_value: list[str] = []
    for expr in value_exprs:
        clean = strip_literals(expr)
        for m in re.finditer(r"[a-zA-Z_$][\w$]*", clean):
            name = m.group(0)
            if name not in methods_keys:
                continue
            tail = clean[m.end():]
            if tail.startswith("("):
                continue                      # 显式调用，安全
            head = clean[:m.start()].rstrip()
            if head.endswith("."):
                continue                      # 成员名（a.foo 里的 foo）
            # 作为回调**传给**别的方法：``list.map(labelOfUsage)``、
            # ``(a, labelOfUsage)`` —— 函数引用在这里是正确的用法，
            # 只有「求值」位置才是 bug。判据取两头同时成立，避免误放。
            if head.endswith(("(", ",")) and tail.lstrip().startswith((")", ",")):
                continue
            bad_value.append(f"{name} in «{expr.strip()[:70]}»")
    check("L1b 值位上没有方法引用（v-if / :prop / {{ }} 里写方法名 = 函数恒为真）",
          not bad_value,
          f"这些名字在 methods: 里，却在值位被引用: {sorted(set(bad_value))}。"
          f"函数对象恒为真 ⇒ v-if 永不成立、:disabled 永远禁用，且状态变化不会"
          f"触发重算。搬进 computed:，或在模板里显式调用 名字()")

    # ---------------------------------------------------------- L2 孤儿引用
    used: set[str] = set()
    # 事件处理器：整段都是代码，取全部根标识符
    for attr in re.findall(r'@[\w.]+="([^"]+)"', tpl):
        used |= set(root_identifiers(attr))
    # v-model：单个路径
    for vm in re.findall(r'v-model(?::[\w.]+)?="([^"]+)"', tpl):
        used |= set(root_identifiers(vm))
    # 文本插值
    for expr in re.findall(r"\{\{(.*?)\}\}", tpl):
        used |= set(root_identifiers(expr))
    # 条件/循环/绑定属性
    for expr in re.findall(
            r'(?:v-if|v-else-if|v-show|v-for|:[\w-]+|v-bind:[\w-]+)="([^"]+)"', tpl):
        used |= set(root_identifiers(expr))
    orphans = sorted(
        n for n in used
        if n not in defined and n not in BUILTINS and n not in LOCAL_VARS
        and n not in vfor_aliases(tpl))
    check("L2 模板无孤儿引用（每个根标识符都在 data/computed/methods 里有定义）",
          not orphans,
          f"未定义: {orphans}。Vue 3 对孤儿引用不报错，只渲染成空 —— "
          f"表现为「功能看着在，点了没反应」")

    # ---------------------------------------------------------- L3 重复键
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for name, ln in data_pairs:
        if name in seen:
            dupes.append(f"{name}（第 {seen[name]} 行与第 {ln} 行）")
        else:
            seen[name] = ln
    check("L3 data() 无重复键（后者会静默覆盖前者，且两处往往隔得很远）",
          not dupes, f"重复: {dupes}")

    # ---------------------------------------------------------- L4 卡片归属
    check_card_ownership(tpl)

    # ---------------------------------------------------------- L5 网络层
    # ⚠️ 传 script 而不是 tpl：tpl 是**剥掉 <script> 之后**的模板，
    # api() 在 script 里。传错对象会让这条断言永远「找不到 api()」。
    check_network_layer(script)

    # ---------------------------------------------------------- L6 单一绑定入口
    # 只对**用户端**断言：管理端本来就没有绑定入口（设备由家属在 / 页绑定）。
    if path == USER:
        check_single_bind_entry(tpl, script)


def check_single_bind_entry(tpl: str, script: str) -> None:
    """L6：用户端绑定设备**只能有一条路径** —— 输入屏幕上的 7 位激活码。

    2026-10-30 用户问「添加设备这两项是不是重复了」。查下来不是"看起来像"：
    页面上并排放着两个表单，都只是「建一行 Device 并把 owner_id 设成当前用户」，
    但一个要 7 位激活码、另一个要 **eFuse MAC**。而设备屏幕只显示激活码
    （elder_bind_guide.cc:303），机身标签印没印那串地址在仓库里查无依据，
    本页帮助区自己也写着「要求用户提供它 = 把求助门槛设在他够不到的地方」。
    ⇒ 按 MAC 的那块**在正常流程里没人填得出来**，只会让人以为有两套流程；
    而且它比激活码路径少了三件事：不作废同设备残留码、不返回设备 token、
    没有归属保护（已登记就直接 400）。

    这条断言守的是"别再长回来"：它属于**零报错**的那类问题（页面照样能开、
    语法照样合法），只能静态拦。真要按 MAC 预登记，应该做在管理端。
    """
    # ⚠️ 必须先剥注释再匹配：删除这两个东西时，原地按惯例会留一句
    # "devForm / createDevice 已删，原因见…"，那句注释自己就会把断言踩红。
    # tpl 在 scan_file 里已剥过 HTML 注释；script 是原始的，这里补剥 JS 注释。
    code = strip_js_comments(script)

    if "输入激活码绑定设备" not in tpl:
        check("L6a 用户端有激活码绑定入口", False,
              "找不到「输入激活码绑定设备」卡片 —— 绑定入口没了？")
        return
    check("L6a 用户端有激活码绑定入口", True)

    check("L6b 用户端没有第二套「按 MAC 登记设备」表单",
          "devForm" not in tpl and "devForm" not in code,
          "又出现了按 MAC 登记设备的表单/方法。它与激活码绑定结果重复，"
          "且 MAC 在设备屏幕上根本不显示（用户拿不到）——"
          "要按 MAC 预登记请做在管理端")

    check("L6c 没有残留的 createDevice 方法",
          "createDevice" not in code,
          "createDevice() 还在 ⇒ 可能又挂回了某个按钮上")


def check_network_layer(src: str) -> None:
    """L5：前端请求层必须带**硬超时 + 幂等重试 + 人话错误**。

    为什么这条要进门禁：``fetch`` 默认**永不超时**。把它改回裸 fetch，
    语法照样合法、页面照样能打开、单测也照样绿 —— 只有在弱网下才会暴露，
    而那时的表现是"页面一直转圈"或页面里出现一行英文 ``Failed to fetch``，
    用户既看不懂也等不到结果。2026-10-30 用户反馈的正是这两句话。

    反过来，这几个特征一旦被后人"顺手简化"掉，也是**零报错**的，
    所以只能靠静态断言守住。
    """
    i = src.find("async api(path, opt){")
    if i < 0:
        check("L5 找得到 api() 请求封装", False, "没有 async api(path, opt)")
        return
    body = src[i:i + 2600]

    check("L5a api() 带硬超时（AbortController + 定时 abort）",
          "AbortController" in body and "ac.abort()" in body,
          "没有超时 ⇒ 连接卡住时页面永远停在加载态")
    check("L5b api() 把 signal 传给了 fetch",
          "signal: ac.signal" in body,
          "建了 AbortController 却没传 signal，等于没做超时")
    check("L5c api() 只对幂等方法重试",
          "const tries" in body and "'GET'" in body,
          "没有按方法区分重试 ⇒ POST 重试会重复创建（点一次建两条）")
    check("L5d 网络异常经 netErrText 翻译（不把英文原文抛给用户）",
          "netErrText(lastErr)" in src and "netErrText(e){" in src.replace(" ", ""),
          "用户会直接看到 Failed to fetch")
    # 登录是唯一绕开 api() 的裸 fetch（401 语义不同），也必须走同一套翻译。
    # 只在**有这个入口的文件**里断言：管理端 index.html 没有登录表单，
    # 对它硬要求这句会得到一条永远红的假断言（而假红比没有断言更坏）。
    if "/api/auth/login" in src:
        check("L5e 登录的裸 fetch 也用 netErrText",
              "this.err = this.netErrText(e)" in src,
              "登录入口仍会显示英文 Failed to fetch")


APP_SRC = SERVER.parent / "app" / "src"


def scan_app_templates() -> None:
    """扫描 uni-app (.vue) 模板：**禁止把 uni.* 写在模板表达式里**。

    2026-10-19 实锤的坑（用户报「系统形象库 / 隐私政策 / 用户协议点不动」，
    三个症状一个根因）：

        @click="uni.navigateTo({ url: '/x' })"     ← 坏：不生效、不报错

    ``uni.*`` 是 @dcloudio/uni-app 插件在**构建期**注入的，插件改写的是
    JS 源码里的调用点；写在模板表达式里它不参与改写，产物里原样变成
    ``o.uni.navigateTo(...)`` —— ``o`` 是 setup 上下文的解构对象，
    它身上的 ``uni`` 不是那个 API 对象 ⇒ 调用抛 TypeError。
    而异常发生在 Vue 的事件处理器里、没有全局 errorHandler ⇒ **被静默吞掉**；
    模板表达式语法又完全合法 ⇒ **构建也不报错**。
    症状于是是「构建成功 + 点击毫无反应」，是能查到吐的假绿。

    为什么值得单独一条门禁：上面这两条（不报错、不构建失败）都让它躲过了
    现有全部检查。而正确写法（把跳转放进 script里的具名函数）一眼就能认出来，
    用正则把坏形态钉死，比指望人记住便宜得多。
    """
    files = sorted(APP_SRC.rglob("*.vue"))
    if not files:
        check("APP 模板扫描（.vue 至少有一个）", False,
              f"{APP_SRC} 下没有 .vue，确认路径")
        return

    bad: list[str] = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        tpl = re.sub(r"<script.*?</script>", "", text, flags=re.S)
        tpl = strip_comments(tpl)
        for m in re.finditer(r'(:|v-bind:|@)?[\w.:-]*="([^"]*\buni\.[A-Za-z_]+[^"]*)"',
                             tpl):
            attr_line = tpl[:m.start()].count("\n") + 1
            bad.append(f"{p.relative_to(SERVER.parent)}:{attr_line}  {m.group(2).strip()[:60]}")

    check("APP 模板里没有内联的 uni.* 调用（必须放进 script 的具名函数）",
          not bad,
          "这些位置把 uni.* 写在模板表达式里，构建期不会被插件改写 ⇒ "
          "点击时抛 TypeError 且被静默吞掉（构建却成功）：\n         "
          + "\n         ".join(bad[:10])
          + ("\n         …共 %d 处" % len(bad) if len(bad) > 10 else ""))


def main() -> int:
    print("=" * 64)
    print("前端模板卫生（静态扫描：方法插值 / 孤儿引用 / 重复键 / uni 内联）")
    print("=" * 64)
    missing = [p for p in (INDEX, USER) if not p.exists()]
    if missing:
        print(f"[fatal] 找不到文件: {[str(m) for m in missing]}")
        return 1
    for path in (INDEX, USER):
        scan_file(path)
    scan_app_templates()
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
