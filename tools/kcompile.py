#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地编译 lx04 客户端（不需要 Android SDK）。

## 为什么需要它

本机没有 Android SDK，`./gradlew` 跑不起来，Kotlin 编译错误只能靠 CI 兜 ——
而 CI 一轮要 1.5 分钟。2026-11-05 连续三次推送被 CI 打回：

| CI run | 错误 |
| --- | --- |
| 41972423369 | `AudioCapture.kt:248 Unresolved reference: !`（Thread.join 返回 void） |
| 41972940902 | `OpusCodec.kt:233 A 'return' expression required in a function with a block body` |
| 41973478969 | `OpusCodec.kt` 多一个右花括号 ⇒ companion object 被挤出类体 ⇒ TAG/encoder 全未解析 |

三次都是**纯 Kotlin 语义错误**，静态门禁（括号配平、关键字存在）全绿。
⇒ 门禁绿 ≠ 编译得过。必须有本地编译这一层。

## 怎么做到的

* Kotlin 编译器从 Maven 拿（kotlin-compiler 1.9.24，与项目一致）；
* `tools/androidstub/` 下是 **Android SDK 的最小 stub**（48 个 .java），
  只声明 lx04 用到的那部分 API —— 足够让编译器做**类型检查**；
* okhttp / okio / org.json / kotlinx-coroutines 从 Maven 拿真 jar；
* Concentus 是仓库里的 .java 源码，一起编进去。

它**不能**替代 gradle（不打 APK、不做资源合并、不做 lint），
但它能在 30 秒内抓出绝大多数 Kotlin 编译错误 —— 而这正是本地最需要的。

## 用法

```bash
python tools/kcompile.py                 # 编译当前工作树
python tools/kcompile.py --repo <别的目录> # 编译别处（做变异验证用）
```

首次运行需要下载依赖（约 70MB，缓存在 `tools/.kcache/`）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".kcache"
KOTLINC = CACHE / "kotlinc"
LIBS = CACHE / "libs"
STUB = HERE / "androidstub"

JAVA_CANDIDATES = [
    Path("C:/Users/Administrator/.jdks/jbr-17.0.14/bin/java.exe"),
    Path("C:/Program Files/Java/jdk-17/bin/java.exe"),
    Path("C:/Program Files/Eclipse Adoptium/jdk-17/bin/java.exe"),
]

MAVEN = "https://repo1.maven.org/maven2/"


def find_java() -> Path | None:
    """找 JDK 17：先看环境变量，再用已知路径。"""
    env = os.environ.get("JAVA_HOME")
    if env:
        p = Path(env) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if p.is_file():
            return p
    for p in JAVA_CANDIDATES:
        if p.is_file():
            return p
    which = shutil.which("java")
    if which:
        return Path(which)
    return None

KOTLIN_JARS = {
    "kc.jar": MAVEN + "org/jetbrains/kotlin/kotlin-compiler/1.9.24/kotlin-compiler-1.9.24.jar",
    "kotlin-stdlib-1.9.24.jar": MAVEN + "org/jetbrains/kotlin/kotlin-stdlib/1.9.24/kotlin-stdlib-1.9.24.jar",
    "kotlin-reflect-1.9.24.jar": MAVEN + "org/jetbrains/kotlin/kotlin-reflect/1.9.24/kotlin-reflect-1.9.24.jar",
    "kotlin-script-runtime-1.9.24.jar": MAVEN + "org/jetbrains/kotlin/kotlin-script-runtime/1.9.24/kotlin-script-runtime-1.9.24.jar",
    "kotlin-daemon-embeddable-1.9.24.jar": MAVEN + "org/jetbrains/kotlin/kotlin-daemon-embeddable/1.9.24/kotlin-daemon-embeddable-1.9.24.jar",
    "trove4j.jar": MAVEN + "org/jetbrains/intellij/deps/trove4j/1.0.20200330/trove4j-1.0.20200330.jar",
    "annotations-13.0.jar": MAVEN + "org/jetbrains/annotations/13.0/annotations-13.0.jar",
}

