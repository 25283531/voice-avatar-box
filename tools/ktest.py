#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地跑 lx04 的 JVM 单测（不需要 Android SDK / gradle）。

与 `tools/kcompile.py` 同一套思路：kotlin-compiler + 最小 Android stub。
区别是这里额外编 test 源码并用 JUnit 跑。

为什么需要它：
  CI 的「跑单元测试」那一步要跑 gradle，一轮 1.5 分钟；
  而单测失败时最费时间的是"改完再等一次 CI"。
  本地 10 秒能出结果，就能把迭代闭环缩短一个数量级。

⚠️ `android.util.Log` 在 JVM 里没有实现 —— 靠 `isReturnDefaultValues`
   （gradle 侧）或本脚本提供的 stub 实现（本地侧）绕开：
   本脚本编译时带上 `tools/androidstub/`，那些 Log 方法返回 0。

用法：python tools/ktest.py
"""
from __future__ import annotations

import argparse
import os
import re
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

MAVEN = "https://repo1.maven.org/maven2/"

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
    "junit-4.13.2.jar": MAVEN + "junit/junit/4.13.2/junit-4.13.2.jar",
    "hamcrest-core-1.3.jar": MAVEN + "org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar",
}


def find_java() -> Path | None:
    env = os.environ.get("JAVA_HOME")
    if env:
        p = Path(env) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if p.is_file():
            return p
    for p in [Path("C:/Users/Administrator/.jdks/jbr-17.0.14/bin/java.exe")]:
        if p.is_file():
            return p
    which = shutil.which("java")
    return Path(which) if which else None


def ensure(dst: Path, url: str) -> bool:
    if dst.is_file() and dst.stat().st_size > 10_000:
        with dst.open("rb") as f:
            if f.read(4) == b"PK\x03\x04":
                return True
        dst.unlink(missing_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=180) as resp, dst.open("wb") as out:
                shutil.copyfileobj(resp, out)
            with dst.open("rb") as f:
                if f.read(4) == b"PK\x03\x04":
                    return True
        except Exception as exc:  # noqa: BLE001
            print(f"  下载失败 {url}: {exc}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(HERE.parent))
    args = ap.parse_args()

    java = find_java()
    if java is None:
        print("找不到 JDK")
        return 2

    for n, u in KOTLIN_JARS.items():
        if not ensure(KOTLINC / n, u):
            return 2
    for n, u in APP_JARS.items():
        if not ensure(LIBS / n, u):
            return 2

    repo = Path(args.repo)
    main_src = repo / "lx04/app/src/main/java"
    test_src = repo / "lx04/app/src/test/java"
    if not test_src.is_dir():
        print(f"找不到测试目录：{test_src}")
        return 2

    out = CACHE / "testout"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    env = dict(os.environ)
    for k in ("MSYSTEM", "MSYS2_ARG_CONV_EXCL", "PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)

    files = [str(p) for p in main_src.rglob("*.kt")]
    files += [str(p) for p in test_src.rglob("*.kt")]

    sep = ";" if os.name == "nt" else ":"
    kcp = sep.join(str(KOTLINC / n) for n in KOTLIN_JARS)

    # ⚠️ 两阶段：javac 编 .java（Concentus + stub），kotlinc 编 .kt。
    #    kotlinc **不编 .java**，所以 Concentus 必须先由 javac 编出 class。
    java_out = CACHE / "javaout"
    if java_out.exists():
        shutil.rmtree(java_out)
    java_out.mkdir(parents=True)
    javac = java.with_name("javac.exe" if os.name == "nt" else "javac")
    if not javac.is_file():
        javac = Path(shutil.which("javac") or javac)
    java_files = [str(p) for p in STUB.rglob("*.java")] + \
                 [str(p) for p in main_src.rglob("*.java")]
    jr = subprocess.run(
        [str(javac), "-nowarn", "-encoding", "UTF-8", "-d", str(java_out)] + java_files,
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    jtext = (jr.stdout or "") + (jr.stderr or "")
    jerrs = [ln for ln in jtext.splitlines() if "error:" in ln]
    if jr.returncode != 0 or jerrs:
        print(f"❌ Java 阶段失败：{len(jerrs)} 个错误")
        for e in jerrs[:20]:
            print("   ", e.strip()[:180])
        if not jerrs:
            for ln in jtext.splitlines()[-15:]:
                print("   ", ln)
        return 1

    acp = sep.join([str(java_out)] +
                   [str(LIBS / n) for n in APP_JARS] +
                   [str(KOTLINC / "kotlin-stdlib-1.9.24.jar")])

    cmd = [str(java), "-cp", kcp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
           "-no-stdlib", "-jvm-target", "1.8", "-classpath", acp, "-d", str(out)] + files
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    text = (r.stdout or "") + (r.stderr or "")
    errors = [ln for ln in text.splitlines() if "e: " in ln and "file:" in ln]
    if r.returncode != 0 or errors:
        print(f"❌ 测试源码编译失败：{len(errors)} 个错误（rc={r.returncode}）")
        for e in errors[:30]:
            print("   ", e.replace("file:///", "").strip())
        if not errors:
            print("--- 原始输出尾部 ---")
            for ln in text.splitlines()[-20:]:
                print("   ", ln)
        return 1

    # 找测试类并跑
    classes = []
    for p in test_src.rglob("*.kt"):
        s = p.read_text(encoding="utf-8")
        m = re.search(r"package\s+([\w.]+)", s)
        c = re.search(r"class\s+(\w+)", s)
        if m and c:
            classes.append(f"{m.group(1)}.{c.group(1)}")
    if not classes:
        print("没找到测试类")
        return 1

    rc = sep.join([str(out), str(java_out), str(LIBS / "junit-4.13.2.jar"),
                   str(LIBS / "hamcrest-core-1.3.jar"),
                   str(LIBS / "kotlinx-coroutines-core-jvm-1.7.3.jar"),
                   str(KOTLINC / "kotlin-stdlib-1.9.24.jar"),
                   # ⚠️ Concentus 是 .java 源码，编译产物在 out 里；
                   #    但它的包名是 org.concentus，与测试类同一个 classpath 根 ——
                   #    漏了它就是 "ClassNotFoundException: org.concentus.OpusEncoder"。
                   str(LIBS / "kotlinx-coroutines-android-1.7.3.jar")])
    tr = subprocess.run([str(java), "-cp", rc, "org.junit.runner.JUnitCore"] + classes,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env)
    tout = (tr.stdout or "") + (tr.stderr or "")
    print(tout.strip()[-3000:])
    return 0 if tr.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