APP_JARS = {
    "okhttp-4.12.0.jar": MAVEN + "com/squareup/okhttp3/okhttp/4.12.0/okhttp-4.12.0.jar",
    "okio-jvm-3.6.0.jar": MAVEN + "com/squareup/okio/okio-jvm/3.6.0/okio-jvm-3.6.0.jar",
    "json-20231013.jar": MAVEN + "org/json/json/20231013/json-20231013.jar",
    "kotlinx-coroutines-core-jvm-1.7.3.jar": MAVEN + "org/jetbrains/kotlinx/kotlinx-coroutines-core-jvm/1.7.3/kotlinx-coroutines-core-jvm-1.7.3.jar",
    "kotlinx-coroutines-android-1.7.3.jar": MAVEN + "org/jetbrains/kotlinx/kotlinx-coroutines-android/1.7.3/kotlinx-coroutines-android-1.7.3.jar",
}


def ensure_jar(dst: Path, url: str) -> bool:
    if dst.is_file() and dst.stat().st_size > 10_000:
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlretrieve(url, dst)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"下载失败 {url}: {exc}")
        return False


def prepare() -> bool:
    ok = True
    for name, url in KOTLIN_JARS.items():
        if not ensure_jar(KOTLINC / name, url):
            ok = False
    for name, url in APP_JARS.items():
        if not ensure_jar(LIBS / name, url):
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="本地编译 lx04 客户端（无需 Android SDK）")
    ap.add_argument("--repo", default=str(HERE.parent),
                    help="仓库根目录（默认：本脚本所在 tools/ 的上一级）")
    ap.add_argument("--out", default=str(CACHE / "out"))
    args = ap.parse_args()

    java = find_java()
    if java is None:
        print("找不到 JDK —— 设 JAVA_HOME 或把 java 放进 PATH")
        return 2

    if not prepare():
        print("依赖下载失败 —— 首次运行需要能访问 repo1.maven.org")
        return 2

    repo = Path(args.repo)
    src = repo / "lx04/app/src/main/java"
    if not src.is_dir():
        print(f"找不到源码目录：{src}")
        return 2

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    files = [str(p) for p in STUB.rglob("*.java")]
    files += [str(p) for p in src.rglob("*.kt")]
    # ⚠️ Concentus 是 **.java** 源码（124 个文件），不在 .kt 里 ——
    #    漏了它，OpusDecoder/OpusEncoder 就是未解析的，
    #    报错会变成 "unresolved reference: decode"（看着像自己的代码错了，
    #    其实是缺依赖）。这个坑本机踩过一次。
    files += [str(p) for p in src.rglob("*.java")]

    kcp = ";".join(str(KOTLINC / n) for n in KOTLIN_JARS)
    acp = ";".join([str(LIBS / n) for n in APP_JARS] + [str(KOTLINC / "kotlin-stdlib-1.9.24.jar")])

    cmd = [
        str(java), "-cp", kcp,
        "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
        "-no-stdlib", "-jvm-target", "1.8",
        "-classpath", acp,
        "-d", str(out),
    ] + files

    # ⚠️ Git Bash 会改写 Windows exe 的参数（MSYSTEM 等环境变量也会污染 JVM），
    #    所以这里显式清掉。用 Python subprocess 调也是为了绕开这层改写。
    env = dict(os.environ)
    for k in ("MSYSTEM", "MSYS2_ARG_CONV_EXCL", "PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    text = (r.stdout or "") + (r.stderr or "")
    # ⚠️ 错误行可能带前导空白或 BOM —— 用"包含 e: "判断，别用 startswith。
    errors = [ln for ln in text.splitlines() if "e: " in ln and "file:" in ln]
    warns = [ln for ln in text.splitlines() if "w: " in ln and "file:" in ln]

    if r.returncode != 0 or errors:
        print(f"❌ 编译失败：{len(errors)} 个错误（rc={r.returncode}）")
        for e in errors[:40]:
            print("   ", e.replace("file:///", "").strip())
        if not errors:
            print("--- 原始输出尾部 ---")
            for ln in text.splitlines()[-15:]:
                print("   ", ln)
        return 1

    print(f"✅ 编译通过（{len(files)} 个源文件，{len(warns)} 条警告）")
    for w in warns[:10]:
        print("   ", w.replace("file:///", "").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
